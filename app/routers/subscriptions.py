"""Crypto billing.

Kept under the /subscriptions prefix, and checkout still returns a field named
`checkout_url`, so peptora-android/app/paywall.js keeps working unchanged
across the Stripe → NOWPayments switch.

There is deliberately no /cancel and no /portal. Access is prepaid: not paying
again *is* cancelling, and there is no stored payment method to manage.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware.auth import get_current_verified_user, has_access
from app.middleware.rate_limit import limiter
from app.models import AuditLog, CryptoPayment, User
from app.schemas import (
    AccessInfo,
    CheckoutResponse,
    CreateCheckoutRequest,
    PlanOption,
    SubscriptionStatusResponse,
)
from app.utils import nowpayments as np

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])
logger = logging.getLogger("peptora.payments")


def _plan_options() -> list[PlanOption]:
    return [
        PlanOption(id="monthly", label="Monthly", price_usd=settings.PRICE_MONTHLY_USD, days=np.PLAN_DAYS["monthly"]),
        PlanOption(id="annual", label="Annual", price_usd=settings.PRICE_ANNUAL_USD, days=np.PLAN_DAYS["annual"]),
    ]


def access_info(user: User) -> AccessInfo:
    """Flatten the two access windows into what the client renders."""
    now = datetime.now(timezone.utc)
    paid_live = bool(user.paid_until and user.paid_until > now)
    trial_live = bool(user.trial_ends_at and user.trial_ends_at > now)
    ends = user.paid_until if paid_live else (user.trial_ends_at if trial_live else None)
    return AccessInfo(
        has_access=paid_live or trial_live,
        # Only call it a trial when the trial is the *only* thing granting
        # access — a paid user still inside their trial window should not see
        # a countdown banner telling them their trial is ending.
        is_trial=trial_live and not paid_live,
        trial_ends_at=user.trial_ends_at,
        paid_until=user.paid_until,
        days_remaining=max(0, (ends - now).days) if ends else None,
    )


@router.post("/create-checkout", response_model=CheckoutResponse)
@limiter.limit("10/minute")
async def create_checkout(
    request: Request,
    body: CreateCheckoutRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_verified_user),
):
    if not settings.payments_configured:
        raise HTTPException(status_code=503, detail="Payments are not available right now")

    order_id = np.build_order_id(user.id, body.plan)

    try:
        invoice = await np.create_invoice(
            order_id=order_id,
            plan=body.plan,
            success_url=f"{settings.WEB_URL}/app/profile?checkout=success",
            cancel_url=f"{settings.WEB_URL}/app/pricing?checkout=cancelled",
        )
    except np.NowPaymentsError:
        # Already logged with the upstream body; don't leak it to the client.
        raise HTTPException(status_code=502, detail="Could not start checkout. Please try again.")

    invoice_url = invoice.get("invoice_url")
    if not invoice_url:
        logger.error("nowpayments invoice missing invoice_url order_id=%s", order_id)
        raise HTTPException(status_code=502, detail="Could not start checkout. Please try again.")

    # Recorded before the user leaves so an IPN can never arrive for a row we
    # do not have. The row stays "waiting" forever if they abandon checkout,
    # which is the honest record of what happened.
    db.add(CryptoPayment(
        user_id=user.id,
        order_id=order_id,
        plan=body.plan,
        np_invoice_id=str(invoice.get("id")) if invoice.get("id") is not None else None,
        price_amount=np.plan_price(body.plan),
        price_currency="usd",
        status="waiting",
    ))
    logger.info("checkout_created user_id=%s plan=%s order_id=%s", user.id, body.plan, order_id)
    return CheckoutResponse(checkout_url=invoice_url, order_id=order_id)


@router.post("/ipn")
async def nowpayments_ipn(request: Request, db: AsyncSession = Depends(get_db)):
    """Payment callback from NOWPayments.

    Unauthenticated and internet-reachable, so the HMAC is the only thing
    standing between an attacker and free access. Everything here returns 200
    unless we genuinely failed to process a *valid* callback: NOWPayments
    retries non-2xx, and retrying a malformed or forged payload forever
    achieves nothing.
    """
    raw = await request.body()
    signature = request.headers.get("x-nowpayments-sig", "")

    if not np.verify_ipn_signature(raw, signature):
        # 400, not 200: a signature mismatch is either an attack or a
        # misconfigured IPN secret, and both should be loud in the logs.
        logger.warning("ipn_signature_rejected bytes=%d", len(raw))
        return Response(status_code=400)

    payload = await request.json()
    order_id = payload.get("order_id") or ""
    status = payload.get("payment_status") or ""
    payment_id = str(payload.get("payment_id")) if payload.get("payment_id") is not None else None

    parsed = np.parse_order_id(order_id)
    if not parsed:
        logger.warning("ipn_bad_order_id order_id=%r", order_id[:80])
        return Response(status_code=200)
    user_id, plan = parsed

    result = await db.execute(select(CryptoPayment).where(CryptoPayment.order_id == order_id))
    payment = result.scalar_one_or_none()
    if not payment:
        # Signature was valid, so this is ours — most likely a row lost to a
        # failed write during checkout. Rebuild it from the callback rather
        # than dropping a real payment on the floor.
        logger.warning("ipn_unknown_order recreating order_id=%s", order_id)
        payment = CryptoPayment(
            user_id=user_id,
            order_id=order_id,
            plan=plan,
            price_amount=np.plan_price(plan),
            price_currency="usd",
            status=status,
        )
        db.add(payment)
        await db.flush()

    payment.status = status
    payment.np_payment_id = payment_id or payment.np_payment_id
    payment.pay_currency = payload.get("pay_currency") or payment.pay_currency
    payment.pay_amount = payload.get("pay_amount") or payment.pay_amount
    payment.actually_paid = payload.get("actually_paid") or payment.actually_paid
    payment.raw_ipn = payload

    if status != np.STATUS_PAID:
        # partially_paid included: NOWPayments holds those funds and a human
        # releases them from the dashboard, which fires a second callback with
        # `finished`. Crediting here would hand out access for a short payment.
        logger.info("ipn_recorded order_id=%s status=%s (no credit)", order_id, status)
        return Response(status_code=200)

    if payment.credited_at is not None:
        # NOWPayments retries callbacks, and a duplicate `finished` must not
        # buy a second period.
        logger.info("ipn_duplicate_ignored order_id=%s", order_id)
        return Response(status_code=200)

    user_result = await db.execute(select(User).where(User.id == payment.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        logger.error("ipn_user_missing order_id=%s user_id=%s", order_id, payment.user_id)
        return Response(status_code=200)

    now = datetime.now(timezone.utc)
    # Extend from whichever window runs longest, never from `now`. Renewing
    # early would otherwise burn the time already owned, and paying mid-trial
    # would cut the trial short — both feel like theft to the user.
    base = max(
        user.paid_until or now,
        user.trial_ends_at or now,
        now,
    )
    user.paid_until = base + timedelta(days=np.PLAN_DAYS[payment.plan])
    user.plan = "pro"
    payment.credited_at = now

    db.add(AuditLog(user_id=user.id, action=f"subscription_paid_{payment.plan}"))
    logger.info(
        "ipn_credited user_id=%s plan=%s paid_until=%s order_id=%s",
        user.id, payment.plan, user.paid_until.isoformat(), order_id,
    )

    try:
        from app.utils.email import send_subscription_active_email
        await send_subscription_active_email(user.email, user.full_name, user.paid_until)
    except Exception:
        logger.exception("Failed to send subscription confirmation to %s", user.email)

    return Response(status_code=200)


@router.get("/status", response_model=SubscriptionStatusResponse)
async def subscription_status(user: User = Depends(get_current_verified_user)):
    return SubscriptionStatusResponse(
        plan="pro" if has_access(user) else "free",
        access=access_info(user),
        plans=_plan_options(),
        payments_enabled=settings.payments_configured,
    )
