"""Manual billing — the customer-facing half.

Peptora is a one-time purchase verified by a human. There is no gateway and
no webhook: a user transfers money, files a claim with a receipt, and an
admin approves it. Everything here is what the user can do; approval lives in
app/routers/admin.py.

The whole flow is reachable *without* access, by definition — a user who
cannot pay cannot be asked to pay first. Every route below therefore requires
a verified session and nothing more.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as env
from app.database import get_db
from app.middleware.auth import get_current_verified_user
from app.middleware.rate_limit import limiter
from app.models import OPEN_CLAIM_STATUSES, AuditLog, PaymentClaim, User
from app.schemas import (
    CreateClaimRequest,
    PaymentClaimResponse,
    PaymentInstructions,
)
from app.utils import storage
from app.utils.settings_store import get_settings

router = APIRouter(prefix="/billing", tags=["billing"])
logger = logging.getLogger("peptora.billing")

# A user with a genuine problem files one claim, maybe two. Anything past this
# in a day is either confusion or abuse, and both are better answered by a
# human than by another row in the queue.
MAX_CLAIMS_PER_DAY = 5


def claim_response(claim: PaymentClaim) -> PaymentClaimResponse:
    return PaymentClaimResponse(
        id=claim.id,
        status=claim.status,
        method=claim.method,
        amount_claimed=float(claim.amount_claimed) if claim.amount_claimed is not None else None,
        currency=claim.currency,
        reference=claim.reference,
        payer_name=claim.payer_name,
        paid_at=claim.paid_at,
        user_note=claim.user_note,
        has_receipt=bool(claim.receipt_key) and claim.receipt_deleted_at is None,
        rejection_reason=claim.rejection_reason,
        review_note=claim.review_note,
        reviewed_at=claim.reviewed_at,
        created_at=claim.created_at,
    )


async def open_claim_for(db: AsyncSession, user_id: uuid.UUID) -> PaymentClaim | None:
    result = await db.execute(
        select(PaymentClaim)
        .where(PaymentClaim.user_id == user_id)
        .where(PaymentClaim.status.in_(OPEN_CLAIM_STATUSES))
        .order_by(PaymentClaim.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def latest_claim_for(db: AsyncSession, user_id: uuid.UUID) -> PaymentClaim | None:
    """The open claim if there is one, otherwise the most recent resolved one.

    The paywall needs both cases: a pending claim renders a status timeline, a
    rejected one renders the reason and a way to correct it.
    """
    result = await db.execute(
        select(PaymentClaim)
        .where(PaymentClaim.user_id == user_id)
        .order_by(PaymentClaim.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.get("/instructions", response_model=PaymentInstructions)
@limiter.limit("30/minute")
async def instructions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_verified_user),
):
    cfg = await get_settings(db)
    return PaymentInstructions(
        price=float(cfg.one_time_price_usd),
        currency=cfg.currency,
        bank_details_md=cfg.bank_details_md,
        payment_instructions_md=cfg.payment_instructions_md,
        support_email=cfg.support_email,
        review_sla_hours=cfg.review_sla_hours,
        manual_payments_enabled=cfg.manual_payments_enabled,
        crypto_payments_enabled=cfg.crypto_payments_enabled and env.payments_configured,
        receipts_enabled=env.receipts_configured or env.is_development,
    )


@router.get("/claims/mine", response_model=list[PaymentClaimResponse])
@limiter.limit("60/minute")
async def my_claims(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_verified_user),
):
    result = await db.execute(
        select(PaymentClaim)
        .where(PaymentClaim.user_id == user.id)
        .order_by(PaymentClaim.created_at.desc())
        .limit(20)
    )
    return [claim_response(c) for c in result.scalars().all()]


@router.post("/claims", response_model=PaymentClaimResponse, status_code=201)
@limiter.limit("10/minute")
async def create_claim(
    request: Request,
    body: CreateClaimRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_verified_user),
):
    cfg = await get_settings(db)
    if not cfg.manual_payments_enabled:
        raise HTTPException(
            status_code=503,
            detail="Payment submissions are closed right now. Please email support.",
        )

    existing = await open_claim_for(db, user.id)
    if existing:
        # 409 rather than silently returning the existing claim: the client
        # should show the one already in flight, not pretend a second was
        # created.
        raise HTTPException(
            status_code=409,
            detail="You already have a payment under review.",
        )

    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    recent = (await db.execute(
        select(func.count())
        .select_from(PaymentClaim)
        .where(PaymentClaim.user_id == user.id)
        .where(PaymentClaim.created_at >= day_ago)
    )).scalar_one()
    if recent >= MAX_CLAIMS_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail="Too many submissions today. Please email support instead.",
        )

    claim = PaymentClaim(
        user_id=user.id,
        method=body.method,
        status="submitted",
        amount_claimed=body.amount_claimed,
        currency=(body.currency or cfg.currency).upper(),
        reference=body.reference,
        payer_name=body.payer_name,
        paid_at=body.paid_at,
        user_note=body.user_note,
    )
    db.add(claim)
    try:
        await db.flush()
    except IntegrityError:
        # Lost the race against the partial unique index — two tabs, two
        # submits. The constraint is the arbiter, not the SELECT above.
        await db.rollback()
        raise HTTPException(status_code=409, detail="You already have a payment under review.")

    db.add(AuditLog(user_id=user.id, action="payment_claim_submitted",
                    extra_data={"claim_id": str(claim.id), "method": claim.method}))
    logger.info("claim_submitted user_id=%s claim_id=%s", user.id, claim.id)

    # Both mails are best-effort. A mail outage must never cost the claim —
    # the row is what matters, and the queue is visible in the admin panel
    # regardless of whether the notification lands.
    try:
        from app.utils.email import send_claim_received_email
        await send_claim_received_email(user.email, user.full_name, cfg.review_sla_hours)
    except Exception:
        logger.exception("claim_received_email_failed to=%s", user.email)

    notify = cfg.claims_notify_email
    if notify:
        try:
            from app.utils.email import send_new_claim_admin_email
            await send_new_claim_admin_email(
                notify, user.email, claim.amount_claimed, claim.currency, claim.reference,
            )
        except Exception:
            logger.exception("new_claim_admin_email_failed to=%s", notify)

    return claim_response(claim)


@router.post("/claims/{claim_id}/receipt", response_model=PaymentClaimResponse)
@limiter.limit("10/minute")
async def upload_receipt(
    request: Request,
    claim_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_verified_user),
    file: UploadFile = File(...),
):
    """Attach proof of payment to an open claim.

    This is the most exposed surface in the manual flow: it accepts a file
    from anyone with an account, paid or not. Validation lives in
    app/utils/storage.py — magic-byte sniffing, an 8 MB ceiling, and
    re-encoding that strips EXIF and destroys polyglots.
    """
    result = await db.execute(
        select(PaymentClaim)
        .where(PaymentClaim.id == claim_id, PaymentClaim.user_id == user.id)
    )
    claim = result.scalar_one_or_none()
    if not claim:
        raise HTTPException(status_code=404, detail="Payment not found")
    if claim.status not in OPEN_CLAIM_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="This payment has already been reviewed. Submit a new one instead.",
        )

    # Read with a hard ceiling rather than trusting Content-Length, which the
    # client controls.
    raw = await file.read(storage.MAX_UPLOAD_BYTES + 1)
    if len(raw) > storage.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That file is larger than 8 MB.")

    try:
        data, mime, ext = storage.normalise_receipt(raw)
    except storage.StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    key = storage.build_key(user.id, claim.id, ext)
    try:
        storage.put(key, data, mime)
    except Exception:
        logger.exception("receipt_upload_failed user_id=%s claim_id=%s", user.id, claim.id)
        raise HTTPException(status_code=502, detail="Could not save the receipt. Please try again.")

    # Replace rather than accumulate: a re-upload means the first one was
    # wrong, and keeping it only costs storage and confuses the reviewer.
    old_key = claim.receipt_key
    claim.receipt_key = key
    claim.receipt_mime = mime
    claim.receipt_bytes = len(data)
    claim.receipt_uploaded_at = datetime.now(timezone.utc)
    claim.receipt_deleted_at = None

    if old_key and old_key != key:
        try:
            storage.delete(old_key)
        except Exception:
            logger.warning("receipt_replace_delete_failed key=%s", old_key)

    logger.info("receipt_uploaded user_id=%s claim_id=%s bytes=%d", user.id, claim.id, len(data))
    return claim_response(claim)


@router.post("/claims/{claim_id}/cancel", response_model=PaymentClaimResponse)
@limiter.limit("10/minute")
async def cancel_claim(
    request: Request,
    claim_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_verified_user),
):
    result = await db.execute(
        select(PaymentClaim)
        .where(PaymentClaim.id == claim_id, PaymentClaim.user_id == user.id)
    )
    claim = result.scalar_one_or_none()
    if not claim:
        raise HTTPException(status_code=404, detail="Payment not found")
    if claim.status not in OPEN_CLAIM_STATUSES:
        raise HTTPException(status_code=409, detail="This payment has already been reviewed.")

    claim.status = "cancelled"
    db.add(AuditLog(user_id=user.id, action="payment_claim_cancelled",
                    extra_data={"claim_id": str(claim.id)}))
    logger.info("claim_cancelled user_id=%s claim_id=%s", user.id, claim.id)
    return claim_response(claim)
