import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.utils.security import decode_token


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User | None:
    auth_header = request.headers.get("Authorization", "")
    token = (
        auth_header.removeprefix("Bearer ").strip()
        if auth_header.startswith("Bearer ")
        else request.cookies.get("access_token")
    )
    if not token:
        return None
    user_id = decode_token(token, "access")
    if not user_id:
        return None
    try:
        result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
        return result.scalar_one_or_none()
    except (ValueError, Exception):
        return None


async def get_current_user(
    user: User | None = Depends(get_current_user_optional),
) -> User:
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


async def get_current_verified_user(user: User = Depends(get_current_user)) -> User:
    if not user.email_verified:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email verification required")
    return user


def has_access(user: User | None) -> bool:
    """Whether this user may use the app right now. The only correct gate.

    This — not `user.plan` — is the authority. `plan` is a denormalised copy
    that the nightly sweep only refreshes once a day, so gating on it would
    keep a lapsed user in for up to 24 hours and, worse, lock out a user for
    that long after they paid.

    Order matters. Revocation is checked FIRST because a refunded user may
    still be inside their original trial window: checked last, refunding
    someone in week one would silently do nothing and they would keep full
    access until the trial lapsed. The kill switch has to outrank every grant.

    Four things grant access, in descending permanence:
      lifetime_access_at  the one-time purchase, approved by an admin
      apple_sub_until     the App Store subscription bought in the iOS app
      paid_until          the dormant crypto rail, kept behind a flag
      trial_ends_at       the 14-day trial, granted once per device
    """
    if not user:
        return False
    if user.access_revoked_at:
        return False
    if user.lifetime_access_at:
        return True
    now = datetime.now(timezone.utc)
    if user.apple_sub_until and user.apple_sub_until > now:
        return True
    if user.paid_until and user.paid_until > now:
        return True
    if user.trial_ends_at and user.trial_ends_at > now:
        return True
    return False


async def get_current_subscriber(user: User = Depends(get_current_verified_user)) -> User:
    """Gate for the account features: protocols, the log, saved history.

    The peptide library and stacks are public reference content and are not
    behind this gate (see app/routers/peptides.py). Auth, consent, billing and
    the App Store purchase endpoints are reachable without access too, since a
    user who cannot pay cannot be asked to pay first.

    402 rather than 403 so the clients can tell "you need Peptora Pro" apart
    from "you are not allowed", and route to the paywall instead of the login
    page. See lib/api/client.js and components/auth/PlanGate.js on the web,
    and src/components/ProGate.js in the native app.
    """
    if not has_access(user):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Peptora Pro is required",
        )
    return user


async def get_current_admin(user: User = Depends(get_current_verified_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user
