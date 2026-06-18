import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import UserProtocol
from app.schemas import UserProtocolCreate, UserProtocolItem
from app.middleware.auth import get_current_verified_user
from app.middleware.rate_limit import limiter
from fastapi import Request

router = APIRouter(prefix="/protocols", tags=["protocols"])
logger = logging.getLogger("peptora.protocols")


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
        label=body.label,
        vial_mg=body.vial_mg,
        reconstituted=body.reconstituted,
        bac_water_ml=body.bac_water_ml,
        target_dose_mcg=body.target_dose_mcg,
        unit=body.unit,
        syringe_type=body.syringe_type,
        frequency=body.frequency,
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
