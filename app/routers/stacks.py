"""The peptide stacks.

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
from app.models import PeptideStack, StackComponent, Peptide
from app.middleware.rate_limit import limiter
from app.utils.text import clean_content
from app.schemas import StackCard, StackDetail

router = APIRouter(prefix="/stacks", tags=["stacks"])


@router.get("", response_model=list[StackCard])
@limiter.limit("60/minute")
async def list_stacks(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PeptideStack).order_by(PeptideStack.name)
    )
    return [
        clean_content(StackCard.model_validate(row).model_dump(mode="json"))
        for row in result.scalars().all()
    ]


@router.get("/{stack_id}", response_model=StackDetail)
@limiter.limit("60/minute")
async def get_stack(
    stack_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PeptideStack)
        .where(PeptideStack.id == stack_id)
        .options(
            selectinload(PeptideStack.components)
            .selectinload(StackComponent.peptide)
            .selectinload(Peptide.dose_ranges),
            selectinload(PeptideStack.stack_references),
        )
    )
    stack = result.scalar_one_or_none()
    if not stack:
        raise HTTPException(status_code=404, detail="Stack not found")
    return clean_content(StackDetail.model_validate(stack).model_dump(mode="json"))
