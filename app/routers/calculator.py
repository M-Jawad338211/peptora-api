from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import User, TrialCounter, CalculatorUsage, Session as DBSession, AuditLog
from app.schemas import (
    TrialCheckRequest, TrialCheckResponse,
    RecordUseRequest, RecordUseResponse,
    CalculatorHistoryItem,
)
from app.middleware.auth import (
    get_current_user_optional, get_current_subscriber, get_current_admin, has_access,
)
from app.middleware.rate_limit import limiter
from app.utils.security import hash_ip

router = APIRouter(prefix="/calculator", tags=["calculator"])

# Anonymous visitors keep a small allowance: the calculator is the top of the
# funnel and the thing that convinces people the subscription is worth buying.
# Signing up converts that into the full 14-day trial.
ANON_LIMIT = 5

# There is no longer a standing free tier. A signed-in user is either inside
# an access window (trial or paid, both unlimited) or out of one, in which
# case the answer is the paywall rather than a smaller allowance.


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
        return TrialCheckResponse(allowed=True, reason="subscribed", remaining=None)

    tc = await _get_or_create_trial(db, user, body.device_fingerprint)

    if not user:
        if tc.calc_uses_anonymous >= ANON_LIMIT:
            return TrialCheckResponse(allowed=False, reason="anonymous_limit", uses_so_far=tc.calc_uses_anonymous)
        return TrialCheckResponse(
            allowed=True, reason="ok", remaining=ANON_LIMIT - tc.calc_uses_anonymous
        )

    # Signed in, no live window: the trial has been used up.
    return TrialCheckResponse(
        allowed=False,
        reason="subscription_required",
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
    tc = await _get_or_create_trial(db, user, body.device_fingerprint)

    # The write side has to enforce too. check-trial is advisory — a client
    # that skips it, or an expired trial that lapsed between the two calls,
    # would otherwise still land a row here.
    if user and not has_access(user):
        raise HTTPException(status_code=402, detail="Subscription required")
    if not user and tc.calc_uses_anonymous >= ANON_LIMIT:
        raise HTTPException(status_code=402, detail="Free preview used up. Create an account to continue.")

    if not user:
        tc.calc_uses_anonymous += 1
        new_count = tc.calc_uses_anonymous
    else:
        new_count = 0  # Inside an access window: nothing to meter.

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
