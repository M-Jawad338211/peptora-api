from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
from app.database import get_db
from app.models import User, CalculatorUsage, AuditLog
from app.schemas import AdminStatsResponse, AdminUserItem
from app.middleware.auth import get_current_admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/stats", response_model=AdminStatsResponse)
async def admin_stats(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    total_users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    free_users = (await db.execute(select(func.count()).select_from(User).where(User.plan == "free"))).scalar_one()
    pro_users = (await db.execute(select(func.count()).select_from(User).where(User.plan == "pro"))).scalar_one()
    calcs_today = (await db.execute(
        select(func.count()).select_from(CalculatorUsage).where(CalculatorUsage.created_at >= today)
    )).scalar_one()
    calcs_week = (await db.execute(
        select(func.count()).select_from(CalculatorUsage).where(CalculatorUsage.created_at >= today - timedelta(days=7))
    )).scalar_one()
    calcs_month = (await db.execute(
        select(func.count()).select_from(CalculatorUsage).where(CalculatorUsage.created_at >= today - timedelta(days=30))
    )).scalar_one()
    new_signups_today = (await db.execute(
        select(func.count()).select_from(User).where(User.created_at >= today)
    )).scalar_one()

    return AdminStatsResponse(
        total_users=total_users,
        free_users=free_users,
        pro_users=pro_users,
        calcs_today=calcs_today,
        calcs_this_week=calcs_week,
        calcs_this_month=calcs_month,
        revenue_today=0.0,  # Calculated from Stripe dashboard
        new_signups_today=new_signups_today,
    )


@router.get("/users", response_model=list[AdminUserItem])
async def list_users(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
    search: str = Query(default=""),
    plan: str = Query(default=""),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0),
):
    from sqlalchemy.orm import selectinload
    q = select(User).options(selectinload(User.trial_counter))
    if search:
        q = q.where(User.email.ilike(f"%{search}%"))
    if plan:
        q = q.where(User.plan == plan)
    q = q.order_by(User.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(q)
    users = result.scalars().all()

    items = []
    for u in users:
        items.append(AdminUserItem(
            id=u.id,
            email=u.email,
            full_name=u.full_name,
            plan=u.plan,
            is_admin=u.is_admin,
            created_at=u.created_at,
            last_login=u.last_login,
            calc_uses_anonymous=u.trial_counter.calc_uses_anonymous if u.trial_counter else 0,
            calc_uses_free=u.trial_counter.calc_uses_free if u.trial_counter else 0,
        ))
    return items


@router.post("/users/{user_id}/set-plan")
async def set_user_plan(
    user_id: str,
    plan: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    if plan not in ("free", "pro"):
        raise HTTPException(status_code=400, detail="Invalid plan")
    await db.execute(update(User).where(User.id == user_id).values(plan=plan))
    db.add(AuditLog(user_id=admin.id, action="admin_action", metadata={"action": "set_plan", "target": user_id, "plan": plan}))
    return {"message": f"User plan updated to {plan}"}


@router.get("/audit-log")
async def audit_log(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
    limit: int = Query(default=100, le=500),
):
    result = await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    )
    logs = result.scalars().all()
    return [
        {"id": str(l.id), "user_id": str(l.user_id) if l.user_id else None,
         "action": l.action, "platform": l.platform, "created_at": l.created_at.isoformat()}
        for l in logs
    ]
