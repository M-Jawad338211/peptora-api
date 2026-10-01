"""A stand-in App Store for the tests.

Apple signs transactions and notifications with a three-certificate chain:
its root CA, an intermediate, and a leaf that does the signing. Real payloads
cannot be produced without Apple's private keys, so the tests build their own
chain with the same shape, and point the verifier at the test root instead of
Apple's. Everything else is the production code path: the same library, the
same chain validation, the same marker extensions on the certificates.

That makes these honest tests of the things that matter (a tampered payload,
another signer, the wrong app, an unknown product), while tests/test_iap.py
separately pins that the certificate bundled for production really is Apple's.
"""

import base64
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

BUNDLE_ID = "app.peptora"
APP_APPLE_ID = 6772127291
MONTHLY = "app.peptora.pro.monthly"
YEARLY = "app.peptora.pro.yearly"

# Marker extensions Apple puts on its certificates. The verifier refuses a
# chain whose intermediate and leaf do not carry them.
_OID_INTERMEDIATE = x509.ObjectIdentifier("1.2.840.113635.100.6.2.1")
_OID_LEAF = x509.ObjectIdentifier("1.2.840.113635.100.6.11.1")
_DER_NULL = b"\x05\x00"


def _name(cn: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Peptora Test Authority"),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class FakeAppStore:
    """Issues signed transactions and notifications from a private test CA."""

    def __init__(self):
        now = datetime.now(timezone.utc)
        not_before = now - timedelta(days=1)
        not_after = now + timedelta(days=365)

        root_key = ec.generate_private_key(ec.SECP384R1())
        root = (
            x509.CertificateBuilder()
            .subject_name(_name("Test Root CA"))
            .issuer_name(_name("Test Root CA"))
            .public_key(root_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False, content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=True,
                    crl_sign=True, encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False)
            .sign(root_key, hashes.SHA384())
        )

        inter_key = ec.generate_private_key(ec.SECP384R1())
        inter = (
            x509.CertificateBuilder()
            .subject_name(_name("Test Worldwide Developer Relations"))
            .issuer_name(root.subject)
            .public_key(inter_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False, content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=True,
                    crl_sign=True, encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(inter_key.public_key()), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False
            )
            .add_extension(x509.UnrecognizedExtension(_OID_INTERMEDIATE, _DER_NULL), critical=False)
            .sign(root_key, hashes.SHA384())
        )

        self._leaf_key = ec.generate_private_key(ec.SECP256R1())
        leaf = (
            x509.CertificateBuilder()
            .subject_name(_name("Test App Store Signing"))
            .issuer_name(inter.subject)
            .public_key(self._leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=False,
                    crl_sign=False, encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self._leaf_key.public_key()), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(inter_key.public_key()), critical=False
            )
            .add_extension(x509.UnrecognizedExtension(_OID_LEAF, _DER_NULL), critical=False)
            .sign(inter_key, hashes.SHA384())
        )

        self.root_der = root.public_bytes(serialization.Encoding.DER)
        self._x5c = [
            base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
            for cert in (leaf, inter, root)
        ]

    # ── signing ─────────────────────────────────────────────────────────────

    def sign(self, payload: dict) -> str:
        return jwt.encode(payload, self._leaf_key, algorithm="ES256", headers={"x5c": self._x5c})

    def transaction(
        self,
        *,
        original_transaction_id: str = "2000000000000001",
        transaction_id: str | None = None,
        product_id: str = MONTHLY,
        environment: str = "Sandbox",
        bundle_id: str = BUNDLE_ID,
        purchased: datetime | None = None,
        expires: datetime | None = None,
        revoked: datetime | None = None,
        app_account_token: str | None = None,
        free_trial: bool = False,
        type_: str = "Auto-Renewable Subscription",
        signed: datetime | None = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        purchased = purchased or now
        expires = expires or (purchased + timedelta(days=30))
        payload = {
            "transactionId": transaction_id or original_transaction_id,
            "originalTransactionId": original_transaction_id,
            "webOrderLineItemId": "2000000000000099",
            "bundleId": bundle_id,
            "productId": product_id,
            "subscriptionGroupIdentifier": "22429515",
            "purchaseDate": _ms(purchased),
            "originalPurchaseDate": _ms(purchased),
            "expiresDate": _ms(expires),
            "quantity": 1,
            "type": type_,
            "inAppOwnershipType": "PURCHASED",
            "signedDate": _ms(signed or now),
            "environment": environment,
            "transactionReason": "PURCHASE",
            "storefront": "USA",
            "storefrontId": "143441",
            "price": 9990,
            "currency": "USD",
        }
        if app_account_token:
            payload["appAccountToken"] = app_account_token
        if revoked:
            payload["revocationDate"] = _ms(revoked)
            payload["revocationReason"] = 0
        if free_trial:
            payload["offerType"] = 1
            payload["offerDiscountType"] = "FREE_TRIAL"
            payload["price"] = 0
        return self.sign(payload)

    def renewal_info(
        self,
        *,
        original_transaction_id: str = "2000000000000001",
        product_id: str = MONTHLY,
        environment: str = "Sandbox",
        auto_renew: bool = True,
        grace_expires: datetime | None = None,
    ) -> str:
        payload = {
            "originalTransactionId": original_transaction_id,
            "autoRenewProductId": product_id,
            "productId": product_id,
            "autoRenewStatus": 1 if auto_renew else 0,
            "signedDate": _ms(datetime.now(timezone.utc)),
            "environment": environment,
        }
        if grace_expires:
            payload["gracePeriodExpiresDate"] = _ms(grace_expires)
            payload["isInBillingRetryPeriod"] = True
        return self.sign(payload)

    def notification(
        self,
        notification_type: str,
        *,
        subtype: str | None = None,
        transaction: str | None = None,
        renewal: str | None = None,
        environment: str = "Sandbox",
        bundle_id: str = BUNDLE_ID,
    ) -> str:
        data = {
            "appAppleId": APP_APPLE_ID,
            "bundleId": bundle_id,
            "bundleVersion": "5",
            "environment": environment,
        }
        if transaction:
            data["signedTransactionInfo"] = transaction
        if renewal:
            data["signedRenewalInfo"] = renewal
        payload = {
            "notificationType": notification_type,
            "notificationUUID": str(uuid.uuid4()),
            "data": data,
            "version": "2.0",
            "signedDate": int(time.time() * 1000),
        }
        if subtype:
            payload["subtype"] = subtype
        return self.sign(payload)
