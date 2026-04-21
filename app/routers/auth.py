import uuid
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.database import get_db
from app.models import User, TrialCounter, AuditLog
from app.schemas import (
    RegisterRequest, LoginRequest, ForgotPasswordRequest,
    ResetPasswordRequest, UserResponse, TrialCountInfo, SubscriptionInfo,
)
from app.utils.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    decode_token, hash_ip,
)
from app.utils.email import send_welcome_email, send_password_reset_email
from app.middleware.auth import get_current_user, get_current_user_optional
from app.middleware.rate_limit import limiter

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_OPTS = dict(httponly=True, samesite="strict", secure=True)


def _set_tokens(response: Response, user_id: str) -> None:
    response.set_cookie("access_token", create_access_token(user_id), max_age=900, **COOKIE_OPTS)
    response.set_cookie("refresh_token", create_refresh_token(user_id), max_age=60 * 60 * 24 * 30, **COOKIE_OPTS)


@router.post("/register", status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def register(
    request: Request,
    body: RegisterRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        plan="free",
    )
    db.add(user)
    await db.flush()

    # Link or create trial counter
    tc_result = await db.execute(
        select(TrialCounter).where(TrialCounter.device_fingerprint == body.device_fingerprint)
    )
    tc = tc_result.scalar_one_or_none()
    if tc:
        tc.user_id = user.id
        tc.signup_bonus_granted = True
    else:
        tc = TrialCounter(
            user_id=user.id,
            device_fingerprint=body.device_fingerprint,
            signup_bonus_granted=True,
        )
        db.add(tc)

    db.add(AuditLog(
        user_id=user.id, action="signup",
        ip_hash=hash_ip(request.client.host if request.client else ""),
        platform=request.headers.get("X-Platform", "web"),
    ))

    _set_tokens(response, str(user.id))

    try:
        await send_welcome_email(user.email, user.full_name)
    except Exception:
        pass

    return {"user": {"id": user.id, "email": user.email, "full_name": user.full_name, "plan": user.plan}, "message": "Account created"}


@router.post("/login")
@limiter.limit("10/minute")
async def login(
    request: Request,
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    await db.execute(update(User).where(User.id == user.id).values(last_login=datetime.now(timezone.utc)))
    db.add(AuditLog(
        user_id=user.id, action="login",
        ip_hash=hash_ip(request.client.host if request.client else ""),
        platform=request.headers.get("X-Platform", "web"),
    ))

    _set_tokens(response, str(user.id))
    return {"user": {"id": user.id, "email": user.email, "full_name": user.full_name, "plan": user.plan}}


@router.post("/refresh")
async def refresh_token(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(status_code=401, detail="No refresh token")
    user_id = decode_token(token, "refresh")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    response.set_cookie("access_token", create_access_token(user_id), max_age=900, **COOKIE_OPTS)
    return {"message": "Token refreshed"}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user_optional),
):
    if user:
        db.add(AuditLog(user_id=user.id, action="logout"))
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")
    return {"message": "Logged out"}


@router.get("/me", response_model=UserResponse)
async def me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(User)
        .options(selectinload(User.trial_counter), selectinload(User.subscriptions))
        .where(User.id == user.id)
    )
    u = result.scalar_one()

    trial_info = None
    if u.trial_counter:
        trial_info = TrialCountInfo(
            anonymous_uses=u.trial_counter.calc_uses_anonymous,
            free_uses=u.trial_counter.calc_uses_free,
            signup_bonus_granted=u.trial_counter.signup_bonus_granted,
        )

    sub_info = None
    active_sub = next((s for s in u.subscriptions if s.status == "active"), None)
    if active_sub:
        sub_info = SubscriptionInfo(
            status=active_sub.status,
            current_period_end=active_sub.current_period_end,
            cancel_at_period_end=active_sub.cancel_at_period_end,
        )

    return UserResponse(
        id=u.id, email=u.email, full_name=u.full_name,
        plan=u.plan, is_admin=u.is_admin,
        trial_count=trial_info, subscription=sub_info,
    )


@router.post("/forgot-password")
@limiter.limit("5/minute")
async def forgot_password(
    request: Request,
    body: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if user:
        reset_token = str(uuid.uuid4())
        # Store token in audit_log with action=password_reset_token
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        db.add(AuditLog(
            user_id=user.id, action="password_reset_token",
            extra_data={"token": reset_token, "expires": expiry.isoformat()},
        ))
        try:
            await send_password_reset_email(user.email, reset_token)
        except Exception:
            pass
    # Always 200 — don't reveal if email exists
    return {"message": "If that email is registered, a reset link has been sent"}


@router.post("/reset-password")
@limiter.limit("5/minute")
async def reset_password(
    request: Request,
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(AuditLog).where(
            AuditLog.action == "password_reset_token",
        ).order_by(AuditLog.created_at.desc())
    )
    logs = result.scalars().all()
    matching = next(
        (l for l in logs if l.extra_data and l.extra_data.get("token") == body.token
         and datetime.fromisoformat(l.extra_data["expires"]) > now),
        None,
    )
    if not matching:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    await db.execute(
        update(User)
        .where(User.id == matching.user_id)
        .values(password_hash=hash_password(body.new_password))
    )
    # Invalidate token by updating its metadata
    matching.extra_data = {**matching.extra_data, "used": True}
    return {"message": "Password updated"}
