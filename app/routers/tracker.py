import logging
from datetime import timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from app.database import get_db
from app.models import User, CycleLog
from app.schemas import CycleLogCreate, CycleLogItem
from app.middleware.auth import get_current_subscriber
from app.middleware.rate_limit import limiter
from fastapi import Request

router = APIRouter(prefix="/tracker", tags=["tracker"])
logger = logging.getLogger("peptora.tracker")


@router.post("/logs", response_model=CycleLogItem, status_code=201)
@limiter.limit("60/minute")
async def create_log(
    request: Request,
    body: CycleLogCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_subscriber),
):
    from datetime import datetime
    taken_at = body.taken_at or datetime.now(timezone.utc)
    log = CycleLog(
        user_id=user.id,
        peptide_name=body.peptide_name,
        dose=body.dose,
        notes=body.notes,
        taken_at=taken_at,
    )
    db.add(log)
    await db.flush()
    logger.info("cycle_log user_id=%s peptide=%s dose=%s", user.id, body.peptide_name, body.dose)
    return log


@router.get("/logs", response_model=list[CycleLogItem])
@limiter.limit("60/minute")
async def get_logs(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_subscriber),
):
    result = await db.execute(
        select(CycleLog)
        .where(CycleLog.user_id == user.id)
        .order_by(CycleLog.taken_at.desc())
        .limit(100)
    )
    return result.scalars().all()


@router.delete("/logs/{log_id}", status_code=204)
@limiter.limit("60/minute")
async def delete_log(
    request: Request,
    log_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_subscriber),
):
    result = await db.execute(
        select(CycleLog).where(CycleLog.id == log_id, CycleLog.user_id == user.id)
    )
    log = result.scalar_one_or_none()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    await db.delete(log)
