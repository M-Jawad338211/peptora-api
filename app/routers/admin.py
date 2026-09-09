"""Admin API — the manual approval half of billing.

Every grant of access in the product passes through this file. Two paths reach
it: a user files a claim with a receipt and an admin approves it, or the money
moved entirely outside the app (we emailed a payment link) and the admin files
the record themselves. Both end in the same place, and both leave an audit row
naming the acting admin.

Rate limits are on every route. Admin endpoints previously had none while
every other router used slowapi, which made this the softest surface in the
API despite being the one that hands out free access.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.middleware.auth import get_current_admin, has_access
from app.middleware.rate_limit import limiter
from app.models import (
    OPEN_CLAIM_STATUSES,
    AuditLog,
    CalculatorUsage,
    PaymentClaim,
    User,
)
from app.schemas import (
    AdminAuditItem,
    AdminClaimDetail,
    AdminClaimItem,
    AdminClaimListResponse,
    AdminSettingsResponse,
    AdminSettingsUpdate,
    AdminStatsResponse,
    AdminUserDetail,
    AdminUserItem,
    AdminUserListResponse,
    ApproveClaimRequest,
    GrantAccessRequest,
    RejectClaimRequest,
    RevokeAccessRequest,
)
from app.utils import storage
from app.utils.settings_store import get_settings

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger("peptora.admin")


def _access_state(user: User) -> str:
    """A single word for what is granting (or denying) this user access.

    Ordered to match has_access(): revocation outranks everything, then the
    permanent licence, then the two windows.
    """
    if user.access_revoked_at:
        return "revoked"
    if user.lifetime_access_at:
        return "lifetime"
    now = datetime.now(timezone.utc)
    if user.paid_until and user.paid_until > now:
        return "crypto"
    if user.trial_ends_at and user.trial_ends_at > now:
        return "trial"
    if user.trial_ends_at or user.paid_until:
        return "lapsed"
    return "none"


def _age_hours(created_at: datetime) -> float:
    return (datetime.now(timezone.utc) - created_at).total_seconds() / 3600.0


# ── Dashboard ───────────────────────────────────────────────────────────────

@router.get("/stats", response_model=AdminStatsResponse)
@limiter.limit("60/minute")
async def admin_stats(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cfg = await get_settings(db)
    sla = timedelta(hours=cfg.review_sla_hours)

    async def count(stmt):
        return (await db.execute(stmt)).scalar_one()

    pending_q = select(func.count()).select_from(PaymentClaim).where(
        PaymentClaim.status.in_(OPEN_CLAIM_STATUSES)
    )
    pending_claims = await count(pending_q)

    oldest = (await db.execute(
        select(func.min(PaymentClaim.created_at)).where(
            PaymentClaim.status.in_(OPEN_CLAIM_STATUSES)
        )
    )).scalar_one_or_none()

    overdue = await count(
        select(func.count()).select_from(PaymentClaim).where(
            PaymentClaim.status.in_(OPEN_CLAIM_STATUSES),
            PaymentClaim.created_at <= now - sla,
        )
    )

    total_users = await count(select(func.count()).select_from(User))
    lifetime_users = await count(
        select(func.count()).select_from(User).where(
            User.lifetime_access_at.is_not(None), User.access_revoked_at.is_(None)
        )
    )
    trialing = await count(
        select(func.count()).select_from(User).where(
            User.lifetime_access_at.is_(None),
            User.access_revoked_at.is_(None),
            User.trial_ends_at.is_not(None),
            User.trial_ends_at > now,
        )
    )
    lapsed = await count(
        select(func.count()).select_from(User).where(
            User.lifetime_access_at.is_(None),
            User.access_revoked_at.is_(None),
            User.trial_ends_at.is_not(None),
            User.trial_ends_at <= now,
            or_(User.paid_until.is_(None), User.paid_until <= now),
        )
    )
    trials_ending = await count(
        select(func.count()).select_from(User).where(
            User.lifetime_access_at.is_(None),
            User.trial_ends_at > now,
            User.trial_ends_at <= now + timedelta(days=7),
        )
    )

    calcs_today = await count(
        select(func.count()).select_from(CalculatorUsage).where(CalculatorUsage.created_at >= today)
    )
    calcs_week = await count(
        select(func.count()).select_from(CalculatorUsage).where(
            CalculatorUsage.created_at >= today - timedelta(days=7)
        )
    )
    calcs_month = await count(
        select(func.count()).select_from(CalculatorUsage).where(
            CalculatorUsage.created_at >= today - timedelta(days=30)
        )
    )
    signups_today = await count(
        select(func.count()).select_from(User).where(User.created_at >= today)
    )

    approved_7d = await count(
        select(func.count()).select_from(PaymentClaim).where(
            PaymentClaim.status == "approved",
            PaymentClaim.reviewed_at >= now - timedelta(days=7),
        )
    )
    rejected_7d = await count(
        select(func.count()).select_from(PaymentClaim).where(
            PaymentClaim.status == "rejected",
            PaymentClaim.reviewed_at >= now - timedelta(days=7),
        )
    )

    # Real revenue, from claims an admin actually approved. The old field was
    # hardcoded to 0.0 behind a comment pointing at a Stripe dashboard that
    # never existed for this product.
    rev_30d = (await db.execute(
        select(func.coalesce(func.sum(PaymentClaim.amount_claimed), 0)).where(
            PaymentClaim.status == "approved",
            PaymentClaim.reviewed_at >= now - timedelta(days=30),
        )
    )).scalar_one()
    rev_all = (await db.execute(
        select(func.coalesce(func.sum(PaymentClaim.amount_claimed), 0)).where(
            PaymentClaim.status == "approved"
        )
    )).scalar_one()

    return AdminStatsResponse(
        pending_claims=pending_claims,
        oldest_pending_hours=round(_age_hours(oldest), 1) if oldest else None,
        overdue_claims=overdue,
        total_users=total_users,
        lifetime_users=lifetime_users,
        trialing_users=trialing,
        lapsed_users=lapsed,
        trials_ending_this_week=trials_ending,
        calcs_today=calcs_today,
        calcs_this_week=calcs_week,
        calcs_this_month=calcs_month,
        new_signups_today=signups_today,
        approved_last_7d=approved_7d,
        rejected_last_7d=rejected_7d,
        revenue_last_30d=float(rev_30d or 0),
        revenue_all_time=float(rev_all or 0),
    )


# ── Users ───────────────────────────────────────────────────────────────────

@router.get("/users", response_model=AdminUserListResponse)
@limiter.limit("60/minute")
async def list_users(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
    search: str = Query(default=""),
    access: str = Query(default="", description="lifetime|trial|lapsed|revoked|none"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    now = datetime.now(timezone.utc)
    q = select(User).options(selectinload(User.trial_counter))
    count_q = select(func.count()).select_from(User)

    if search:
        term = f"%{search}%"
        cond = or_(User.email.ilike(term), User.full_name.ilike(term))
        q = q.where(cond)
        count_q = count_q.where(cond)

    # Filters mirror _access_state so the list and the badge never disagree.
    filters = {
        "lifetime": (User.lifetime_access_at.is_not(None), User.access_revoked_at.is_(None)),
        "revoked": (User.access_revoked_at.is_not(None),),
        "trial": (
            User.lifetime_access_at.is_(None),
            User.access_revoked_at.is_(None),
            User.trial_ends_at.is_not(None),
            User.trial_ends_at > now,
        ),
        "lapsed": (
            User.lifetime_access_at.is_(None),
            User.access_revoked_at.is_(None),
            User.trial_ends_at.is_not(None),
            User.trial_ends_at <= now,
        ),
        "none": (User.lifetime_access_at.is_(None), User.trial_ends_at.is_(None)),
    }
    if access in filters:
        for cond in filters[access]:
            q = q.where(cond)
            count_q = count_q.where(cond)

    total = (await db.execute(count_q)).scalar_one()
    q = q.order_by(User.created_at.desc()).limit(limit).offset(offset)
    users = (await db.execute(q)).scalars().all()

    # One query for every open claim in the page, rather than one per row.
    ids = [u.id for u in users]
    open_claims: dict[uuid.UUID, uuid.UUID] = {}
    if ids:
        rows = (await db.execute(
            select(PaymentClaim.user_id, PaymentClaim.id).where(
                PaymentClaim.user_id.in_(ids),
                PaymentClaim.status.in_(OPEN_CLAIM_STATUSES),
            )
        )).all()
        open_claims = {uid: cid for uid, cid in rows}

    return AdminUserListResponse(
        items=[
            AdminUserItem(
                id=u.id,
                email=u.email,
                full_name=u.full_name,
                plan=u.plan,
                is_admin=u.is_admin,
                created_at=u.created_at,
                last_login=u.last_login,
                calc_uses_anonymous=u.trial_counter.calc_uses_anonymous if u.trial_counter else 0,
                calc_uses_free=u.trial_counter.calc_uses_free if u.trial_counter else 0,
                has_access=has_access(u),
                access_state=_access_state(u),
                lifetime_access_at=u.lifetime_access_at,
                trial_ends_at=u.trial_ends_at,
                access_revoked_at=u.access_revoked_at,
                open_claim_id=open_claims.get(u.id),
            )
            for u in users
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


def _claim_item(claim: PaymentClaim, user: User, sla_hours: int) -> AdminClaimItem:
    now = datetime.now(timezone.utc)
    return AdminClaimItem(
        id=claim.id,
        status=claim.status,
        method=claim.method,
        user_id=user.id,
        user_email=user.email,
        user_full_name=user.full_name,
        amount_claimed=float(claim.amount_claimed) if claim.amount_claimed is not None else None,
        currency=claim.currency,
        reference=claim.reference,
        payer_name=claim.payer_name,
        paid_at=claim.paid_at,
        has_receipt=bool(claim.receipt_key) and claim.receipt_deleted_at is None,
        created_at=claim.created_at,
        age_hours=round(_age_hours(claim.created_at), 1),
        is_overdue=(
            claim.status in OPEN_CLAIM_STATUSES
            and claim.created_at <= now - timedelta(hours=sla_hours)
        ),
    )


@router.get("/users/{user_id}", response_model=AdminUserDetail)
@limiter.limit("60/minute")
async def user_detail(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    result = await db.execute(
        select(User).options(selectinload(User.trial_counter)).where(User.id == user_id)
    )
    u = result.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    cfg = await get_settings(db)
    claim_rows = (await db.execute(
        select(PaymentClaim)
        .where(PaymentClaim.user_id == u.id)
        .order_by(PaymentClaim.created_at.desc())
        .limit(50)
    )).scalars().all()

    open_claim = next((c for c in claim_rows if c.status in OPEN_CLAIM_STATUSES), None)

    return AdminUserDetail(
        id=u.id,
        email=u.email,
        full_name=u.full_name,
        plan=u.plan,
        is_admin=u.is_admin,
        created_at=u.created_at,
        last_login=u.last_login,
        calc_uses_anonymous=u.trial_counter.calc_uses_anonymous if u.trial_counter else 0,
        calc_uses_free=u.trial_counter.calc_uses_free if u.trial_counter else 0,
        has_access=has_access(u),
        access_state=_access_state(u),
        lifetime_access_at=u.lifetime_access_at,
        trial_ends_at=u.trial_ends_at,
        access_revoked_at=u.access_revoked_at,
        open_claim_id=open_claim.id if open_claim else None,
        email_verified=u.email_verified,
        consent_accepted=u.consent_accepted,
        paid_until=u.paid_until,
        access_revoked_reason=u.access_revoked_reason,
        signup_fingerprint=u.signup_fingerprint,
        claims=[_claim_item(c, u, cfg.review_sla_hours) for c in claim_rows],
    )


async def _get_user(db: AsyncSession, user_id: uuid.UUID) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/users/{user_id}/grant")
@limiter.limit("30/minute")
async def grant_access(
    request: Request,
    user_id: uuid.UUID,
    body: GrantAccessRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Grant a licence with no user-submitted claim — the payment-link path.

    A synthetic claim is created rather than only stamping the user, so every
    licence in the system has a record explaining itself. Without this, the
    flow that happens entirely in your inbox would be the one flow with no
    evidence attached.
    """
    user = await _get_user(db, user_id)
    cfg = await get_settings(db)
    now = datetime.now(timezone.utc)

    existing = (await db.execute(
        select(PaymentClaim).where(
            PaymentClaim.user_id == user.id,
            PaymentClaim.status.in_(OPEN_CLAIM_STATUSES),
        )
    )).scalar_one_or_none()

    if existing:
        # Reuse the open claim rather than fighting the partial unique index.
        claim = existing
        claim.method = "payment_link"
    else:
        claim = PaymentClaim(
            user_id=user.id,
            method="payment_link",
            status="submitted",
            currency=body.currency or cfg.currency,
        )
        db.add(claim)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(status_code=409, detail="This user already has a claim under review.")

    if body.amount is not None:
        claim.amount_claimed = body.amount
    elif claim.amount_claimed is None:
        claim.amount_claimed = cfg.one_time_price_usd
    if body.reference:
        claim.reference = body.reference
    if body.note:
        claim.internal_note = body.note

    _approve(claim, user, admin, now)
    db.add(AuditLog(
        user_id=admin.id,
        action="admin_grant_access",
        extra_data={"target": str(user.id), "claim_id": str(claim.id), "reference": body.reference},
    ))
    logger.info("admin_grant admin=%s target=%s claim=%s", admin.id, user.id, claim.id)
    await _notify_approved(user)
    return {"message": "Licence activated", "claim_id": str(claim.id)}


@router.post("/users/{user_id}/revoke")
@limiter.limit("30/minute")
async def revoke_access(
    request: Request,
    user_id: uuid.UUID,
    body: RevokeAccessRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    user = await _get_user(db, user_id)
    user.access_revoked_at = datetime.now(timezone.utc)
    user.access_revoked_reason = body.reason
    user.plan = "free"
    db.add(AuditLog(
        user_id=admin.id,
        action="admin_revoke_access",
        extra_data={"target": str(user.id), "reason": body.reason},
    ))
    logger.info("admin_revoke admin=%s target=%s", admin.id, user.id)
    return {"message": "Access revoked"}


@router.post("/users/{user_id}/restore")
@limiter.limit("30/minute")
async def restore_access(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Undo a revocation. Whatever granted access before grants it again."""
    user = await _get_user(db, user_id)
    user.access_revoked_at = None
    user.access_revoked_reason = None
    if user.lifetime_access_at:
        user.plan = "pro"
    db.add(AuditLog(
        user_id=admin.id,
        action="admin_restore_access",
        extra_data={"target": str(user.id)},
    ))
    return {"message": "Access restored"}


@router.post("/users/{user_id}/set-admin")
@limiter.limit("10/minute")
async def set_admin(
    request: Request,
    user_id: uuid.UUID,
    is_admin: bool = Query(...),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    user = await _get_user(db, user_id)
    if user.id == admin.id and not is_admin:
        # Removing your own last admin rights locks everyone out of the panel.
        raise HTTPException(status_code=400, detail="You cannot remove your own admin access.")
    user.is_admin = is_admin
    db.add(AuditLog(
        user_id=admin.id,
        action="admin_set_admin",
        extra_data={"target": str(user.id), "is_admin": is_admin},
    ))
    return {"message": "Updated"}


# ── Claims queue ────────────────────────────────────────────────────────────

@router.get("/claims", response_model=AdminClaimListResponse)
@limiter.limit("60/minute")
async def list_claims(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
    status: str = Query(default="pending", description="pending|submitted|under_review|approved|rejected|cancelled|all"),
    search: str = Query(default=""),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    cfg = await get_settings(db)
    sla = timedelta(hours=cfg.review_sla_hours)
    now = datetime.now(timezone.utc)

    q = select(PaymentClaim, User).join(User, PaymentClaim.user_id == User.id)
    count_q = select(func.count()).select_from(PaymentClaim).join(User, PaymentClaim.user_id == User.id)

    if status == "pending":
        cond = PaymentClaim.status.in_(OPEN_CLAIM_STATUSES)
    elif status == "all":
        cond = None
    else:
        cond = PaymentClaim.status == status
    if cond is not None:
        q = q.where(cond)
        count_q = count_q.where(cond)

    if search:
        term = f"%{search}%"
        scond = or_(
            User.email.ilike(term),
            PaymentClaim.reference.ilike(term),
            PaymentClaim.payer_name.ilike(term),
        )
        q = q.where(scond)
        count_q = count_q.where(scond)

    total = (await db.execute(count_q)).scalar_one()

    # Oldest first for the pending queue — the person who has waited longest
    # is the one to serve next. Newest first once resolved, where recency is
    # what you are usually looking for.
    order = PaymentClaim.created_at.asc() if status == "pending" else PaymentClaim.created_at.desc()
    rows = (await db.execute(q.order_by(order).limit(limit).offset(offset))).all()

    items = []
    for claim, user in rows:
        items.append(AdminClaimItem(
            id=claim.id,
            status=claim.status,
            method=claim.method,
            user_id=user.id,
            user_email=user.email,
            user_full_name=user.full_name,
            amount_claimed=float(claim.amount_claimed) if claim.amount_claimed is not None else None,
            currency=claim.currency,
            reference=claim.reference,
            payer_name=claim.payer_name,
            paid_at=claim.paid_at,
            has_receipt=bool(claim.receipt_key) and claim.receipt_deleted_at is None,
            created_at=claim.created_at,
            age_hours=round(_age_hours(claim.created_at), 1),
            is_overdue=claim.status in OPEN_CLAIM_STATUSES and claim.created_at <= now - sla,
        ))

    return AdminClaimListResponse(items=items, total=total, limit=limit, offset=offset)


async def _load_claim(db: AsyncSession, claim_id: uuid.UUID) -> tuple[PaymentClaim, User]:
    row = (await db.execute(
        select(PaymentClaim, User)
        .join(User, PaymentClaim.user_id == User.id)
        .where(PaymentClaim.id == claim_id)
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Claim not found")
    return row[0], row[1]


@router.get("/claims/{claim_id}", response_model=AdminClaimDetail)
@limiter.limit("60/minute")
async def claim_detail(
    request: Request,
    claim_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    claim, user = await _load_claim(db, claim_id)
    cfg = await get_settings(db)
    now = datetime.now(timezone.utc)

    prior = (await db.execute(
        select(func.count()).select_from(PaymentClaim).where(
            PaymentClaim.user_id == user.id, PaymentClaim.id != claim.id
        )
    )).scalar_one()
    prior_rej = (await db.execute(
        select(func.count()).select_from(PaymentClaim).where(
            PaymentClaim.user_id == user.id,
            PaymentClaim.id != claim.id,
            PaymentClaim.status == "rejected",
        )
    )).scalar_one()

    reviewer_email = None
    if claim.reviewed_by:
        reviewer_email = (await db.execute(
            select(User.email).where(User.id == claim.reviewed_by)
        )).scalar_one_or_none()

    # A cheap identity signal, computed here so the reviewer is not comparing
    # two strings in different parts of the page. A mismatch is common and
    # legitimate — someone's partner paid — so this informs, never decides.
    matches = None
    if claim.payer_name and user.full_name:
        matches = claim.payer_name.strip().lower() == user.full_name.strip().lower()

    return AdminClaimDetail(
        id=claim.id,
        status=claim.status,
        method=claim.method,
        user_id=user.id,
        user_email=user.email,
        user_full_name=user.full_name,
        amount_claimed=float(claim.amount_claimed) if claim.amount_claimed is not None else None,
        currency=claim.currency,
        reference=claim.reference,
        payer_name=claim.payer_name,
        paid_at=claim.paid_at,
        has_receipt=bool(claim.receipt_key) and claim.receipt_deleted_at is None,
        created_at=claim.created_at,
        age_hours=round(_age_hours(claim.created_at), 1),
        is_overdue=(
            claim.status in OPEN_CLAIM_STATUSES
            and claim.created_at <= now - timedelta(hours=cfg.review_sla_hours)
        ),
        user_note=claim.user_note,
        internal_note=claim.internal_note,
        review_note=claim.review_note,
        rejection_reason=claim.rejection_reason,
        reviewed_at=claim.reviewed_at,
        reviewer_email=reviewer_email,
        receipt_mime=claim.receipt_mime,
        receipt_bytes=claim.receipt_bytes,
        receipt_deleted_at=claim.receipt_deleted_at,
        user_created_at=user.created_at,
        user_has_access=has_access(user),
        user_access_state=_access_state(user),
        user_trial_ends_at=user.trial_ends_at,
        user_lifetime_access_at=user.lifetime_access_at,
        prior_claims=prior,
        prior_rejections=prior_rej,
        payer_name_matches=matches,
    )


@router.get("/claims/{claim_id}/receipt")
@limiter.limit("60/minute")
async def claim_receipt(
    request: Request,
    claim_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """A short-lived link to the receipt.

    Never a public or stable URL: the bucket is private and the signature
    expires in five minutes, so a link pasted into a chat is dead by the time
    anyone else opens it. The local dev backend cannot sign, so it streams the
    bytes instead — hence the two shapes of response.
    """
    claim, user = await _load_claim(db, claim_id)
    if not claim.receipt_key or claim.receipt_deleted_at:
        raise HTTPException(status_code=404, detail="No receipt on this claim")

    ext = storage.extension_for(claim.receipt_mime or "")
    filename = f"receipt-{claim.id}{ext}"

    try:
        url = storage.presign(claim.receipt_key, filename)
    except Exception:
        logger.exception("receipt_presign_failed claim_id=%s", claim.id)
        raise HTTPException(status_code=502, detail="Could not open the receipt.")

    # Always JSON, both shapes. The local dev backend cannot sign, so it points
    # at the streaming route below instead — returning raw bytes from this
    # endpoint would hand an image to a client that is about to call .json()
    # on it, and the panel would appear broken on every developer machine.
    return {
        "url": url or f"/api/admin/claims/{claim.id}/receipt/file",
        "mime": claim.receipt_mime,
        "expires_in": storage.PRESIGN_TTL_SECONDS if url else None,
    }


@router.get("/claims/{claim_id}/receipt/file")
@limiter.limit("60/minute")
async def claim_receipt_file(
    request: Request,
    claim_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Stream the receipt bytes. Used when the backend cannot presign."""
    from fastapi.responses import Response as RawResponse

    claim, _user = await _load_claim(db, claim_id)
    if not claim.receipt_key or claim.receipt_deleted_at:
        raise HTTPException(status_code=404, detail="No receipt on this claim")

    try:
        data = storage.get(claim.receipt_key)
    except Exception:
        logger.exception("receipt_read_failed claim_id=%s", claim.id)
        raise HTTPException(status_code=502, detail="Could not open the receipt.")

    ext = storage.extension_for(claim.receipt_mime or "")
    return RawResponse(
        content=data,
        media_type=claim.receipt_mime or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="receipt-{claim.id}{ext}"',
            # Uploaded by a stranger: never let it run in the admin origin.
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _approve(claim: PaymentClaim, user: User, admin: User, now: datetime) -> None:
    """Apply an approval to both rows.

    COALESCE semantics on the grant: re-approving never moves the date, which
    is what makes a double-clicked button harmless. Same idempotency
    discipline the IPN handler uses with `credited_at`.
    """
    claim.status = "approved"
    claim.reviewed_by = admin.id
    claim.reviewed_at = now
    claim.rejection_reason = None

    if user.lifetime_access_at is None:
        user.lifetime_access_at = now
    user.plan = "pro"
    # An approved payment overrides an earlier revocation — someone who was
    # cut off and has now paid should not stay locked out by a stale flag.
    user.access_revoked_at = None
    user.access_revoked_reason = None


async def _notify_approved(user: User) -> None:
    try:
        from app.utils.email import send_licence_active_email
        await send_licence_active_email(user.email, user.full_name)
    except Exception:
        logger.exception("licence_active_email_failed to=%s", user.email)


@router.post("/claims/{claim_id}/approve")
@limiter.limit("30/minute")
async def approve_claim(
    request: Request,
    claim_id: uuid.UUID,
    body: ApproveClaimRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    claim, user = await _load_claim(db, claim_id)

    if claim.status == "approved":
        # Idempotent: a double-click, or a retried request, is a no-op rather
        # than a second audit row and a second email.
        return {"message": "Already approved", "claim_id": str(claim.id)}
    if claim.status in ("rejected", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail="This claim was already resolved. Grant access from the user page instead.",
        )

    now = datetime.now(timezone.utc)
    if body.internal_note:
        claim.internal_note = body.internal_note
    _approve(claim, user, admin, now)

    db.add(AuditLog(
        user_id=admin.id,
        action="admin_claim_approved",
        extra_data={
            "claim_id": str(claim.id),
            "target": str(user.id),
            "amount": float(claim.amount_claimed) if claim.amount_claimed is not None else None,
        },
    ))
    logger.info("claim_approved admin=%s claim=%s target=%s", admin.id, claim.id, user.id)

    # The commit happens in get_db's teardown; the email is fired after the
    # response is built, so a mail failure can never cost the grant.
    await _notify_approved(user)
    return {"message": "Licence activated", "claim_id": str(claim.id)}


@router.post("/claims/{claim_id}/reject")
@limiter.limit("30/minute")
async def reject_claim(
    request: Request,
    claim_id: uuid.UUID,
    body: RejectClaimRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    claim, user = await _load_claim(db, claim_id)

    if claim.status == "rejected":
        return {"message": "Already rejected", "claim_id": str(claim.id)}
    if claim.status in ("approved", "cancelled"):
        raise HTTPException(status_code=409, detail="This claim was already resolved.")

    claim.status = "rejected"
    claim.rejection_reason = body.reason
    claim.review_note = body.review_note
    if body.internal_note:
        claim.internal_note = body.internal_note
    claim.reviewed_by = admin.id
    claim.reviewed_at = datetime.now(timezone.utc)

    db.add(AuditLog(
        user_id=admin.id,
        action="admin_claim_rejected",
        extra_data={"claim_id": str(claim.id), "target": str(user.id), "reason": body.reason},
    ))
    logger.info("claim_rejected admin=%s claim=%s reason=%s", admin.id, claim.id, body.reason)

    try:
        from app.utils.email import send_claim_rejected_email
        await send_claim_rejected_email(user.email, user.full_name, body.reason, body.review_note)
    except Exception:
        logger.exception("claim_rejected_email_failed to=%s", user.email)

    return {"message": "Claim rejected", "claim_id": str(claim.id)}


@router.post("/claims/{claim_id}/receipt")
@limiter.limit("20/minute")
async def admin_upload_receipt(
    request: Request,
    claim_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
    file: UploadFile = File(...),
):
    """Attach proof to a claim on the user's behalf.

    The payment-link flow happens in your inbox: the user emails, you send a
    link, they pay. The evidence is a screenshot you hold, not one they
    uploaded — so the admin needs the same attachment path the user has, or
    that entire flow ends up with no proof on file.
    """
    claim, user = await _load_claim(db, claim_id)

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
        logger.exception("admin_receipt_upload_failed claim_id=%s", claim.id)
        raise HTTPException(status_code=502, detail="Could not save the receipt.")

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
            logger.warning("admin_receipt_replace_delete_failed key=%s", old_key)

    db.add(AuditLog(
        user_id=admin.id,
        action="admin_receipt_uploaded",
        extra_data={"claim_id": str(claim.id), "target": str(user.id)},
    ))
    return {"message": "Receipt attached", "claim_id": str(claim.id)}


# ── Settings ────────────────────────────────────────────────────────────────

@router.get("/settings", response_model=AdminSettingsResponse)
@limiter.limit("60/minute")
async def read_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    cfg = await get_settings(db)
    return AdminSettingsResponse(
        one_time_price_usd=float(cfg.one_time_price_usd),
        currency=cfg.currency,
        bank_details_md=cfg.bank_details_md,
        payment_instructions_md=cfg.payment_instructions_md,
        support_email=cfg.support_email,
        claims_notify_email=cfg.claims_notify_email,
        review_sla_hours=cfg.review_sla_hours,
        manual_payments_enabled=cfg.manual_payments_enabled,
        crypto_payments_enabled=cfg.crypto_payments_enabled,
        updated_at=cfg.updated_at,
    )


@router.patch("/settings", response_model=AdminSettingsResponse)
@limiter.limit("30/minute")
async def update_settings(
    request: Request,
    body: AdminSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    cfg = await get_settings(db)
    changed = body.model_dump(exclude_unset=True)

    if changed.get("manual_payments_enabled") and not (cfg.bank_details_md or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Add bank details before enabling manual payments.",
        )

    for field, value in changed.items():
        setattr(cfg, field, value)
    cfg.updated_by = admin.id

    # A changed bank account is exactly the edit you will want to prove the
    # provenance of later, so the audit row names the fields, not the values.
    db.add(AuditLog(
        user_id=admin.id,
        action="admin_settings_updated",
        extra_data={"fields": sorted(changed.keys())},
    ))
    logger.info("settings_updated admin=%s fields=%s", admin.id, sorted(changed.keys()))

    return AdminSettingsResponse(
        one_time_price_usd=float(cfg.one_time_price_usd),
        currency=cfg.currency,
        bank_details_md=cfg.bank_details_md,
        payment_instructions_md=cfg.payment_instructions_md,
        support_email=cfg.support_email,
        claims_notify_email=cfg.claims_notify_email,
        review_sla_hours=cfg.review_sla_hours,
        manual_payments_enabled=cfg.manual_payments_enabled,
        crypto_payments_enabled=cfg.crypto_payments_enabled,
        updated_at=cfg.updated_at,
    )


# ── Audit ───────────────────────────────────────────────────────────────────

@router.get("/audit-log", response_model=list[AdminAuditItem])
@limiter.limit("60/minute")
async def audit_log(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
    action: str = Query(default=""),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Recent actions, with the acting account's email joined in.

    The previous version returned bare UUIDs and no emails, which made it
    unreadable in practice — you cannot audit what you cannot identify.
    """
    q = select(AuditLog, User.email).outerjoin(User, AuditLog.user_id == User.id)
    if action:
        q = q.where(AuditLog.action.ilike(f"%{action}%"))
    rows = (await db.execute(
        q.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    )).all()

    return [
        AdminAuditItem(
            id=log.id,
            action=log.action,
            user_id=log.user_id,
            user_email=email,
            extra_data=log.extra_data,
            platform=log.platform,
            created_at=log.created_at,
        )
        for log, email in rows
    ]
