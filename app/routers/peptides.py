"""The peptide library.

Public reference content: readable without an account or a subscription.

It was briefly put behind the paid gate along with the rest of the app. That
made the iOS app demand a login before showing anything at all, which App
Review rejected under guideline 5.1.1(v): content that is not tied to an
account must be reachable without one. The library is reference material with
citations, the same for every reader, so there is nothing account-based about
it. What stays behind the gate is what a user saves: protocols, logs, history.

Still rate-limited per IP, like every other route.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Peptide, StackComponent
from app.middleware.rate_limit import limiter
from app.utils.text import clean_content
from app.schemas import PeptideCard, PeptideDetail

router = APIRouter(prefix="/peptides", tags=["peptides"])


@router.get("", response_model=list[PeptideCard])
@limiter.limit("60/minute")
async def list_peptides(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Peptide).order_by(Peptide.name)
    )
    return [
        clean_content(PeptideCard.model_validate(row).model_dump(mode="json"))
        for row in result.scalars().all()
    ]


@router.get("/{peptide_id}", response_model=PeptideDetail)
@limiter.limit("60/minute")
async def get_peptide(
    peptide_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Peptide)
        .where(Peptide.id == peptide_id)
        .options(
            selectinload(Peptide.references),
            selectinload(Peptide.dose_ranges),
            selectinload(Peptide.protocols),
            selectinload(Peptide.related_peptides),
            selectinload(Peptide.stack_memberships).selectinload(StackComponent.stack),
        )
    )
    peptide = result.scalar_one_or_none()
    if not peptide:
        raise HTTPException(status_code=404, detail="Peptide not found")
    return clean_content(PeptideDetail.model_validate(peptide).model_dump(mode="json"))
