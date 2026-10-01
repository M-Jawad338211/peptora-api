import uuid
import secrets
import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from app.database import get_db
from app.models import (
    AppleSubscription, AppSettings, AuditLog, CalculatorUsage, CryptoPayment,
    CycleLog, EmailVerificationOTP, PaymentClaim, TrialCounter, TrialGrant,
    User, UserProtocol,
)
from app.models import Session as DBSession
from app.schemas import (
    RegisterRequest, LoginRequest, ForgotPasswordRequest,
    ResetPasswordRequest, UserResponse, TrialCountInfo, VerifyEmailRequest, ResendVerificationOTPRequest, PushTokenUpdate,
    DeleteAccountRequest, DeleteAccountResponse,
)
from app.utils.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    decode_token, hash_ip, hash_otp, verify_otp,
)
from app.utils.email import send_welcome_email, send_password_reset_email, send_email_verification_otp
from app.middleware.auth import get_current_user, get_current_verified_user, get_current_user_optional
from app.middleware.rate_limit import limiter
from app.config import settings as _settings

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("peptora.auth")

COOKIE_OPTS = dict(
    httponly=True,
    # SameSite=None;Secure is required for cross-origin cookie auth (web app on peptora.io,
    # API on railway.app). Lax is sufficient locally where both run on localhost — and
    # Secure would make the browser drop the cookie over plain http.
    samesite="lax" if _settings.is_development else "none",
    secure=not _settings.is_development,
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

    # Fallback fingerprints are shared across many devices — never link them,
    # always create a fresh counter and never bind a trial to them.
    is_fallback_fp = body.device_fingerprint.startswith("fallback-fp-")

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        plan="free",
        email_verified=False,
        # Persisted because the trial is granted at /verify-email, where the
        # fingerprint is not in scope. Fallback fingerprints are shared across
        # devices and must never be bound against, so they are not stored.
        signup_fingerprint=None if is_fallback_fp else body.device_fingerprint,
    )
    db.add(user)
    await db.flush()

    # Link or create trial counter.
    tc = None
    if not is_fallback_fp:
        tc_result = await db.execute(
            select(TrialCounter).where(TrialCounter.device_fingerprint == body.device_fingerprint).limit(1)
        )
        tc = tc_result.scalars().first()
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

    # Start the 14-day trial here rather than at registration: an unverified
    # account can never log in, so a trial granted at signup would spend most
    # of itself before the user ever reached the app. Guarded so that
    # re-verifying can never mint a second trial.
    #
    # Also bound to the signup device. Peptora is a one-time purchase, so an
    # account-scoped trial is a permanent free tier for anyone who notices
    # they can just sign up again with another address.
    #
    # Not granted to an account created in the iOS app. There the free trial
    # is the App Store subscription's introductory offer; see
    # TRIAL_FOR_IOS_SIGNUPS in app/config.py.
    platform = request.headers.get("X-Platform", "web").strip().lower()
    trial_granted = False
    if user.trial_ends_at is None:
        if platform == "ios" and not _settings.TRIAL_FOR_IOS_SIGNUPS:
            logger.info("trial_not_granted_ios_signup user_id=%s", user.id)
        elif await _may_grant_trial(db, user):
            user.trial_ends_at = now + timedelta(days=_settings.TRIAL_DAYS)
            trial_granted = True
        else:
            logger.info("trial_denied_device_reuse user_id=%s", user.id)
    db.add(AuditLog(
        user_id=user.id,
        action="email_verified",
        ip_hash=hash_ip(request.client.host if request.client else ""),
        platform=request.headers.get("X-Platform", "web"),
    ))

    try:
        await send_welcome_email(
            user.email, user.full_name,
            trial_days=_settings.TRIAL_DAYS if trial_granted else None,
        )
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
@limiter.limit("60/minute")
async def refresh_token(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """Exchange a refresh token for a new access token.

    The web app sends the refresh token as a cookie. The native app has no
    cookie jar it controls, so it sends the token it stored at login in the
    JSON body and reads the new access token from the response. Without that
    second path a native session ended fifteen minutes after login, when the
    first access token expired.
    """
    token = request.cookies.get("refresh_token")
    if not token:
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("refresh_token"), str):
            token = payload["refresh_token"]
    if not token:
        raise HTTPException(status_code=401, detail="No refresh token")
    user_id = decode_token(token, "refresh")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    # A refresh token outlives the account it was issued for by up to thirty
    # days. Without this check a deleted account's token would keep minting
    # access tokens (useless ones, but there is no reason to hand them out).
    try:
        exists = await db.execute(select(User.id).where(User.id == uuid.UUID(user_id)))
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    access = create_access_token(user_id)
    response.set_cookie("access_token", access, max_age=900, **COOKIE_OPTS)
    return {"message": "Token refreshed", "access_token": access}


async def _may_grant_trial(db: AsyncSession, user: User) -> bool:
    """Claim the trial for this user's signup device, once.

    The unique constraint on trial_grants.device_fingerprint is the arbiter
    rather than a read-then-write, which races between two simultaneous
    verifications on the same device.

    Fails OPEN when there is no usable fingerprint. Refusing a trial to
    everyone whose browser blocks the fingerprinting APIs would punish
    privacy-conscious users and Safari far more than it would punish anyone
    farming trials.

    Note this cannot be answered from TrialCounter: its `user_id` is UNIQUE and
    is reassigned when a second account registers on the same fingerprint, so
    it can never testify that a device already had a trial.
    """
    fp = user.signup_fingerprint
    if not fp:
        return True

    try:
        # A SAVEPOINT, not the outer transaction. A plain rollback here would
        # discard the whole verification — the OTP consumption, email_verified,
        # last_login — and the user would be told their code was invalid.
        async with db.begin_nested():
            db.add(TrialGrant(device_fingerprint=fp, user_id=user.id))
            await db.flush()
        return True
    except IntegrityError:
        # This device has already had its trial.
        return False


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
    # Must match the attributes used when setting, or the browser keeps the cookie.
    response.delete_cookie("access_token", **COOKIE_OPTS)
    response.delete_cookie("refresh_token", **COOKIE_OPTS)
    return {"message": "Logged out"}


@router.get("/me", response_model=UserResponse)
async def me(
    user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(User)
        .options(selectinload(User.trial_counter))
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

    from app.routers.billing import latest_claim_for
    from app.routers.subscriptions import access_info

    # The user's latest claim rides along so the paywall renders its real
    # state on first paint — pending, rejected or clear — instead of flashing
    # a submission form at someone who already paid an hour ago.
    claim = await latest_claim_for(db, u.id)
    from app.routers.iap import current_subscription_for
    subscription = await current_subscription_for(db, u.id)
    access = access_info(u, claim, subscription)

    return UserResponse(
        id=u.id, email=u.email, full_name=u.full_name,
        # Derived, not read from the column: `plan` is refreshed by a nightly
        # sweep and would otherwise show "pro" to a user whose window lapsed
        # this morning, or "free" to one who paid a minute ago.
        plan="pro" if access.has_access else "free",
        is_admin=u.is_admin, email_verified=u.email_verified,
        consent_accepted=u.consent_accepted,
        trial_count=trial_info, access=access,
    )


@router.post("/delete-account", response_model=DeleteAccountResponse)
@limiter.limit("5/minute")
async def delete_account(
    request: Request,
    body: DeleteAccountRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Permanently delete the signed-in account and everything stored for it.

    This is deletion, not deactivation: the user row and every row that
    belongs to it are removed in one transaction, and the email address can be
    registered again afterwards. Required by App Store guideline 5.1.1(v) for
    any app that lets people create an account, and it is the same endpoint
    the web app uses.

    What goes: profile, protocols, log entries, saved calculations, sessions,
    verification codes, push token, payment claims and their receipt images,
    and the App Store subscription link.

    What stays, with nothing pointing back at the person:
      * the device's trial_grants row, with user_id cleared, so deleting an
        account and signing up again does not mint a second free trial;
      * one audit row saying that *an* account was deleted, with no user id.

    An App Store subscription cannot be cancelled from here. Only the customer
    can do that, in their Apple ID settings, and the response says so when one
    is still running.
    """
    # 403, never 401: both clients treat a 401 as "the session is dead" and
    # sign the user out, which is the wrong reaction to a mistyped password.
    if user.password_hash and not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=403, detail="That password is not correct.")

    if user.is_admin:
        # Removing an administrator here could lock everyone out of the review
        # queue. Admin rights have to be handed over first.
        raise HTTPException(
            status_code=400,
            detail="Administrator accounts cannot be deleted in the app. Remove admin rights first.",
        )

    uid = user.id
    now = datetime.now(timezone.utc)
    had_lifetime = bool(user.lifetime_access_at)
    subscription_active = bool(user.apple_sub_until and user.apple_sub_until > now)

    # Receipt images are financial PII and live outside the database, so they
    # are removed first. Best-effort: an object that will not delete is logged
    # and the account deletion still goes ahead.
    receipt_keys = (await db.execute(
        select(PaymentClaim.receipt_key).where(
            PaymentClaim.user_id == uid, PaymentClaim.receipt_key.is_not(None)
        )
    )).scalars().all()
    if receipt_keys:
        from app.utils import storage
        for key in receipt_keys:
            try:
                storage.delete(key)
            except Exception:
                logger.warning("account_delete_receipt_failed user_id=%s", uid)

    # References to this user from rows that belong to someone else, or to
    # nobody. Cleared rather than deleted.
    await db.execute(update(PaymentClaim).where(PaymentClaim.reviewed_by == uid).values(reviewed_by=None))
    await db.execute(update(AppSettings).where(AppSettings.updated_by == uid).values(updated_by=None))
    await db.execute(update(TrialGrant).where(TrialGrant.user_id == uid).values(user_id=None))

    # Everything the user owns. Children before parents.
    await db.execute(delete(EmailVerificationOTP).where(EmailVerificationOTP.user_id == uid))
    await db.execute(delete(CycleLog).where(CycleLog.user_id == uid))
    await db.execute(delete(UserProtocol).where(UserProtocol.user_id == uid))
    await db.execute(delete(CalculatorUsage).where(CalculatorUsage.user_id == uid))
    # A session row is keyed by device, so another account on the same device
    # may have saved calculations pointing at one of this user's sessions.
    # Those calculations stay; only the pointer goes.
    await db.execute(
        update(CalculatorUsage)
        .where(CalculatorUsage.session_id.in_(select(DBSession.id).where(DBSession.user_id == uid)))
        .values(session_id=None)
    )
    await db.execute(delete(DBSession).where(DBSession.user_id == uid))
    await db.execute(delete(TrialCounter).where(TrialCounter.user_id == uid))
    await db.execute(delete(PaymentClaim).where(PaymentClaim.user_id == uid))
    await db.execute(delete(CryptoPayment).where(CryptoPayment.user_id == uid))
    await db.execute(delete(AppleSubscription).where(AppleSubscription.user_id == uid))
    await db.execute(delete(AuditLog).where(AuditLog.user_id == uid))
    await db.execute(delete(User).where(User.id == uid))

    db.add(AuditLog(
        user_id=None,
        action="account_deleted",
        extra_data={"had_lifetime": had_lifetime, "had_active_subscription": subscription_active},
        platform=request.headers.get("X-Platform", "web"),
    ))
    logger.info("account_deleted had_lifetime=%s had_subscription=%s", had_lifetime, subscription_active)

    response.delete_cookie("access_token", **COOKIE_OPTS)
    response.delete_cookie("refresh_token", **COOKIE_OPTS)
    return DeleteAccountResponse(
        message="Your account and its data have been deleted.",
        app_store_subscription_active=subscription_active,
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
        (entry for entry in logs if entry.extra_data and entry.extra_data.get("token") == body.token
         and datetime.fromisoformat(entry.extra_data["expires"]) > now),
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
