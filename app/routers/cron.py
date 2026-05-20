import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import User, CycleLog
from app.config import settings
from app.utils.push import send_expo_push
from app.schemas import CronReminderResult

router = APIRouter(prefix="/internal/cron", tags=["cron"])
logger = logging.getLogger("peptora.cron")


def _verify_secret(x_cron_secret: str = Header(...)):
    if not settings.CRON_SECRET or x_cron_secret != settings.CRON_SECRET:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


@router.post("/weekly-reminders", response_model=CronReminderResult)
async def send_weekly_reminders(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_verify_secret),
):
    now = datetime.now(timezone.utc)

    latest_sq = (
        select(CycleLog.user_id, func.max(CycleLog.taken_at).label("last_taken"))
        .group_by(CycleLog.user_id)
        .subquery()
    )

    result = await db.execute(
        select(User)
        .join(latest_sq, User.id == latest_sq.c.user_id)
        .where(
            User.expo_push_token.is_not(None),
            latest_sq.c.last_taken >= now - timedelta(days=8),
            latest_sq.c.last_taken <= now - timedelta(days=7),
        )
    )
    users = result.scalars().all()

    sent = failed = 0
    for user in users:
        ok = await send_expo_push(
            token=user.expo_push_token,
            title="Time to log your cycle 💉",
            body="It's been 7 days since your last Peptora entry. Log today's dose.",
            data={"screen": "tracker"},
        )
        if ok:
            sent += 1
        else:
            failed += 1

    logger.info("weekly_reminders total=%d sent=%d failed=%d", len(users), sent, failed)
    return CronReminderResult(sent=sent, failed=failed, skipped=0)
