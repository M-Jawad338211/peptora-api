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
from sqlalchemy import update

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


@router.post("/billing-sweep", response_model=CronReminderResult)
async def billing_sweep(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_verify_secret),
):
    """Daily: warn people before access ends, then demote those it ended for.

    Nothing here gates access — has_access() reads the timestamps directly, so
    a lapsed user loses the tools the moment their window passes, whether or
    not this job has run. This only keeps the denormalised `plan` column
    honest (admin lists, the native app) and sends the reminder emails that
    take the place of Stripe's automatic renewal.
    """
    from app.utils.email import send_renewal_reminder_email, send_trial_ending_email

    now = datetime.now(timezone.utc)
    window_start = now + timedelta(days=2)
    window_end = now + timedelta(days=3)

    sent = failed = 0

    # Paid access ending in ~3 days. Nothing auto-renews, so this email is the
    # only thing standing between a paying user and a silent lapse.
    expiring = await db.execute(
        select(User).where(
            User.paid_until.is_not(None),
            User.paid_until > window_start,
            User.paid_until <= window_end,
        )
    )
    for user in expiring.scalars().all():
        try:
            await send_renewal_reminder_email(user.email, user.full_name, (user.paid_until - now).days)
            sent += 1
        except Exception:
            logger.exception("renewal reminder failed for %s", user.email)
            failed += 1

    # Trials ending in ~3 days, for users who never paid.
    trials = await db.execute(
        select(User).where(
            User.trial_ends_at.is_not(None),
            User.trial_ends_at > window_start,
            User.trial_ends_at <= window_end,
            User.paid_until.is_(None),
        )
    )
    for user in trials.scalars().all():
        try:
            await send_trial_ending_email(user.email, user.full_name, (user.trial_ends_at - now).days)
            sent += 1
        except Exception:
            logger.exception("trial reminder failed for %s", user.email)
            failed += 1

    # Demote anyone whose windows have both closed. Set-based, so it stays one
    # statement regardless of how many users lapse on a given day.
    demoted = await db.execute(
        update(User)
        .where(
            User.plan == "pro",
            (User.paid_until.is_(None)) | (User.paid_until <= now),
            (User.trial_ends_at.is_(None)) | (User.trial_ends_at <= now),
        )
        .values(plan="free")
    )

    logger.info("billing_sweep sent=%d failed=%d demoted=%d", sent, failed, demoted.rowcount or 0)
    return CronReminderResult(sent=sent, failed=failed, skipped=demoted.rowcount or 0)
