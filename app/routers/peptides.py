from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Peptide
from app.middleware.rate_limit import limiter
from app.schemas import PeptideCard, PeptideDetail

router = APIRouter(prefix="/peptides", tags=["peptides"])


@router.get("", response_model=list[PeptideCard])
@limiter.limit("60/minute")
async def list_peptides(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Peptide).order_by(Peptide.name)
    )
    return result.scalars().all()


@router.get("/{peptide_id}", response_model=PeptideDetail)
@limiter.limit("60/minute")
async def get_peptide(peptide_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Peptide)
        .where(Peptide.id == peptide_id)
        .options(
            selectinload(Peptide.references),
            selectinload(Peptide.dose_ranges),
            selectinload(Peptide.protocols),
            selectinload(Peptide.related_peptides),
            selectinload(Peptide.stack_compatibility),
        )
    )
    peptide = result.scalar_one_or_none()
    if not peptide:
        raise HTTPException(status_code=404, detail="Peptide not found")
    return peptide
