from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import PeptideStack, StackComponent, Peptide
from app.middleware.rate_limit import limiter
from app.schemas import StackCard, StackDetail

router = APIRouter(prefix="/stacks", tags=["stacks"])


@router.get("", response_model=list[StackCard])
@limiter.limit("60/minute")
async def list_stacks(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PeptideStack).order_by(PeptideStack.name)
    )
    return result.scalars().all()


@router.get("/{stack_id}", response_model=StackDetail)
@limiter.limit("60/minute")
async def get_stack(stack_id: str, request: Request, db: AsyncSession = Depends(get_db)):
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
    return stack
