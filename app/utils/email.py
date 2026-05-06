import asyncio
import logging
import resend
from app.config import settings

resend.api_key = settings.RESEND_API_KEY
logger = logging.getLogger("peptora.email")


async def _send_email(payload: dict) -> dict:
    if not settings.RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY is not configured")
    if not settings.FROM_EMAIL:
        raise RuntimeError("FROM_EMAIL is not configured")

    response = await asyncio.to_thread(resend.Emails.send, payload)
    logger.info(
        "Resend email queued to=%s subject=%s response=%s",
        payload.get("to"),
        payload.get("subject"),
        response,
    )
    return response


async def send_welcome_email(to_email: str, full_name: str | None) -> None:
    name = full_name or "Researcher"
    await _send_email({
        "from": settings.FROM_EMAIL,
        "to": to_email,
        "subject": "Welcome to Peptora",
        "html": f"""
        <h2>Welcome to Peptora, {name}!</h2>
        <p>Your free account is ready. You have 25 free calculator uses to start.</p>
        <p>Upgrade to Pro for unlimited calculations, AI assistant, stack checker, and more.</p>
        <p><a href="{settings.FRONTEND_URL}/pricing">View Pro plans →</a></p>
        <hr/>
        <small>For research and educational purposes only. Not medical advice.</small>
        """,
    })


async def send_email_verification_otp(to_email: str, full_name: str | None, otp: str) -> None:
    name = full_name or "Researcher"
    await _send_email({
        "from": settings.FROM_EMAIL,
        "to": to_email,
        "subject": "Verify your Peptora email",
        "html": f"""
        <h2>Verify your email, {name}</h2>
        <p>Use this one-time code to finish creating your Peptora account:</p>
        <p style="font-size:28px;font-weight:700;letter-spacing:6px;margin:24px 0;">{otp}</p>
        <p>This code expires in 10 minutes.</p>
        <p>If you did not request this, you can safely ignore this email.</p>
        <hr/>
        <small>For research and educational purposes only. Not medical advice.</small>
        """,
    })


async def send_pro_welcome_email(to_email: str, full_name: str | None) -> None:
    name = full_name or "Researcher"
    await _send_email({
        "from": settings.FROM_EMAIL,
        "to": to_email,
        "subject": "You're now on Peptora Pro",
        "html": f"""
        <h2>Welcome to Peptora Pro, {name}!</h2>
        <p>You now have unlimited calculator uses, AI research assistant, stack checker, and cycle tracker.</p>
        <p><a href="{settings.FRONTEND_URL}/dashboard">Go to your dashboard →</a></p>
        """,
    })


async def send_payment_failed_email(to_email: str) -> None:
    await _send_email({
        "from": settings.FROM_EMAIL,
        "to": to_email,
        "subject": "Action required: Payment failed for Peptora Pro",
        "html": f"""
        <h2>Your Peptora Pro payment failed</h2>
        <p>Please update your payment method to keep Pro access.</p>
        <p><a href="{settings.FRONTEND_URL}/dashboard">Manage billing →</a></p>
        """,
    })


async def send_cancellation_email(to_email: str) -> None:
    await _send_email({
        "from": settings.FROM_EMAIL,
        "to": to_email,
        "subject": "Your Peptora Pro subscription has been cancelled",
        "html": f"""
        <h2>Subscription cancelled</h2>
        <p>Your Pro access will remain active until the end of your billing period.</p>
        <p>You can resubscribe anytime at <a href="{settings.FRONTEND_URL}/pricing">peptora.app/pricing</a>.</p>
        """,
    })


async def send_password_reset_email(to_email: str, reset_token: str) -> None:
    reset_url = f"{settings.FRONTEND_URL}/auth/reset-password?token={reset_token}"
    await _send_email({
        "from": settings.FROM_EMAIL,
        "to": to_email,
        "subject": "Reset your Peptora password",
        "html": f"""
        <h2>Password Reset</h2>
        <p>Click the link below to reset your password. This link expires in 1 hour.</p>
        <p><a href="{reset_url}">Reset password →</a></p>
        <p>If you did not request this, you can safely ignore this email.</p>
        """,
    })
