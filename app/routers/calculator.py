from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import (
    get_current_admin,
    get_current_subscriber,
    get_current_user_optional,
    has_access,
)
from app.middleware.rate_limit import limiter
from app.models import AuditLog, CalculatorUsage, TrialCounter, User
from app.models import Session as DBSession
from app.schemas import (
    CalculatorHistoryItem,
    RecordUseRequest,
    RecordUseResponse,
    TrialCheckRequest,
    TrialCheckResponse,
)
from app.utils.security import hash_ip

router = APIRouter(prefix="/calculator", tags=["calculator"])

# There is no free tier and no anonymous allowance. Peptora is a one-time
# purchase behind a 14-day trial: a user is either inside an access window or
# out of one, and the answer to being out of one is the paywall.
#
# The anonymous 5-calculation preview was removed with the move to a paid app
# shell. It existed as top-of-funnel, but keeping it meant carving an
# exception into a gate whose entire value is having none — and the funnel
# belongs on the marketing site, where it also earns search traffic.


async def _get_or_create_trial(db: AsyncSession, user: User | None, fp: str) -> TrialCounter:
    if user:
        result = await db.execute(select(TrialCounter).where(TrialCounter.user_id == user.id))
        tc = result.scalar_one_or_none()
        if not tc:
            tc = TrialCounter(user_id=user.id, device_fingerprint=fp)
            db.add(tc)
            await db.flush()
    else:
        result = await db.execute(select(TrialCounter).where(TrialCounter.device_fingerprint == fp).limit(1))
        tc = result.scalars().first()
        if not tc:
            tc = TrialCounter(device_fingerprint=fp)
            db.add(tc)
            await db.flush()
    return tc


@router.post("/check-trial", response_model=TrialCheckResponse)
@limiter.limit("60/minute")
async def check_trial(
    request: Request,
    body: TrialCheckRequest,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    if has_access(user):
        return TrialCheckResponse(allowed=True, reason="licensed", remaining=None)

    if not user:
        return TrialCheckResponse(allowed=False, reason="signup_required")

    tc = await _get_or_create_trial(db, user, body.device_fingerprint)

    # Signed in, no live window: the trial has run out or was never granted.
    return TrialCheckResponse(
        allowed=False,
        reason="licence_required",
        uses_so_far=tc.calc_uses_anonymous + tc.calc_uses_free,
    )


@router.post("/record-use", response_model=RecordUseResponse)
@limiter.limit("60/minute")
async def record_use(
    request: Request,
    body: RecordUseRequest,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    # The write side has to enforce too. check-trial is advisory — a client
    # that skips it, or a trial that lapsed between the two calls, would
    # otherwise still land a row here.
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not has_access(user):
        raise HTTPException(status_code=402, detail="A Peptora licence is required")

    # Still ensures the counter row exists — /auth/me reports it — but there
    # is nothing to meter for a user inside an access window.
    await _get_or_create_trial(db, user, body.device_fingerprint)
    new_count = 0

    # Get or create session
    session_result = await db.execute(
        select(DBSession).where(DBSession.device_fingerprint == body.device_fingerprint).limit(1)
    )
    session = session_result.scalar_one_or_none()
    if not session:
        session = DBSession(
            user_id=user.id if user else None,
            device_fingerprint=body.device_fingerprint,
            platform=body.platform,
            ip_hash=hash_ip(request.client.host if request.client else ""),
            user_agent=request.headers.get("User-Agent"),
        )
        db.add(session)
        await db.flush()

    usage = CalculatorUsage(
        user_id=user.id if user else None,
        session_id=session.id,
        peptide_name=body.peptide_name,
        vial_mg=body.vial_mg,
        bac_water_ml=body.bac_water_ml,
        target_mcg=body.target_mcg,
        result_units=body.result_units,
        result_ml=body.result_ml,
        platform=body.platform,
    )
    db.add(usage)

    db.add(AuditLog(
        user_id=user.id if user else None,
        action="calc_use",
        extra_data={"peptide": body.peptide_name, "platform": body.platform},
        ip_hash=hash_ip(request.client.host if request.client else ""),
        platform=body.platform,
    ))

    return RecordUseResponse(recorded=True, new_count=new_count)


@router.get("/history", response_model=list[CalculatorHistoryItem])
async def get_history(
    user: User = Depends(get_current_subscriber),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(CalculatorUsage)
        .where(CalculatorUsage.user_id == user.id)
        .order_by(CalculatorUsage.created_at.desc())
        .limit(100)
    )
    return result.scalars().all()


@router.get("/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)
    month_start = today_start - timedelta(days=30)

    async def count_since(since: datetime) -> int:
        r = await db.execute(
            select(func.count()).select_from(CalculatorUsage)
            .where(CalculatorUsage.created_at >= since)
        )
        return r.scalar_one()

    top_peptides_result = await db.execute(
        select(CalculatorUsage.peptide_name, func.count().label("cnt"))
        .group_by(CalculatorUsage.peptide_name)
        .order_by(func.count().desc())
        .limit(10)
    )

    by_platform_result = await db.execute(
        select(CalculatorUsage.platform, func.count().label("cnt"))
        .group_by(CalculatorUsage.platform)
    )

    return {
        "calcs_today": await count_since(today_start),
        "calcs_week": await count_since(week_start),
        "calcs_month": await count_since(month_start),
        "top_peptides": [{"peptide": r[0], "count": r[1]} for r in top_peptides_result.all()],
        "by_platform": [{"platform": r[0], "count": r[1]} for r in by_platform_result.all()],
    }
