"""End-to-end billing test against a real database and the real ASGI app.

This exists because the NOWPayments sandbox dashboard cannot be signed into
(its reCAPTCHA is misconfigured on their side), so their emulator is not
available to us. It turns out not to matter: everything the sandbox would have
exercised on our side of the wire is exercised here, and more thoroughly than
the sandbox allows — replayed callbacks, forged signatures and short payments
are all awkward to trigger there and trivial here.

What this does NOT cover is the one thing only NOWPayments can answer: whether
POST /v1/invoice accepts our payload and returns an invoice_url. That needs a
live API key.

Requires a Postgres. Start one with:
    docker run -d --name peptora-test -e POSTGRES_PASSWORD=test \\
        -e POSTGRES_DB=peptora -p 55433:5432 postgres:16-alpine
    export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55433/peptora

Run: pytest tests/test_ipn_integration.py -q
"""

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

TEST_DB = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set"),
]

IPN_SECRET = "integration-test-ipn-secret"


def sign(body: dict) -> tuple[bytes, str]:
    """Produce (raw_bytes, signature) the way NOWPayments does."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(IPN_SECRET.encode(), canonical.encode(), hashlib.sha512).hexdigest()
    return json.dumps(body).encode(), sig


@pytest_asyncio.fixture
async def env(monkeypatch):
    """A live app, a real schema, and one verified user whose trial has expired."""
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("JWT_SECRET", "integration-test-jwt-secret")

    from app.config import settings
    monkeypatch.setattr(settings, "NOWPAYMENTS_IPN_SECRET", IPN_SECRET)
    monkeypatch.setattr(settings, "NOWPAYMENTS_API_KEY", "test-key")

    from app.database import AsyncSessionLocal, Base, engine
    from app.main import app
    from app.models import CryptoPayment, User
    from app.utils.security import create_access_token

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(User(
            id=user_id,
            email=f"ipn-{user_id.hex[:8]}@example.com",
            password_hash="x",
            plan="free",
            email_verified=True,
            consent_accepted=True,
            # Trial already spent: the state a lapsed user is actually in.
            trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
            paid_until=None,
        ))
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("access_token", create_access_token(str(user_id)))
        yield {
            "client": client,
            "user_id": user_id,
            "session": AsyncSessionLocal,
            "User": User,
            "CryptoPayment": CryptoPayment,
        }

    await engine.dispose()


async def fetch_user(env):
    async with env["session"]() as db:
        result = await db.execute(select(env["User"]).where(env["User"].id == env["user_id"]))
        return result.scalar_one()


def ipn_payload(order_id: str, status: str = "finished", **kw) -> dict:
    return {
        "payment_id": kw.get("payment_id", 4455667788),
        "payment_status": status,
        "order_id": order_id,
        "price_amount": 49,
        "price_currency": "usd",
        "pay_currency": "usdttrc20",
        "actually_paid": kw.get("actually_paid", 49.0),
    }


async def post_ipn(client, payload, signature=None):
    raw, sig = sign(payload)
    return await client.post(
        "/subscriptions/ipn",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "x-nowpayments-sig": signature if signature is not None else sig,
        },
    )


# ── the whole loop ──────────────────────────────────────────────────────────

async def test_lapsed_user_is_locked_out_then_unlocked_by_a_paid_ipn(env):
    client = env["client"]
    order_id = f"{env['user_id']}:annual:{uuid.uuid4().hex[:12]}"

    # Locked out to begin with.
    assert (await client.get("/protocols")).status_code == 402
    assert (await client.get("/tracker/logs")).status_code == 402
    assert (await client.get("/calculator/history")).status_code == 402

    me = (await client.get("/auth/me")).json()
    assert me["plan"] == "free"
    assert me["access"]["has_access"] is False

    # The payment lands.
    assert (await post_ipn(client, ipn_payload(order_id))).status_code == 200

    user = await fetch_user(env)
    assert user.plan == "pro"
    assert user.paid_until is not None
    granted = (user.paid_until - datetime.now(timezone.utc)).days
    assert 364 <= granted <= 365, granted

    # And the tools open up.
    assert (await client.get("/protocols")).status_code == 200
    assert (await client.get("/tracker/logs")).status_code == 200
    assert (await client.get("/calculator/history")).status_code == 200

    me = (await client.get("/auth/me")).json()
    assert me["plan"] == "pro"
    assert me["access"]["has_access"] is True
    assert me["access"]["is_trial"] is False


async def test_replayed_callback_does_not_buy_a_second_year(env):
    """NOWPayments retries callbacks; a duplicate must be inert."""
    client = env["client"]
    order_id = f"{env['user_id']}:annual:{uuid.uuid4().hex[:12]}"
    payload = ipn_payload(order_id)

    assert (await post_ipn(client, payload)).status_code == 200
    first = (await fetch_user(env)).paid_until

    for _ in range(3):
        assert (await post_ipn(client, payload)).status_code == 200

    assert (await fetch_user(env)).paid_until == first


async def test_forged_signature_is_rejected_and_grants_nothing(env):
    client = env["client"]
    order_id = f"{env['user_id']}:annual:{uuid.uuid4().hex[:12]}"

    res = await post_ipn(client, ipn_payload(order_id), signature="deadbeef")
    assert res.status_code == 400

    user = await fetch_user(env)
    assert user.paid_until is None
    assert user.plan == "free"
    assert (await client.get("/protocols")).status_code == 402


async def test_partial_payment_grants_nothing(env):
    """Short payment: NOWPayments holds the funds, so we must not open the door."""
    client = env["client"]
    order_id = f"{env['user_id']}:annual:{uuid.uuid4().hex[:12]}"

    res = await post_ipn(
        client,
        ipn_payload(order_id, status="partially_paid", actually_paid=12.0),
    )
    assert res.status_code == 200

    user = await fetch_user(env)
    assert user.paid_until is None
    assert (await client.get("/protocols")).status_code == 402


async def test_non_terminal_statuses_grant_nothing(env):
    client = env["client"]
    for status in ("waiting", "confirming", "confirmed", "sending", "failed", "expired"):
        order_id = f"{env['user_id']}:annual:{uuid.uuid4().hex[:12]}"
        res = await post_ipn(client, ipn_payload(order_id, status=status, payment_id=abs(hash(status)) % 10**9))
        assert res.status_code == 200, status
        assert (await fetch_user(env)).paid_until is None, status


async def test_duplicate_payment_id_does_not_500(env):
    """Regression: a UNIQUE constraint here turned any collision into a 500,
    and a 500 makes NOWPayments retry that callback indefinitely."""
    client = env["client"]
    for _ in range(2):
        order_id = f"{env['user_id']}:annual:{uuid.uuid4().hex[:12]}"
        res = await post_ipn(client, ipn_payload(order_id, status="confirming", payment_id=999000111))
        assert res.status_code == 200


async def test_renewal_extends_from_the_existing_end_not_from_now(env):
    """Paying early must not burn the time already owned."""
    client = env["client"]

    first_order = f"{env['user_id']}:monthly:{uuid.uuid4().hex[:12]}"
    await post_ipn(client, ipn_payload(first_order, status="finished", payment_id=1))
    after_first = (await fetch_user(env)).paid_until

    second_order = f"{env['user_id']}:monthly:{uuid.uuid4().hex[:12]}"
    await post_ipn(client, ipn_payload(second_order, status="finished", payment_id=2))
    after_second = (await fetch_user(env)).paid_until

    # Stacked, not reset: two months from the first end, give or take a second.
    assert abs((after_second - after_first).days - 30) <= 1


async def test_ipn_for_an_unknown_order_id_is_ignored_not_crashed(env):
    """Callbacks are attacker-reachable; junk must not 500 into a retry loop."""
    client = env["client"]
    for bad in ["", "garbage", "not-a-uuid:annual:abc", f"{uuid.uuid4()}:lifetime:abc"]:
        res = await post_ipn(client, ipn_payload(bad))
        assert res.status_code == 200, bad
        assert (await fetch_user(env)).paid_until is None
