"""NOWPayments client — the only payment rail.

There is no card processor here, and that is not an implementation gap: crypto
has no pull payment, so nothing can debit a wallet on a schedule. NOWPayments'
own "Recurring Payments" product is really recurring *invoicing* — it emails
the customer a fresh link each period and waits for them to click it. That
buys us little and costs a lot: it needs a JWT minted from the dashboard
account password, it puts NOWPayments' branding on our renewal emails, and it
reconciles by their subscription id rather than our user id.

So we drive plain invoices ourselves (`create_invoice`) and own the renewal
reminders in app/routers/cron.py, where Resend and our own templates already
live. Access is a prepaid window on the user row; see has_access() in
app/middleware/auth.py.
"""

import hashlib
import hmac
import json
import logging
import uuid

import httpx

from app.config import settings

logger = logging.getLogger("peptora.payments")

# How far one payment moves `users.paid_until` forward.
PLAN_DAYS = {"monthly": 30, "annual": 365}


def plan_price(plan: str) -> float:
    return {
        "monthly": settings.PRICE_MONTHLY_USD,
        "annual": settings.PRICE_ANNUAL_USD,
    }[plan]


# Coins whose minimum payment amount reliably sits under the $5 monthly price.
# NOWPayments sets a per-currency minimum to cover network fees; on BTC and ETH
# mainnet that minimum routinely exceeds $5, so a monthly invoice in those
# coins is simply unpayable and the user hits a dead end at checkout. The
# annual plan clears every minimum, so it is left unrestricted.
LOW_FEE_CURRENCIES = ["usdttrc20", "trx", "ltc", "sol", "usdcsol", "maticmainnet", "bnbbsc"]

# Terminal-success. Only this status extends an access window: `confirmed`
# means the chain accepted it, but `finished` means it settled to our wallet.
STATUS_PAID = "finished"

# Money arrived but short of the invoice — NOWPayments holds it rather than
# refunding, and a human can top it up or release it from the dashboard.
STATUS_PARTIAL = "partially_paid"

# No money is coming; safe to let the row rest.
STATUSES_DEAD = {"failed", "refunded", "expired"}


class NowPaymentsError(RuntimeError):
    pass


def _headers() -> dict:
    if not settings.NOWPAYMENTS_API_KEY:
        raise NowPaymentsError("NOWPAYMENTS_API_KEY is not configured")
    return {
        "x-api-key": settings.NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json",
    }


def build_order_id(user_id: uuid.UUID, plan: str) -> str:
    """Our reconciliation key, echoed back on every callback.

    NOWPayments knows nothing about our accounts, so this is the only thread
    tying an IPN to a user. The nonce keeps it unique per invoice: a user who
    abandons checkout and starts again must not collide with their own earlier
    row, since order_id is unique.
    """
    return f"{user_id}:{plan}:{uuid.uuid4().hex[:12]}"


def parse_order_id(order_id: str) -> tuple[str, str] | None:
    """Split an order_id back into (user_id, plan), or None if malformed.

    Callbacks are attacker-reachable, so this never raises — a junk order_id
    is a payload to ignore, not a 500 that makes NOWPayments retry forever.
    """
    parts = (order_id or "").split(":")
    if len(parts) != 3:
        return None
    user_id, plan, _nonce = parts
    if plan not in PLAN_DAYS:
        return None
    try:
        uuid.UUID(user_id)
    except ValueError:
        return None
    return user_id, plan


async def create_invoice(*, order_id: str, plan: str, success_url: str, cancel_url: str) -> dict:
    """Create a hosted invoice and return the raw NOWPayments response.

    `is_fee_paid_by_user` adds the service and network fees on top of the price
    instead of taking them out of it — without it a $5 invoice settles as ~$4.90
    and lands in `partially_paid` rather than `finished`, so nobody is ever
    credited. It implies a fixed exchange rate, which is what we want anyway.
    """
    payload = {
        "price_amount": plan_price(plan),
        "price_currency": "usd",
        "order_id": order_id,
        "order_description": f"Peptora Pro — {plan}",
        "ipn_callback_url": f"{settings.API_PUBLIC_URL}/subscriptions/ipn",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "is_fee_paid_by_user": True,
    }
    # Only the $5 plan needs the currency whitelist; see LOW_FEE_CURRENCIES.
    if plan == "monthly":
        payload["pay_currency"] = None
        payload["available_currencies"] = LOW_FEE_CURRENCIES

    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.post(
            f"{settings.NOWPAYMENTS_API_URL}/invoice",
            headers=_headers(),
            json={k: v for k, v in payload.items() if v is not None},
        )

    if res.status_code >= 400:
        # Body may carry the real reason (bad key, price below minimum). Log it
        # for us; the caller turns this into a generic 502 for the user.
        logger.error("nowpayments invoice failed status=%s body=%s", res.status_code, res.text[:500])
        raise NowPaymentsError(f"NOWPayments returned {res.status_code}")

    return res.json()


def verify_ipn_signature(raw_body: bytes, signature: str) -> bool:
    """Check the `x-nowpayments-sig` header against the IPN secret.

    NOWPayments signs HMAC-SHA512 over the body re-serialised with its keys
    sorted and no whitespace — the shape Node's
    `JSON.stringify(params, Object.keys(params).sort())` produces. Python's
    json.dumps defaults to `(', ', ': ')` separators, which inserts spaces and
    makes every signature mismatch, so passing separators here is load-bearing.
    sort_keys sorts nested objects too, matching the replacer-array behaviour.
    """
    if not signature or not settings.NOWPAYMENTS_IPN_SECRET:
        return False
    try:
        parsed = json.loads(raw_body)
    except (ValueError, TypeError):
        return False

    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    expected = hmac.new(
        settings.NOWPAYMENTS_IPN_SECRET.encode(),
        canonical.encode(),
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
