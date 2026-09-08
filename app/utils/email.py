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
        <p>That covers the peptide encyclopedia, the dose calculator, protocols
        and the cycle tracker.</p>
        <p>After 14 days, Peptora is a one-time purchase. Buy it once and it is
        yours — no subscription, nothing to cancel.</p>
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
    """Retired with the move to a one-time purchase — nothing renews any more.

    Kept, and only ever called when the crypto rail is switched back on, since
    that rail really does sell a period that expires.
    """
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
        <p><a href="{settings.WEB_URL}/app/billing">Renew →</a></p>
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
        <p>Your 14-day trial ends <strong>{when}</strong>. After that the
        encyclopedia, calculator, protocols and tracker are locked.</p>
        <p>Peptora is a <strong>one-time purchase</strong> — buy it once and it
        is yours. There is no subscription and nothing to cancel.</p>
        <p>Everything you have saved stays exactly where it is either way.</p>
        <p><a href="{settings.WEB_URL}/app/billing">Unlock Peptora →</a></p>
        <hr/>
        <small>For research and educational purposes only. Not medical advice.</small>
        """,
    })


# What each rejection code means to the person who receives it. Written as the
# next action rather than as a verdict — a rejected user with no next step
# files a refund request instead of a corrected claim.
REJECTION_COPY = {
    "amount_mismatch": "The amount received did not match the price. If you sent a different amount, reply and tell us what you sent.",
    "receipt_unreadable": "We could not read the receipt you uploaded. A clearer screenshot showing the amount, date and reference should do it.",
    "reference_not_found": "We could not find that reference on our side. Please double-check the transaction ID and submit it again.",
    "duplicate_claim": "This looks like a duplicate of a payment we have already handled.",
    "not_received": "We have not seen this payment arrive yet. Bank transfers can take a few days — please resubmit once it has cleared.",
    "other": "We could not verify this payment from the details provided.",
}


async def send_claim_received_email(to_email: str, full_name: str | None, sla_hours: int) -> None:
    name = full_name or "Researcher"
    window = "one business day" if sla_hours >= 24 else f"{sla_hours} hours"
    await _send_email({
        "from": settings.FROM_EMAIL,
        "to": to_email,
        "subject": "We have your payment details",
        "html": f"""
        <h2>Thanks, {name} — we are checking your payment.</h2>
        <p>Every payment is verified by a person, so this is not instant. We
        usually get through them within <strong>{window}</strong>.</p>
        <p>You do not need to do anything else. We will email you the moment
        your licence is active, and you can check the status any time.</p>
        <p><a href="{settings.WEB_URL}/app/billing">Check status →</a></p>
        <hr/>
        <small>For research and educational purposes only. Not medical advice.</small>
        """,
    })


async def send_licence_active_email(to_email: str, full_name: str | None) -> None:
    name = full_name or "Researcher"
    await _send_email({
        "from": settings.FROM_EMAIL,
        "to": to_email,
        "subject": "Your Peptora licence is active",
        "html": f"""
        <h2>You are in, {name}.</h2>
        <p>Your payment checked out and every Peptora tool is unlocked.</p>
        <p>This is a <strong>one-time purchase</strong>. There is no expiry
        date, no renewal and nothing to cancel — it does not lapse.</p>
        <p><a href="{settings.WEB_URL}/app/home">Open Peptora →</a></p>
        <hr/>
        <small>For research and educational purposes only. Not medical advice.</small>
        """,
    })


async def send_claim_rejected_email(
    to_email: str, full_name: str | None, reason: str, note: str | None = None,
) -> None:
    name = full_name or "Researcher"
    explanation = REJECTION_COPY.get(reason, REJECTION_COPY["other"])
    extra = f"<p>{note}</p>" if note else ""
    await _send_email({
        "from": settings.FROM_EMAIL,
        "to": to_email,
        "subject": "We could not verify your Peptora payment",
        "html": f"""
        <h2>Hi {name},</h2>
        <p>We were not able to confirm your payment yet.</p>
        <p><strong>{explanation}</strong></p>
        {extra}
        <p>Nothing is lost — you can correct the details and submit again, and
        we will take another look.</p>
        <p><a href="{settings.WEB_URL}/app/billing">Submit corrected details →</a></p>
        <hr/>
        <small>For research and educational purposes only. Not medical advice.</small>
        """,
    })


async def send_new_claim_admin_email(
    to_email: str, user_email: str, amount, currency: str, reference: str | None,
) -> None:
    """Tells the reviewer a claim is waiting.

    Without this, the first hour of every wait is spent not knowing there is
    anything to review.
    """
    amount_str = f"{currency} {amount}" if amount is not None else "not stated"
    await _send_email({
        "from": settings.FROM_EMAIL,
        "to": to_email,
        "subject": f"New Peptora payment to review — {user_email}",
        "html": f"""
        <h2>A payment is waiting for review.</h2>
        <ul>
          <li><strong>Account:</strong> {user_email}</li>
          <li><strong>Amount claimed:</strong> {amount_str}</li>
          <li><strong>Reference:</strong> {reference or "not given"}</li>
        </ul>
        <p><a href="{settings.ADMIN_URL}/claims">Open the review queue →</a></p>
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
