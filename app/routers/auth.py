import uuid
import secrets
import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.database import get_db
from app.models import User, TrialCounter, AuditLog, EmailVerificationOTP
from app.schemas import (
    RegisterRequest, LoginRequest, ForgotPasswordRequest,
    ResetPasswordRequest, UserResponse, TrialCountInfo, SubscriptionInfo,
    VerifyEmailRequest, ResendVerificationOTPRequest, PushTokenUpdate,
)
from app.utils.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    decode_token, hash_ip, hash_otp, verify_otp,
)
from app.utils.email import send_welcome_email, send_password_reset_email, send_email_verification_otp
from app.middleware.auth import get_current_verified_user, get_current_user_optional
from app.middleware.rate_limit import limiter

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("peptora.auth")

from app.config import settings as _settings
COOKIE_OPTS = dict(
    httponly=True,
    # SameSite=None;Secure is required for cross-origin cookie auth (frontend on peptora.io,
    # API on railway.app). Lax is sufficient locally where both run on localhost.
    samesite="lax" if _settings.ENVIRONMENT == "development" else "none",
    secure=_settings.ENVIRONMENT != "development",
)


def _set_tokens(response: Response, user_id: str) -> dict:
    access = create_access_token(user_id)
    refresh = create_refresh_token(user_id)
    response.set_cookie("access_token", access, max_age=900, **COOKIE_OPTS)
    response.set_cookie("refresh_token", refresh, max_age=60 * 60 * 24 * 30, **COOKIE_OPTS)
    return {"access_token": access, "refresh_token": refresh}


def _user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "plan": user.plan,
        "email_verified": user.email_verified,
        "consent_accepted": user.consent_accepted,
    }


def _generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def _send_verification_otp(db: AsyncSession, user: User) -> None:
    otp = _generate_otp()
    db.add(EmailVerificationOTP(
        user_id=user.id,
        otp_hash=hash_otp(user.email, otp),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    ))
    await db.flush()
    await send_email_verification_otp(user.email, user.full_name, otp)


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
        email_verified=False,
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

    try:
        await _send_verification_otp(db, user)
    except Exception as exc:
        logger.exception("Failed to send verification OTP email to %s", user.email)
        raise HTTPException(status_code=502, detail="Could not send verification email. Check email configuration.") from exc

    return {
        "user": _user_payload(user),
        "message": "Account created. Check your email for the verification code.",
        "requires_verification": True,
    }


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

    if not user.email_verified:
        try:
            await _send_verification_otp(db, user)
        except Exception as exc:
            logger.exception("Failed to send verification OTP email to %s", user.email)
            raise HTTPException(status_code=502, detail="Could not send verification email. Check email configuration.") from exc
        return {
            "user": _user_payload(user),
            "message": "Email verification required",
            "requires_verification": True,
        }

    await db.execute(update(User).where(User.id == user.id).values(last_login=datetime.now(timezone.utc)))
    db.add(AuditLog(
        user_id=user.id, action="login",
        ip_hash=hash_ip(request.client.host if request.client else ""),
        platform=request.headers.get("X-Platform", "web"),
    ))

    tokens = _set_tokens(response, str(user.id))
    return {"user": _user_payload(user), **tokens}


@router.post("/verify-email")
@limiter.limit("10/minute")
async def verify_email(
    request: Request,
    body: VerifyEmailRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")

    if user.email_verified:
        raise HTTPException(status_code=400, detail="Email already verified. Please log in.")

    now = datetime.now(timezone.utc)
    otp_result = await db.execute(
        select(EmailVerificationOTP)
        .where(
            EmailVerificationOTP.user_id == user.id,
            EmailVerificationOTP.used_at.is_(None),
            EmailVerificationOTP.expires_at > now,
        )
        .order_by(EmailVerificationOTP.created_at.desc())
    )
    otp_record = otp_result.scalars().first()

    if not otp_record:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")

    if otp_record.attempts >= 5:
        raise HTTPException(status_code=429, detail="Too many verification attempts. Request a new code.")

    otp_record.attempts += 1
    if not verify_otp(user.email, body.otp, otp_record.otp_hash):
        await db.commit()
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")

    otp_record.used_at = now
    user.email_verified = True
    user.last_login = now
    db.add(AuditLog(
        user_id=user.id,
        action="email_verified",
        ip_hash=hash_ip(request.client.host if request.client else ""),
        platform=request.headers.get("X-Platform", "web"),
    ))

    try:
        await send_welcome_email(user.email, user.full_name)
    except Exception:
        logger.exception("Failed to send welcome email to %s", user.email)

    tokens = _set_tokens(response, str(user.id))
    return {"user": _user_payload(user), "message": "Email verified", **tokens}


@router.post("/resend-verification-otp")
@limiter.limit("3/minute")
async def resend_verification_otp(
    request: Request,
    body: ResendVerificationOTPRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if user and not user.email_verified:
        try:
            await _send_verification_otp(db, user)
            db.add(AuditLog(
                user_id=user.id,
                action="verification_otp_resent",
                ip_hash=hash_ip(request.client.host if request.client else ""),
                platform=request.headers.get("X-Platform", "web"),
            ))
        except Exception as exc:
            logger.exception("Failed to resend verification OTP email to %s", user.email)
            raise HTTPException(status_code=502, detail="Could not send verification email. Check email configuration.") from exc

    return {"message": "If that email needs verification, a new code has been sent"}


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
        await db.execute(update(User).where(User.id == user.id).values(expo_push_token=None))
    response.delete_cookie("access_token", httponly=True,
                           samesite="lax" if _settings.ENVIRONMENT == "development" else "none",
                           secure=_settings.ENVIRONMENT != "development")
    response.delete_cookie("refresh_token", httponly=True,
                           samesite="lax" if _settings.ENVIRONMENT == "development" else "none",
                           secure=_settings.ENVIRONMENT != "development")
    return {"message": "Logged out"}


@router.get("/me", response_model=UserResponse)
async def me(
    user: User = Depends(get_current_verified_user),
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
        plan=u.plan, is_admin=u.is_admin, email_verified=u.email_verified,
        consent_accepted=u.consent_accepted,
        trial_count=trial_info, subscription=sub_info,
    )


@router.post("/accept-consent", status_code=200)
async def accept_consent(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_verified_user),
):
    await db.execute(
        update(User).where(User.id == user.id).values(
            consent_accepted=True,
            consent_accepted_at=datetime.now(timezone.utc),
        )
    )
    logger.info("consent_accepted user_id=%s", user.id)
    return {"message": "Consent accepted"}


@router.put("/push-token", status_code=200)
async def update_push_token(
    body: PushTokenUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_verified_user),
):
    await db.execute(update(User).where(User.id == user.id).values(expo_push_token=body.token))
    logger.info("push_token_updated user_id=%s", user.id)
    return {"message": "Push token saved"}


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
