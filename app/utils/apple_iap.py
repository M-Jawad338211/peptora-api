"""Apple In-App Purchase verification.

The iOS app buys Peptora Pro through StoreKit 2. StoreKit hands the app a
signed transaction (a JWS) and the app posts it here; Apple also posts signed
App Store Server Notifications (V2) straight to this API whenever a
subscription renews, lapses or is refunded.

Both are verified the same way: the JWS carries its certificate chain, and
that chain must lead to Apple's root certificate. There is no shared secret.
The root certificate is bundled in app/certs and pinned by its SHA-256
fingerprint below, so swapping the file for another certificate fails loudly
at the first verification instead of quietly trusting a different signer.

Verification is done by Apple's own library (app-store-server-library). It is
synchronous, so callers in request handlers should run these functions through
asyncio.to_thread.

Never trust a field read from a transaction before verify_transaction() has
returned it.
"""

import base64
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.models.Type import Type
from appstoreserverlibrary.signed_data_verifier import (
    SignedDataVerifier,
    VerificationException,
)

from app.config import settings

logger = logging.getLogger("peptora.iap")

ROOT_CERT_PATH = Path(__file__).resolve().parent.parent / "certs" / "AppleRootCA-G3.cer"

# SHA-256 of "Apple Root CA - G3" (DER), as published at
# https://www.apple.com/certificateauthority/ and valid until 30 April 2039.
APPLE_ROOT_CA_G3_SHA256 = "63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179"

# A signed transaction is about 6 KB and a notification about 12 KB. Anything
# far beyond that is not from Apple, and is refused before any parsing.
MAX_SIGNED_TRANSACTION_CHARS = 20_000
MAX_SIGNED_NOTIFICATION_CHARS = 100_000

# Apple's free-trial markers on a transaction.
_OFFER_TYPE_INTRODUCTORY = 1
_OFFER_DISCOUNT_FREE_TRIAL = "FREE_TRIAL"


class AppleVerificationError(Exception):
    """A payload that is not a valid, acceptable App Store payload.

    `reason` is a short machine-readable code for the logs. It is never shown
    to the client, which only needs to know the payload was refused.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class AppleTransaction:
    """The verified fields of one StoreKit 2 transaction."""

    original_transaction_id: str
    transaction_id: str
    product_id: str
    environment: str
    purchased_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    signed_at: datetime | None
    app_account_token: str | None
    is_trial_period: bool
    raw: dict = field(default_factory=dict)

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.revoked_at is None and self.expires_at is not None and self.expires_at > now


@dataclass
class AppleNotification:
    """The verified fields of one App Store Server Notification (V2)."""

    notification_type: str
    subtype: str | None
    notification_uuid: str | None
    environment: str | None
    transaction: AppleTransaction | None
    auto_renew: bool | None
    grace_expires_at: datetime | None


def _from_ms(value) -> datetime | None:
    """Apple timestamps are milliseconds since the Unix epoch."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


@lru_cache(maxsize=1)
def _root_certificates() -> tuple:
    """Apple's root certificate, checked against the pinned fingerprint."""
    data = ROOT_CERT_PATH.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != APPLE_ROOT_CA_G3_SHA256:
        raise RuntimeError(
            "app/certs/AppleRootCA-G3.cer does not match the pinned Apple Root CA G3 fingerprint"
        )
    return (data,)


@lru_cache(maxsize=4)
def _verifier(environment: Environment) -> SignedDataVerifier:
    return SignedDataVerifier(
        root_certificates=list(_root_certificates()),
        enable_online_checks=settings.APPLE_IAP_ONLINE_CHECKS,
        environment=environment,
        bundle_id=settings.APPLE_BUNDLE_ID,
        app_apple_id=settings.APPLE_APP_ID,
    )


def reset_caches() -> None:
    """Drop cached verifiers and certificates. Used by the tests, which swap
    in their own certificate authority."""
    _verifier.cache_clear()
    clear = getattr(_root_certificates, "cache_clear", None)
    if clear is not None:
        clear()


def _peek_claims(signed: str) -> dict:
    """Read a JWS payload WITHOUT verifying it.

    Used for exactly one thing: finding out which App Store environment the
    payload claims to be from, so the matching verifier can be chosen. The
    verifier then checks the signature and re-checks the environment, so a
    lie here only ever leads to a refusal.
    """
    try:
        payload = signed.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:
        raise AppleVerificationError("malformed") from exc
    if not isinstance(claims, dict):
        raise AppleVerificationError("malformed")
    return claims


def _environment_for(name) -> Environment:
    """Map a claimed environment onto a verifier we are willing to build.

    Only Production and Sandbox. Apple's library skips signature checks
    entirely for its Xcode and LocalTesting environments, so letting a client
    choose one of those would let it forge any purchase.
    """
    if name == Environment.PRODUCTION.value:
        return Environment.PRODUCTION
    if name == Environment.SANDBOX.value:
        if not settings.APPLE_IAP_ALLOW_SANDBOX:
            raise AppleVerificationError("sandbox_not_allowed")
        return Environment.SANDBOX
    raise AppleVerificationError("environment_not_allowed")


def _transaction_from(decoded, claims: dict) -> AppleTransaction:
    if decoded.type != Type.AUTO_RENEWABLE_SUBSCRIPTION:
        raise AppleVerificationError("not_a_subscription")
    if decoded.productId not in settings.apple_product_ids:
        raise AppleVerificationError("unknown_product")
    if not decoded.originalTransactionId or not decoded.transactionId:
        raise AppleVerificationError("missing_transaction_id")

    is_trial = (
        decoded.rawOfferType == _OFFER_TYPE_INTRODUCTORY
        and decoded.rawOfferDiscountType == _OFFER_DISCOUNT_FREE_TRIAL
    )
    environment = decoded.rawEnvironment or (decoded.environment.value if decoded.environment else "")
    return AppleTransaction(
        original_transaction_id=str(decoded.originalTransactionId),
        transaction_id=str(decoded.transactionId),
        product_id=decoded.productId,
        environment=environment,
        purchased_at=_from_ms(decoded.purchaseDate),
        expires_at=_from_ms(decoded.expiresDate),
        revoked_at=_from_ms(decoded.revocationDate),
        signed_at=_from_ms(decoded.signedDate),
        app_account_token=str(decoded.appAccountToken).lower() if decoded.appAccountToken else None,
        is_trial_period=is_trial,
        raw=claims,
    )


def verify_transaction(signed: str) -> AppleTransaction:
    """Verify one signed StoreKit 2 transaction and return its fields.

    Raises AppleVerificationError for anything that is not a genuine Peptora
    Pro subscription transaction: a bad signature, another app's bundle id, an
    unknown product, or an environment this API does not accept.
    """
    if not isinstance(signed, str) or not signed or len(signed) > MAX_SIGNED_TRANSACTION_CHARS:
        raise AppleVerificationError("malformed")

    claims = _peek_claims(signed)
    environment = _environment_for(claims.get("environment"))
    try:
        decoded = _verifier(environment).verify_and_decode_signed_transaction(signed)
    except VerificationException as exc:
        raise AppleVerificationError(f"verification_{exc.status.name.lower()}") from exc
    return _transaction_from(decoded, claims)


def verify_notification(signed_payload: str) -> AppleNotification:
    """Verify one App Store Server Notification (V2) and return its fields.

    The notification, the transaction inside it and the renewal info inside it
    are three separately signed objects. All three are verified.
    """
    if (
        not isinstance(signed_payload, str)
        or not signed_payload
        or len(signed_payload) > MAX_SIGNED_NOTIFICATION_CHARS
    ):
        raise AppleVerificationError("malformed")

    claims = _peek_claims(signed_payload)
    body = claims.get("data") or claims.get("summary") or claims.get("appData") or {}
    environment = _environment_for(body.get("environment") if isinstance(body, dict) else None)
    verifier = _verifier(environment)

    try:
        decoded = verifier.verify_and_decode_notification(signed_payload)
    except VerificationException as exc:
        raise AppleVerificationError(f"verification_{exc.status.name.lower()}") from exc

    data = decoded.data
    transaction = None
    auto_renew = None
    grace_expires_at = None

    if data is not None and data.signedTransactionInfo:
        try:
            tx = verifier.verify_and_decode_signed_transaction(data.signedTransactionInfo)
        except VerificationException as exc:
            raise AppleVerificationError(f"verification_{exc.status.name.lower()}") from exc
        try:
            transaction = _transaction_from(tx, _peek_claims(data.signedTransactionInfo))
        except AppleVerificationError as exc:
            # Genuinely from Apple, but about a product this API does not sell
            # access for. Nothing to apply; the caller acknowledges it.
            logger.info("apple_notification_ignored reason=%s", exc.reason)
            transaction = None

    if data is not None and data.signedRenewalInfo:
        try:
            renewal = verifier.verify_and_decode_renewal_info(data.signedRenewalInfo)
        except VerificationException as exc:
            raise AppleVerificationError(f"verification_{exc.status.name.lower()}") from exc
        if renewal.rawAutoRenewStatus is not None:
            auto_renew = renewal.rawAutoRenewStatus == 1
        grace_expires_at = _from_ms(renewal.gracePeriodExpiresDate)

    return AppleNotification(
        notification_type=decoded.rawNotificationType or "",
        subtype=decoded.rawSubtype,
        notification_uuid=decoded.notificationUUID,
        environment=environment.value,
        transaction=transaction,
        auto_renew=auto_renew,
        grace_expires_at=grace_expires_at,
    )
