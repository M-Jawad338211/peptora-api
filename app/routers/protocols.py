import logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.models import UserProtocol, CycleLog
from app.schemas import (
    UserProtocolCreate,
    UserProtocolUpdate,
    UserProtocolItem,
    UserProtocolDetail,
    DoseLogCreate,
    DoseLogItem,
)
from app.middleware.auth import get_current_verified_user
from app.middleware.rate_limit import limiter
from fastapi import Request

router = APIRouter(prefix="/protocols", tags=["protocols"])
logger = logging.getLogger("peptora.protocols")


@router.get("/stats/summary", response_model=dict)
@limiter.limit("30/minute")
async def protocol_stats(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_verified_user),
):
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    active_count = (await db.execute(
        select(func.count()).where(UserProtocol.user_id == user.id, UserProtocol.status == "active")
    )).scalar() or 0

    total_count = (await db.execute(
        select(func.count()).where(UserProtocol.user_id == user.id)
    )).scalar() or 0

    logs_week = (await db.execute(
        select(func.count()).where(CycleLog.user_id == user.id, CycleLog.taken_at >= week_ago)
    )).scalar() or 0

    total_logs = (await db.execute(
        select(func.count()).where(CycleLog.user_id == user.id)
    )).scalar() or 0

    return {
        "active_protocols": active_count,
        "total_protocols": total_count,
        "logs_this_week": logs_week,
        "total_logs": total_logs,
    }


@router.post("", response_model=UserProtocolItem, status_code=201)
@limiter.limit("30/minute")
async def create_protocol(
    request: Request,
    body: UserProtocolCreate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_verified_user),
):
    protocol = UserProtocol(
        user_id=user.id,
        peptide_id=body.peptide_id,
        peptide_name=body.peptide_name,
        stack_id=body.stack_id,
        stack_name=body.stack_name,
        label=body.label,
        status=body.status,
        vial_mg=body.vial_mg,
        reconstituted=body.reconstituted,
        bac_water_ml=body.bac_water_ml,
        target_dose_mcg=body.target_dose_mcg,
        unit=body.unit,
        syringe_type=body.syringe_type,
        frequency=body.frequency,
        start_date=body.start_date,
        duration_weeks=body.duration_weeks,
        notes=body.notes,
    )
    db.add(protocol)
    await db.flush()
    logger.info("protocol_created user_id=%s peptide_id=%s", user.id, body.peptide_id)
    return protocol


@router.get("", response_model=list[UserProtocolItem])
@limiter.limit("60/minute")
async def list_protocols(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_verified_user),
):
    result = await db.execute(
        select(UserProtocol)
        .where(UserProtocol.user_id == user.id)
        .order_by(UserProtocol.created_at.desc())
        .limit(100)
    )
    return result.scalars().all()


@router.get("/{protocol_id}", response_model=UserProtocolDetail)
@limiter.limit("60/minute")
async def get_protocol(
    request: Request,
    protocol_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_verified_user),
):
    result = await db.execute(
        select(UserProtocol)
        .options(selectinload(UserProtocol.dose_logs))
        .where(UserProtocol.id == protocol_id, UserProtocol.user_id == user.id)
    )
    protocol = result.scalar_one_or_none()
    if not protocol:
        raise HTTPException(status_code=404, detail="Protocol not found")
    return protocol


@router.patch("/{protocol_id}", response_model=UserProtocolItem)
@limiter.limit("30/minute")
async def update_protocol(
    request: Request,
    protocol_id: str,
    body: UserProtocolUpdate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_verified_user),
):
    result = await db.execute(
        select(UserProtocol).where(
            UserProtocol.id == protocol_id, UserProtocol.user_id == user.id
        )
    )
    protocol = result.scalar_one_or_none()
    if not protocol:
        raise HTTPException(status_code=404, detail="Protocol not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(protocol, field, value)

    await db.flush()
    logger.info("protocol_updated user_id=%s protocol_id=%s", user.id, protocol_id)
    return protocol


@router.delete("/{protocol_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_protocol(
    request: Request,
    protocol_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_verified_user),
):
    result = await db.execute(
        select(UserProtocol).where(
            UserProtocol.id == protocol_id,
            UserProtocol.user_id == user.id,
        )
    )
    protocol = result.scalar_one_or_none()
    if not protocol:
        raise HTTPException(status_code=404, detail="Protocol not found")
    await db.delete(protocol)
    logger.info("protocol_deleted user_id=%s protocol_id=%s", user.id, protocol_id)


# ── Dose logs scoped to a protocol ───────────────────────────────────────────

@router.post("/{protocol_id}/logs", response_model=DoseLogItem, status_code=201)
@limiter.limit("60/minute")
async def add_protocol_log(
    request: Request,
    protocol_id: str,
    body: DoseLogCreate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_verified_user),
):
    p_result = await db.execute(
        select(UserProtocol).where(
            UserProtocol.id == protocol_id, UserProtocol.user_id == user.id
        )
    )
    if not p_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Protocol not found")

    log = CycleLog(
        user_id=user.id,
        protocol_id=protocol_id,
        peptide_name=body.peptide_name,
        dose=body.dose,
        notes=body.notes,
        taken_at=body.taken_at or datetime.now(timezone.utc),
    )
    db.add(log)
    await db.flush()
    logger.info("dose_logged user_id=%s protocol_id=%s", user.id, protocol_id)
    return log


@router.get("/{protocol_id}/logs", response_model=list[DoseLogItem])
@limiter.limit("60/minute")
async def list_protocol_logs(
    request: Request,
    protocol_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_verified_user),
):
    p_result = await db.execute(
        select(UserProtocol).where(
            UserProtocol.id == protocol_id, UserProtocol.user_id == user.id
        )
    )
    if not p_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Protocol not found")

    logs_result = await db.execute(
        select(CycleLog)
        .where(CycleLog.protocol_id == protocol_id, CycleLog.user_id == user.id)
        .order_by(CycleLog.taken_at.desc())
        .limit(200)
    )
    return logs_result.scalars().all()


@router.delete("/{protocol_id}/logs/{log_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_protocol_log(
    request: Request,
    protocol_id: str,
    log_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_verified_user),
):
    result = await db.execute(
        select(CycleLog).where(
            CycleLog.id == log_id,
            CycleLog.protocol_id == protocol_id,
            CycleLog.user_id == user.id,
        )
    )
    log = result.scalar_one_or_none()
    if not log:
        raise HTTPException(status_code=404, detail="Log entry not found")
    await db.delete(log)
