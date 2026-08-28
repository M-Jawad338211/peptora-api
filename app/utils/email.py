import asyncio
import logging
import resend
from app.config import settings

resend.api_key = settings.RESEND_API_KEY
logger = logging.getLogger("peptora.email")


async def _send_email(payload: dict) -> dict:
    if not settings.RESEND_API_KEY:
        # A local checkout has no Resend key, and registration refuses to
        # complete when the verification mail cannot be sent — which left no
        # way to create an account at all. In development the message is
        # written to the log instead, so the OTP is readable in the server
        # output. Anywhere else this stays a hard failure: silently dropping a
        # password-reset or verification mail is worse than a 502.
        if settings.is_development:
            logger.warning(
                "RESEND_API_KEY not set — email NOT sent. to=%s subject=%s\n%s",
                payload.get("to"),
                payload.get("subject"),
                payload.get("html", ""),
            )
            return {"id": "dev-not-sent"}
        raise RuntimeError("RESEND_API_KEY is not configured")
    if not settings.FROM_EMAIL:
        raise RuntimeError("FROM_EMAIL is not configured")

    # Resend v2 requires `to` as a list
    if isinstance(payload.get("to"), str):
        payload = {**payload, "to": [payload["to"]]}

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
        <p>Your 14-day free trial has started — every tool is unlocked, with no
        payment details required.</p>
        <p>That covers the dose calculator, protocols and the cycle tracker.</p>
        <p><a href="{settings.WEB_URL}/app/home">Open Peptora →</a></p>
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


async def send_subscription_active_email(to_email: str, full_name: str | None, paid_until) -> None:
    name = full_name or "Researcher"
    until = paid_until.strftime("%d %B %Y")
    await _send_email({
        "from": settings.FROM_EMAIL,
        "to": to_email,
        "subject": "Your Peptora subscription is active",
        "html": f"""
        <h2>Payment received — thanks, {name}.</h2>
        <p>Every Peptora tool is unlocked until <strong>{until}</strong>.</p>
        <p>Because payment is in crypto there is nothing stored to charge again,
        so nothing renews automatically. We'll email you a few days before
        {until} with a link if you want to continue.</p>
        <p><a href="{settings.WEB_URL}/app/home">Open Peptora →</a></p>
        <hr/>
        <small>For research and educational purposes only. Not medical advice.</small>
        """,
    })


async def send_renewal_reminder_email(to_email: str, full_name: str | None, days_left: int) -> None:
    name = full_name or "Researcher"
    when = "today" if days_left <= 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
    await _send_email({
        "from": settings.FROM_EMAIL,
        "to": to_email,
        "subject": f"Your Peptora access ends {when}",
        "html": f"""
        <h2>Hi {name},</h2>
        <p>Your Peptora access ends <strong>{when}</strong>. Nothing renews on its
        own — crypto payments can't be charged automatically — so you'll need to
        renew manually if you'd like to keep going.</p>
        <p>Your protocols and dose history stay exactly where they are either way.</p>
        <p><a href="{settings.WEB_URL}/app/pricing">Renew →</a></p>
        <hr/>
        <small>For research and educational purposes only. Not medical advice.</small>
        """,
    })


async def send_trial_ending_email(to_email: str, full_name: str | None, days_left: int) -> None:
    name = full_name or "Researcher"
    when = "today" if days_left <= 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
    await _send_email({
        "from": settings.FROM_EMAIL,
        "to": to_email,
        "subject": f"Your Peptora trial ends {when}",
        "html": f"""
        <h2>Hi {name},</h2>
        <p>Your 14-day trial ends <strong>{when}</strong>. To keep the calculator,
        protocols and tracker, pick a plan — $5 a month or $49 a year, paid in crypto.</p>
        <p>Everything you've saved stays put whether or not you subscribe.</p>
        <p><a href="{settings.WEB_URL}/app/pricing">Choose a plan →</a></p>
        <hr/>
        <small>For research and educational purposes only. Not medical advice.</small>
        """,
    })


async def send_password_reset_email(to_email: str, reset_token: str) -> None:
    reset_url = f"{settings.WEB_URL}/auth/reset-password?token={reset_token}"
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
