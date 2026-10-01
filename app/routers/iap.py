"""App Store purchases: the iOS app's Peptora Pro subscriptions.

Two entry points, and both end in the same place:

  POST /iap/apple/verify         the app posts the signed transaction StoreKit
                                 gave it after a purchase, a restore, or at
                                 launch.
  POST /iap/apple/notifications  Apple posts renewals, expiries and refunds
                                 here directly (App Store Server Notifications
                                 V2). Set the URL in App Store Connect.

Neither trusts anything it is told. Every payload is a JWS signed by Apple,
verified in app/utils/apple_iap.py before a single field is read. The app
posting "I paid" proves nothing; Apple's signature on the transaction does.

Access itself is not decided here. This module keeps one row per subscription
in `apple_subscriptions` and copies the latest unrevoked expiry onto
`users.apple_sub_until`; has_access() in app/middleware/auth.py reads that
column, so there is still exactly one gate.

The subscription follows the Apple ID, not the Peptora account: whichever
account presents a valid signed transaction becomes its owner. That is what
"Restore Purchases" means, and it is also what keeps a reviewer or a customer
from being stranded when the Apple ID already holds a subscription bought
from a different Peptora account.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_verified_user, has_access
from app.middleware.rate_limit import limiter
from app.models import AppleSubscription, AuditLog, User
from app.schemas import AppleVerifyRequest, AppleVerifyResponse, AppleVerifyResult
from app.utils import apple_iap

router = APIRouter(prefix="/iap", tags=["iap"])
logger = logging.getLogger("peptora.iap")

# Notification types that end access early. Everything else either extends
# the paid period or only changes renewal preferences, and is handled by
# applying the transaction it carries.
_REVOKING_TYPES = {"REFUND", "REVOKE"}
_ENDING_TYPES = {"EXPIRED", "GRACE_PERIOD_EXPIRED"}


async def current_subscription_for(db: AsyncSession, user_id: uuid.UUID) -> AppleSubscription | None:
    """The subscription row that best describes this user's plan: the one
    running longest, ignoring refunded ones."""
    result = await db.execute(
        select(AppleSubscription).where(
            AppleSubscription.user_id == user_id,
            AppleSubscription.revoked_at.is_(None),
        )
    )
    rows = [r for r in result.scalars().all() if r.effective_expires_at is not None]
    if not rows:
        return None
    return max(rows, key=lambda r: r.effective_expires_at)


async def refresh_user_access(db: AsyncSession, user: User) -> None:
    """Recompute `apple_sub_until` from the user's subscription rows.

    Always derived from the rows rather than adjusted in place, so applying
    the same transaction or notification twice lands on the same value.
    """
    current = await current_subscription_for(db, user.id)
    user.apple_sub_until = current.effective_expires_at if current else None
    # `plan` is only a denormalised label for admin lists. has_access() is the
    # gate; this just saves the label from lagging until the nightly sweep.
    now = datetime.now(timezone.utc)
    if user.apple_sub_until and user.apple_sub_until > now and not user.access_revoked_at:
        user.plan = "pro"
    elif not has_access(user):
        user.plan = "free"


def _status_of(tx: apple_iap.AppleTransaction, now: datetime) -> str:
    if tx.revoked_at is not None:
        return "revoked"
    return "active" if tx.is_active(now) else "expired"


def _apply_fields(row: AppleSubscription, tx: apple_iap.AppleTransaction) -> None:
    """Move a row forward to the state a verified transaction describes.

    Forward only. Transactions and notifications can arrive out of order, and
    a client can replay an old one, so an older period never overwrites a
    newer one.
    """
    is_newer = row.expires_at is None or (tx.expires_at is not None and tx.expires_at > row.expires_at)
    is_same_period = row.latest_transaction_id == tx.transaction_id

    if tx.revoked_at is not None:
        # A refund only ends access when it is for the period we believe is
        # current. A refund of some earlier month does not cancel this one.
        if is_newer or is_same_period:
            row.revoked_at = tx.revoked_at
        if not is_newer:
            return
    elif is_newer and row.revoked_at is not None:
        # A later, unrefunded period: the customer subscribed again after the
        # refund. Deliberately NOT cleared for the same period, or replaying
        # the pre-refund transaction would undo the refund.
        row.revoked_at = None

    if is_newer:
        row.latest_transaction_id = tx.transaction_id
        row.product_id = tx.product_id
        row.environment = tx.environment
        row.purchased_at = tx.purchased_at
        row.expires_at = tx.expires_at
        row.is_trial_period = tx.is_trial_period
        row.raw = tx.raw

    if tx.app_account_token and not row.app_account_token:
        row.app_account_token = tx.app_account_token


async def _user_for_token(db: AsyncSession, token: str | None) -> User | None:
    """The account a purchase was started from, read from appAccountToken."""
    if not token:
        return None
    try:
        user_id = uuid.UUID(token)
    except ValueError:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def _load_row(db: AsyncSession, original_transaction_id: str) -> AppleSubscription | None:
    result = await db.execute(
        select(AppleSubscription)
        .where(AppleSubscription.original_transaction_id == original_transaction_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def _upsert_row(
    db: AsyncSession, tx: apple_iap.AppleTransaction, owner_id: uuid.UUID | None
) -> AppleSubscription:
    """Find or create the row for this subscription.

    The unique constraint on original_transaction_id is the arbiter for the
    create: the app's verify call and Apple's notification for the same
    purchase routinely arrive within milliseconds of each other.
    """
    row = await _load_row(db, tx.original_transaction_id)
    if row is not None:
        return row
    try:
        # A SAVEPOINT, so losing the race rolls back only this insert and not
        # the rest of the request's work.
        async with db.begin_nested():
            row = AppleSubscription(
                user_id=owner_id,
                original_transaction_id=tx.original_transaction_id,
                product_id=tx.product_id,
                environment=tx.environment,
                app_account_token=tx.app_account_token,
            )
            db.add(row)
            await db.flush()
        return row
    except IntegrityError:
        row = await _load_row(db, tx.original_transaction_id)
        if row is None:
            raise
        return row


async def _apply_for_user(
    db: AsyncSession, tx: apple_iap.AppleTransaction, user: User, request: Request
) -> None:
    """Record a verified transaction and make `user` its owner."""
    row = await _upsert_row(db, tx, user.id)

    previous_owner_id = row.user_id
    if previous_owner_id != user.id:
        row.user_id = user.id
        if previous_owner_id is not None:
            db.add(AuditLog(
                user_id=user.id,
                action="apple_subscription_transferred",
                extra_data={"original_transaction_id": row.original_transaction_id},
                platform=request.headers.get("X-Platform", "ios"),
            ))
            logger.info(
                "apple_subscription_transferred otid=%s to=%s",
                row.original_transaction_id, user.id,
            )

    _apply_fields(row, tx)
    await db.flush()

    if previous_owner_id is not None and previous_owner_id != user.id:
        previous = (await db.execute(select(User).where(User.id == previous_owner_id))).scalar_one_or_none()
        if previous is not None:
            await refresh_user_access(db, previous)


@router.post("/apple/verify", response_model=AppleVerifyResponse)
@limiter.limit("30/minute")
async def verify_apple_transactions(
    request: Request,
    body: AppleVerifyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_verified_user),
):
    """Verify signed transactions from the app and grant what they prove.

    Called after a purchase, after Restore Purchases, and quietly at launch so
    a renewal that happened while the app was closed is picked up even if
    Apple's server notification never arrived.

    Idempotent: posting the same transaction again changes nothing.
    """
    from app.routers.billing import latest_claim_for
    from app.routers.subscriptions import access_info

    now = datetime.now(timezone.utc)
    had_access = has_access(user)
    results: list[AppleVerifyResult] = []
    applied = 0

    for signed in body.transactions:
        try:
            # Signature and certificate-chain checks are CPU work in a
            # synchronous library; keep them off the event loop.
            tx = await asyncio.to_thread(apple_iap.verify_transaction, signed)
        except apple_iap.AppleVerificationError as exc:
            logger.warning("apple_verify_rejected user_id=%s reason=%s", user.id, exc.reason)
            results.append(AppleVerifyResult(status="invalid"))
            continue

        await _apply_for_user(db, tx, user, request)
        applied += 1
        results.append(AppleVerifyResult(
            status=_status_of(tx, now),
            product_id=tx.product_id,
            expires_at=tx.expires_at,
            transaction_id=tx.transaction_id,
        ))

    if applied:
        await refresh_user_access(db, user)
        if has_access(user) and not had_access:
            db.add(AuditLog(
                user_id=user.id,
                action="apple_subscription_activated",
                platform=request.headers.get("X-Platform", "ios"),
            ))
        logger.info(
            "apple_verify user_id=%s applied=%d until=%s",
            user.id, applied, user.apple_sub_until.isoformat() if user.apple_sub_until else None,
        )

    claim = await latest_claim_for(db, user.id)
    subscription = await current_subscription_for(db, user.id)
    return AppleVerifyResponse(access=access_info(user, claim, subscription), results=results)


@router.post("/apple/notifications")
@limiter.limit("600/minute")
async def apple_server_notification(request: Request, db: AsyncSession = Depends(get_db)):
    """App Store Server Notifications, version 2.

    Unauthenticated and internet-reachable, so Apple's signature is the only
    thing standing between a stranger and free access. A payload that fails
    verification gets a 400. Everything genuine gets a 200 even when there is
    nothing to do with it: Apple retries non-2xx responses for three days, and
    retrying a notification we have deliberately ignored achieves nothing.
    """
    try:
        payload = await request.json()
        signed = payload.get("signedPayload") if isinstance(payload, dict) else None
    except Exception:
        signed = None
    if not isinstance(signed, str) or not signed:
        return Response(status_code=400)

    try:
        note = await asyncio.to_thread(apple_iap.verify_notification, signed)
    except apple_iap.AppleVerificationError as exc:
        if exc.reason in ("sandbox_not_allowed", "environment_not_allowed"):
            # Not forged, just from an environment this deployment has been
            # told not to accept. Acknowledge so Apple stops resending it.
            logger.info("apple_notification_skipped reason=%s", exc.reason)
            return Response(status_code=200)
        logger.warning("apple_notification_rejected reason=%s bytes=%d", exc.reason, len(signed))
        return Response(status_code=400)

    tx = note.transaction
    if tx is None:
        # TEST pings, summaries and types that carry no transaction.
        logger.info("apple_notification type=%s subtype=%s (no transaction)", note.notification_type, note.subtype)
        return Response(status_code=200)

    now = datetime.now(timezone.utc)
    row = await _upsert_row(db, tx, None)
    if row.user_id is None:
        # Apple got here before the app did, or the app never managed to post
        # the transaction at all. Link by the account the purchase was started
        # from; if that account is gone, the row stays unowned so a later
        # restore from the same Apple ID can pick it up.
        owner = await _user_for_token(db, tx.app_account_token or row.app_account_token)
        if owner is not None:
            row.user_id = owner.id

    _apply_fields(row, tx)

    kind = note.notification_type
    if kind in _REVOKING_TYPES and row.revoked_at is None and row.latest_transaction_id == tx.transaction_id:
        # Apple normally stamps revocationDate on the transaction itself; this
        # covers a refund notification that arrives without one.
        row.revoked_at = now
    elif kind == "REFUND_REVERSED" and row.latest_transaction_id == tx.transaction_id:
        row.revoked_at = None

    if kind == "DID_FAIL_TO_RENEW":
        # Present only when Billing Grace Period is enabled in App Store
        # Connect. Without it the paid period simply runs out.
        row.grace_expires_at = note.grace_expires_at
    elif kind == "DID_RENEW" or kind in _ENDING_TYPES:
        row.grace_expires_at = None

    if note.auto_renew is not None:
        row.auto_renew = note.auto_renew
    row.last_notification_type = f"{kind}:{note.subtype}" if note.subtype else kind
    row.last_notification_at = now
    await db.flush()

    if row.user_id is not None:
        owner = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
        if owner is not None:
            await refresh_user_access(db, owner)
            db.add(AuditLog(
                user_id=owner.id,
                action=f"apple_{kind.lower()}"[:100],
                extra_data={"subtype": note.subtype, "product_id": tx.product_id},
                platform="ios",
            ))

    logger.info(
        "apple_notification type=%s subtype=%s otid=%s env=%s owner=%s",
        kind, note.subtype, tx.original_transaction_id, note.environment, row.user_id,
    )
    return Response(status_code=200)
