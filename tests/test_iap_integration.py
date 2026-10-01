"""End-to-end: App Store purchases, the public library, and account deletion.

Against a real database and the real ASGI app, with a stand-in App Store that
signs payloads from a private test CA (see tests/apple_fakes.py). Everything
after the certificate root is the production code path.

Requires a Postgres:
    export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5432/peptora_test
    export DATABASE_URL=postgresql://postgres:test@localhost:5432/peptora_test

Run: pytest tests/test_iap_integration.py -q
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from tests.apple_fakes import MONTHLY, YEARLY, FakeAppStore

TEST_DB = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set"),
]

PASSWORD = "correct horse battery staple"
STORE = FakeAppStore()


def now():
    return datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def env(monkeypatch):
    """A live app, a clean schema, and two verified users with spent trials."""
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("JWT_SECRET", "integration-test-jwt-secret")

    from app.utils import apple_iap
    apple_iap.reset_caches()
    monkeypatch.setattr(apple_iap, "_root_certificates", lambda: (STORE.root_der,))
    apple_iap._verifier.cache_clear()

    from app import models
    from app.database import AsyncSessionLocal, Base, engine
    from app.main import app
    from app.middleware.rate_limit import limiter
    from app.utils.security import create_access_token, create_refresh_token, hash_password

    # One shared in-memory bucket per client IP would otherwise carry over
    # between tests and turn an unrelated assertion into a 429.
    limiter.reset()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    password_hash = hash_password(PASSWORD)
    user_id, other_id = uuid.uuid4(), uuid.uuid4()
    async with AsyncSessionLocal() as db:
        for uid, name in ((user_id, "user"), (other_id, "other")):
            db.add(models.User(
                id=uid,
                email=f"{name}-{uid.hex[:8]}@example.com",
                password_hash=password_hash,
                full_name=name.title(),
                plan="free",
                email_verified=True,
                consent_accepted=True,
                # Trial already spent: the state in which the paywall matters.
                trial_ends_at=now() - timedelta(days=1),
            ))
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client, \
            AsyncClient(transport=transport, base_url="http://test") as other, \
            AsyncClient(transport=transport, base_url="http://test") as anonymous:
        # The native app authenticates with a Bearer token, not a cookie.
        client.headers["Authorization"] = f"Bearer {create_access_token(str(user_id))}"
        other.headers["Authorization"] = f"Bearer {create_access_token(str(other_id))}"
        yield {
            "client": client,
            "other": other,
            "anonymous": anonymous,
            "user_id": user_id,
            "other_id": other_id,
            "refresh_token": create_refresh_token(str(user_id)),
            "session": AsyncSessionLocal,
            "models": models,
        }

    apple_iap.reset_caches()
    await engine.dispose()


async def fetch_user(env, user_id=None):
    User = env["models"].User
    async with env["session"]() as db:
        result = await db.execute(select(User).where(User.id == (user_id or env["user_id"])))
        return result.scalar_one_or_none()


async def count(env, model, *where):
    async with env["session"]() as db:
        stmt = select(func.count()).select_from(model)
        for cond in where:
            stmt = stmt.where(cond)
        return (await db.execute(stmt)).scalar_one()


async def verify(client, *transactions):
    return await client.post("/iap/apple/verify", json={"transactions": list(transactions)})


async def notify(client, signed_payload):
    return await client.post("/iap/apple/notifications", json={"signedPayload": signed_payload})


# ── the library is public ───────────────────────────────────────────────────

async def test_library_is_readable_without_an_account(env):
    """App Review rejected the sign-up wall (5.1.1(v)). Reference content must
    load for someone who has never registered."""
    anonymous = env["anonymous"]
    assert (await anonymous.get("/peptides")).status_code == 200
    assert (await anonymous.get("/stacks")).status_code == 200
    assert (await anonymous.get("/peptides/does-not-exist")).status_code == 404
    assert (await anonymous.get("/stacks/does-not-exist")).status_code == 404


async def test_ai_endpoints_are_not_registered(env):
    """No Peptora client has an AI feature and the privacy policy says nothing
    is sent to an AI provider, so the legacy routes must not exist by default."""
    client = env["client"]
    assert (await client.post("/ai/assistant", json={"message": "hi", "conversation_history": []})).status_code == 404
    assert (await client.post("/ai/stack-check", json={"peptides": ["a", "b"]})).status_code == 404


async def test_saved_data_still_needs_an_account_and_pro(env):
    anonymous, client = env["anonymous"], env["client"]
    assert (await anonymous.get("/protocols")).status_code == 401
    assert (await anonymous.post("/iap/apple/verify", json={"transactions": ["x"]})).status_code == 401
    # Signed in, trial spent, nothing bought.
    assert (await client.get("/protocols")).status_code == 402
    assert (await client.get("/tracker/logs")).status_code == 402
    assert (await client.get("/calculator/history")).status_code == 402


# ── buying ──────────────────────────────────────────────────────────────────

async def test_purchase_unlocks_pro(env):
    client = env["client"]
    expires = now() + timedelta(days=30)

    res = await verify(client, STORE.transaction(
        product_id=MONTHLY, expires=expires, app_account_token=str(env["user_id"]),
    ))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["results"][0]["status"] == "active"
    assert body["results"][0]["product_id"] == MONTHLY
    assert body["access"]["has_access"] is True
    assert body["access"]["is_subscription"] is True
    assert body["access"]["is_trial"] is False

    assert (await client.get("/protocols")).status_code == 200
    assert (await client.get("/tracker/logs")).status_code == 200

    me = (await client.get("/auth/me")).json()
    assert me["plan"] == "pro"
    assert me["access"]["subscription_product"] == MONTHLY
    assert me["access"]["subscription_store"] == "app_store"

    user = await fetch_user(env)
    assert user.plan == "pro"
    assert abs((user.apple_sub_until - expires).total_seconds()) < 1


async def test_free_trial_week_is_reported(env):
    client = env["client"]
    res = await verify(client, STORE.transaction(
        product_id=YEARLY, expires=now() + timedelta(days=7), free_trial=True,
    ))
    access = res.json()["access"]
    assert access["has_access"] is True
    assert access["subscription_is_trial"] is True
    # Apple's introductory week is not the account's own 14-day trial.
    assert access["is_trial"] is False


async def test_posting_the_same_transaction_again_changes_nothing(env):
    client = env["client"]
    signed = STORE.transaction(expires=now() + timedelta(days=30))
    await verify(client, signed)
    first = (await fetch_user(env)).apple_sub_until
    for _ in range(3):
        assert (await verify(client, signed)).status_code == 200
    assert (await fetch_user(env)).apple_sub_until == first
    assert await count(env, env["models"].AppleSubscription) == 1


async def test_forged_transaction_grants_nothing(env):
    client = env["client"]
    forger = FakeAppStore()  # a different certificate authority
    res = await verify(client, forger.transaction(expires=now() + timedelta(days=3650)))
    assert res.status_code == 200
    assert res.json()["results"][0]["status"] == "invalid"
    assert res.json()["access"]["has_access"] is False
    assert (await client.get("/protocols")).status_code == 402
    assert await count(env, env["models"].AppleSubscription) == 0


async def test_expired_transaction_grants_nothing(env):
    client = env["client"]
    res = await verify(client, STORE.transaction(
        purchased=now() - timedelta(days=40), expires=now() - timedelta(days=10),
    ))
    assert res.json()["results"][0]["status"] == "expired"
    assert res.json()["access"]["has_access"] is False
    assert (await client.get("/protocols")).status_code == 402


async def test_one_bad_transaction_does_not_spoil_a_good_one(env):
    """Restore posts several at once; a junk entry must not sink the batch."""
    client = env["client"]
    res = await verify(
        client,
        "not-a-transaction",
        STORE.transaction(expires=now() + timedelta(days=30)),
    )
    statuses = [r["status"] for r in res.json()["results"]]
    assert statuses == ["invalid", "active"]
    assert res.json()["access"]["has_access"] is True


async def test_request_bounds(env):
    client = env["client"]
    assert (await client.post("/iap/apple/verify", json={"transactions": []})).status_code == 422
    assert (await client.post("/iap/apple/verify", json={"transactions": ["x"] * 11})).status_code == 422
    assert (await client.post("/iap/apple/verify", json={"transactions": ["x" * 20_001]})).status_code == 422


# ── restore: the subscription follows the Apple ID ──────────────────────────

async def test_restore_moves_the_subscription_to_the_account_that_presents_it(env):
    client, other = env["client"], env["other"]
    signed = STORE.transaction(expires=now() + timedelta(days=30), app_account_token=str(env["user_id"]))

    await verify(client, signed)
    assert (await client.get("/protocols")).status_code == 200

    # Same Apple ID, now signed in to a different Peptora account.
    res = await verify(other, signed)
    assert res.json()["access"]["has_access"] is True
    assert (await other.get("/protocols")).status_code == 200

    # One subscription pays for one account at a time.
    assert (await client.get("/protocols")).status_code == 402
    assert (await fetch_user(env)).apple_sub_until is None
    assert await count(env, env["models"].AppleSubscription) == 1


# ── notifications from Apple ────────────────────────────────────────────────

async def test_renewal_notification_extends_access(env):
    client, anonymous = env["client"], env["anonymous"]
    first_end = now() + timedelta(days=1)
    await verify(client, STORE.transaction(expires=first_end))

    renewed_end = first_end + timedelta(days=30)
    res = await notify(anonymous, STORE.notification(
        "DID_RENEW",
        transaction=STORE.transaction(transaction_id="2000000000000002", expires=renewed_end),
        renewal=STORE.renewal_info(auto_renew=True),
    ))
    assert res.status_code == 200

    user = await fetch_user(env)
    assert abs((user.apple_sub_until - renewed_end).total_seconds()) < 1
    me = (await client.get("/auth/me")).json()
    assert me["access"]["subscription_auto_renew"] is True


async def test_refund_notification_ends_access(env):
    client, anonymous = env["client"], env["anonymous"]
    expires = now() + timedelta(days=30)
    await verify(client, STORE.transaction(expires=expires))
    assert (await client.get("/protocols")).status_code == 200

    res = await notify(anonymous, STORE.notification(
        "REFUND", transaction=STORE.transaction(expires=expires, revoked=now()),
    ))
    assert res.status_code == 200
    assert (await client.get("/protocols")).status_code == 402
    user = await fetch_user(env)
    assert user.apple_sub_until is None
    assert user.plan == "free"

    # The device still holds the transaction from before the refund. Sending
    # it again must not bring the access back.
    await verify(client, STORE.transaction(expires=expires))
    assert (await client.get("/protocols")).status_code == 402


async def test_access_ends_when_the_paid_period_runs_out(env):
    """Nothing has to happen for a lapsed subscription to lock: has_access()
    compares the stored expiry with the clock on every request."""
    import asyncio

    client, anonymous = env["client"], env["anonymous"]
    purchased = now() - timedelta(days=30)
    expires = now() + timedelta(seconds=2)
    await verify(client, STORE.transaction(purchased=purchased, expires=expires))
    assert (await client.get("/protocols")).status_code == 200

    await asyncio.sleep(2.5)
    assert (await client.get("/protocols")).status_code == 402

    # Apple's EXPIRED notification then arrives, carrying that same final
    # transaction. It has nothing left to change except the renewal flag.
    res = await notify(anonymous, STORE.notification(
        "EXPIRED", subtype="VOLUNTARY",
        transaction=STORE.transaction(purchased=purchased, expires=expires),
        renewal=STORE.renewal_info(auto_renew=False),
    ))
    assert res.status_code == 200
    assert (await client.get("/protocols")).status_code == 402
    me = (await client.get("/auth/me")).json()
    assert me["access"]["has_access"] is False
    assert me["access"]["is_subscription"] is False
    assert me["plan"] == "free"


async def test_billing_grace_period_keeps_access_after_a_failed_renewal(env):
    """Only when Billing Grace Period is enabled in App Store Connect."""
    import asyncio

    client, anonymous = env["client"], env["anonymous"]
    purchased = now() - timedelta(days=30)
    expires = now() + timedelta(seconds=2)
    signed = STORE.transaction(purchased=purchased, expires=expires)
    await verify(client, signed)

    res = await notify(anonymous, STORE.notification(
        "DID_FAIL_TO_RENEW", subtype="GRACE_PERIOD",
        transaction=signed,
        renewal=STORE.renewal_info(grace_expires=now() + timedelta(days=16)),
    ))
    assert res.status_code == 200

    await asyncio.sleep(2.5)
    assert (await client.get("/protocols")).status_code == 200

    # The grace period runs out without a successful charge.
    res = await notify(anonymous, STORE.notification(
        "GRACE_PERIOD_EXPIRED", transaction=signed, renewal=STORE.renewal_info(auto_renew=True),
    ))
    assert res.status_code == 200
    assert (await client.get("/protocols")).status_code == 402


async def test_cancelling_auto_renew_keeps_access_until_the_period_ends(env):
    client, anonymous = env["client"], env["anonymous"]
    expires = now() + timedelta(days=20)
    await verify(client, STORE.transaction(expires=expires))

    res = await notify(anonymous, STORE.notification(
        "DID_CHANGE_RENEWAL_STATUS", subtype="AUTO_RENEW_DISABLED",
        transaction=STORE.transaction(expires=expires),
        renewal=STORE.renewal_info(auto_renew=False),
    ))
    assert res.status_code == 200
    assert (await client.get("/protocols")).status_code == 200
    me = (await client.get("/auth/me")).json()
    assert me["access"]["has_access"] is True
    assert me["access"]["subscription_auto_renew"] is False


async def test_notification_arriving_before_the_app_links_by_account_token(env):
    """The purchase went through but the app never managed to post it."""
    client, anonymous = env["client"], env["anonymous"]
    assert (await client.get("/protocols")).status_code == 402

    res = await notify(anonymous, STORE.notification(
        "SUBSCRIBED", subtype="INITIAL_BUY",
        transaction=STORE.transaction(
            expires=now() + timedelta(days=30), app_account_token=str(env["user_id"]),
        ),
        renewal=STORE.renewal_info(),
    ))
    assert res.status_code == 200
    assert (await client.get("/protocols")).status_code == 200


async def test_notification_for_an_unknown_account_is_kept_for_a_later_restore(env):
    client, anonymous = env["client"], env["anonymous"]
    signed = STORE.transaction(expires=now() + timedelta(days=30), app_account_token=str(uuid.uuid4()))

    assert (await notify(anonymous, STORE.notification("SUBSCRIBED", transaction=signed))).status_code == 200
    assert await count(env, env["models"].AppleSubscription) == 1
    assert (await client.get("/protocols")).status_code == 402

    # Restore Purchases from that Apple ID claims it.
    await verify(client, signed)
    assert (await client.get("/protocols")).status_code == 200
    assert await count(env, env["models"].AppleSubscription) == 1


async def test_forged_notification_is_rejected(env):
    client, anonymous = env["client"], env["anonymous"]
    forger = FakeAppStore()
    res = await notify(anonymous, forger.notification(
        "SUBSCRIBED",
        transaction=forger.transaction(
            expires=now() + timedelta(days=3650), app_account_token=str(env["user_id"]),
        ),
    ))
    assert res.status_code == 400
    assert (await client.get("/protocols")).status_code == 402
    assert await count(env, env["models"].AppleSubscription) == 0


async def test_junk_notifications_do_not_crash(env):
    anonymous = env["anonymous"]
    assert (await anonymous.post("/iap/apple/notifications", content=b"not json")).status_code == 400
    assert (await anonymous.post("/iap/apple/notifications", json={})).status_code == 400
    assert (await anonymous.post("/iap/apple/notifications", json={"signedPayload": 7})).status_code == 400
    assert (await anonymous.post("/iap/apple/notifications", json=["signedPayload"])).status_code == 400
    assert (await notify(anonymous, "a.b.c")).status_code == 400


async def test_apples_test_notification_is_acknowledged(env):
    """The 'Request a Test Notification' button in App Store Connect."""
    assert (await notify(env["anonymous"], STORE.notification("TEST"))).status_code == 200


# ── token refresh for the native app ────────────────────────────────────────

async def test_native_app_can_refresh_with_a_body_token(env):
    anonymous = env["anonymous"]
    res = await anonymous.post("/auth/refresh", json={"refresh_token": env["refresh_token"]})
    assert res.status_code == 200
    access = res.json()["access_token"]

    me = await anonymous.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["id"] == str(env["user_id"])


async def test_refresh_refuses_missing_and_wrong_tokens(env):
    anonymous = env["anonymous"]
    assert (await anonymous.post("/auth/refresh")).status_code == 401
    assert (await anonymous.post("/auth/refresh", json={"refresh_token": "nope"})).status_code == 401
    # An access token is not a refresh token.
    access = env["client"].headers["Authorization"].removeprefix("Bearer ")
    assert (await anonymous.post("/auth/refresh", json={"refresh_token": access})).status_code == 401


# ── account deletion ────────────────────────────────────────────────────────

async def seed_account_data(env):
    """Give the user one of everything an account can own."""
    m = env["models"]
    uid, other_id = env["user_id"], env["other_id"]
    async with env["session"]() as db:
        session = m.Session(user_id=uid, device_fingerprint="fp-shared", platform="ios", ip_hash="x")
        db.add(session)
        protocol = m.UserProtocol(
            user_id=uid, label="Mine", vial_mg=5, reconstituted=True, bac_water_ml=2, target_dose_mcg=250,
        )
        db.add(protocol)
        await db.flush()
        db.add_all([
            m.CycleLog(user_id=uid, protocol_id=protocol.id, peptide_name="Mine", dose="250 mcg"),
            m.CycleLog(user_id=uid, peptide_name="Loose", dose="1 mg"),
            m.CalculatorUsage(user_id=uid, session_id=session.id, peptide_name="Mine",
                              vial_mg=5, bac_water_ml=2, target_mcg=250),
            # Another account's saved calculation, made on the same device.
            m.CalculatorUsage(user_id=other_id, session_id=session.id, peptide_name="Theirs",
                              vial_mg=10, bac_water_ml=2, target_mcg=500),
            m.TrialCounter(user_id=uid, device_fingerprint="fp-shared"),
            m.TrialGrant(user_id=uid, device_fingerprint="fp-shared"),
            m.EmailVerificationOTP(user_id=uid, otp_hash="h", expires_at=now() + timedelta(minutes=5)),
            m.AuditLog(user_id=uid, action="login", ip_hash="x"),
            m.PaymentClaim(user_id=uid, method="bank_transfer", status="rejected", currency="USD"),
            m.CryptoPayment(user_id=uid, order_id=f"{uid}:annual:x", plan="annual", price_amount=49),
            # A claim by someone else that this user once reviewed.
            m.PaymentClaim(user_id=other_id, method="bank_transfer", status="approved",
                           currency="USD", reviewed_by=uid, reviewed_at=now()),
        ])
        await db.commit()


async def test_wrong_password_deletes_nothing_and_keeps_the_session(env):
    client = env["client"]
    res = await client.post("/auth/delete-account", json={"password": "not it"})
    # 403, not 401: a 401 makes the clients sign the user out.
    assert res.status_code == 403
    assert (await fetch_user(env)) is not None
    assert (await client.get("/auth/me")).status_code == 200


async def test_delete_account_requires_a_session(env):
    res = await env["anonymous"].post("/auth/delete-account", json={"password": PASSWORD})
    assert res.status_code == 401


async def test_delete_account_removes_the_user_and_everything_they_own(env):
    m = env["models"]
    client, uid, other_id = env["client"], env["user_id"], env["other_id"]
    await seed_account_data(env)
    await verify(client, STORE.transaction(expires=now() + timedelta(days=30)))
    email = (await fetch_user(env)).email

    res = await client.post("/auth/delete-account", json={"password": PASSWORD})
    assert res.status_code == 200, res.text
    assert res.json()["app_store_subscription_active"] is True

    # Gone.
    assert (await fetch_user(env)) is None
    for model in (m.UserProtocol, m.CycleLog, m.Session, m.TrialCounter, m.EmailVerificationOTP,
                  m.CryptoPayment, m.AppleSubscription):
        assert await count(env, model, model.user_id == uid) == 0, model.__name__
    assert await count(env, m.CalculatorUsage, m.CalculatorUsage.user_id == uid) == 0
    assert await count(env, m.PaymentClaim, m.PaymentClaim.user_id == uid) == 0
    assert await count(env, m.AuditLog, m.AuditLog.user_id == uid) == 0

    # Other people's rows survive, without the pointer to the deleted user.
    assert await count(env, m.CalculatorUsage, m.CalculatorUsage.user_id == other_id) == 1
    assert await count(env, m.CalculatorUsage, m.CalculatorUsage.session_id.is_not(None)) == 0
    assert await count(env, m.PaymentClaim, m.PaymentClaim.user_id == other_id) == 1
    assert await count(env, m.PaymentClaim, m.PaymentClaim.reviewed_by == uid) == 0
    assert (await fetch_user(env, other_id)) is not None

    # The device's trial stays spent, attached to nobody.
    assert await count(env, m.TrialGrant, m.TrialGrant.device_fingerprint == "fp-shared") == 1
    assert await count(env, m.TrialGrant, m.TrialGrant.user_id.is_not(None)) == 0

    # One anonymous record that a deletion happened.
    assert await count(env, m.AuditLog, m.AuditLog.action == "account_deleted",
                       m.AuditLog.user_id.is_(None)) == 1

    # The session is dead and the credentials no longer work.
    assert (await client.get("/auth/me")).status_code == 401
    anonymous = env["anonymous"]
    login = await anonymous.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 401
    refresh = await anonymous.post("/auth/refresh", json={"refresh_token": env["refresh_token"]})
    assert refresh.status_code == 401


async def test_deleted_email_can_register_again(env, monkeypatch):
    client = env["client"]
    email = (await fetch_user(env)).email
    assert (await client.post("/auth/delete-account", json={"password": PASSWORD})).status_code == 200

    from app.routers import auth as auth_router

    async def no_mail(*args, **kwargs):
        return None

    monkeypatch.setattr(auth_router, "send_email_verification_otp", no_mail)
    res = await env["anonymous"].post("/auth/register", json={
        "email": email, "password": PASSWORD, "confirm_password": PASSWORD,
        "full_name": "Back Again", "device_fingerprint": "fallback-fp-test",
    })
    assert res.status_code == 201, res.text


async def test_administrators_cannot_delete_themselves(env):
    User = env["models"].User
    async with env["session"]() as db:
        user = (await db.execute(select(User).where(User.id == env["user_id"]))).scalar_one()
        user.is_admin = True
        await db.commit()

    res = await env["client"].post("/auth/delete-account", json={"password": PASSWORD})
    assert res.status_code == 400
    assert (await fetch_user(env)) is not None


async def test_deleting_an_account_without_a_subscription_says_so(env):
    res = await env["client"].post("/auth/delete-account", json={"password": PASSWORD})
    assert res.status_code == 200
    assert res.json()["app_store_subscription_active"] is False


# ── the account trial ───────────────────────────────────────────────────────

async def _sign_up(env, monkeypatch, *, platform, fingerprint):
    """Register and verify one account the way a client on `platform` would.

    Returns the verification response and whatever the welcome mail was sent
    with.
    """
    from app.routers import auth as auth_router

    welcome = {}

    async def no_mail(*args, **kwargs):
        return None

    async def capture_welcome(to_email, full_name, trial_days=None):
        welcome["trial_days"] = trial_days

    monkeypatch.setattr(auth_router, "send_email_verification_otp", no_mail)
    monkeypatch.setattr(auth_router, "send_welcome_email", capture_welcome)
    monkeypatch.setattr(auth_router, "_generate_otp", lambda: "123456")

    email = f"new-{uuid.uuid4().hex[:10]}@example.com"
    headers = {"X-Platform": platform} if platform else {}
    anonymous = env["anonymous"]
    res = await anonymous.post("/auth/register", headers=headers, json={
        "email": email, "password": PASSWORD, "confirm_password": PASSWORD,
        "full_name": "New Person", "device_fingerprint": fingerprint,
    })
    assert res.status_code == 201, res.text
    res = await anonymous.post("/auth/verify-email", headers=headers, json={"email": email, "otp": "123456"})
    assert res.status_code == 200, res.text
    anonymous.cookies.clear()
    return res, welcome


async def test_account_created_in_the_ios_app_gets_no_account_trial(env, monkeypatch):
    """On the App Store the free trial is the subscription's introductory
    offer. A second trial from this API would be Pro unlocked outside In-App
    Purchase (guideline 3.1.1)."""
    res, welcome = await _sign_up(env, monkeypatch, platform="ios", fingerprint="device-ios-1")
    token = res.json()["access_token"]

    me = (await env["anonymous"].get("/auth/me", headers={"Authorization": f"Bearer {token}"})).json()
    assert me["access"]["has_access"] is False
    assert me["access"]["is_trial"] is False
    assert me["access"]["trial_ends_at"] is None
    assert welcome["trial_days"] is None

    # The device's one trial was not spent, so it is still there on the web.
    assert await count(env, env["models"].TrialGrant) == 0

    # Pro is the subscription.
    protocols = await env["anonymous"].get("/protocols", headers={"Authorization": f"Bearer {token}"})
    assert protocols.status_code == 402


async def test_account_created_on_the_web_still_gets_the_account_trial(env, monkeypatch):
    for platform in ("web", None, "android"):
        res, welcome = await _sign_up(
            env, monkeypatch, platform=platform, fingerprint=f"device-{platform or 'none'}",
        )
        token = res.json()["access_token"]
        me = (await env["anonymous"].get("/auth/me", headers={"Authorization": f"Bearer {token}"})).json()
        assert me["access"]["has_access"] is True, platform
        assert me["access"]["is_trial"] is True, platform
        assert welcome["trial_days"] == 14, platform


async def test_ios_trial_can_be_switched_back_on(env, monkeypatch):
    from app.routers import auth as auth_router

    monkeypatch.setattr(auth_router._settings, "TRIAL_FOR_IOS_SIGNUPS", True)
    res, welcome = await _sign_up(env, monkeypatch, platform="ios", fingerprint="device-ios-2")
    token = res.json()["access_token"]
    me = (await env["anonymous"].get("/auth/me", headers={"Authorization": f"Bearer {token}"})).json()
    assert me["access"]["is_trial"] is True
    assert welcome["trial_days"] == 14


# ── admin view ──────────────────────────────────────────────────────────────

async def test_admin_sees_subscribers(env):
    User = env["models"].User
    async with env["session"]() as db:
        other = (await db.execute(select(User).where(User.id == env["other_id"]))).scalar_one()
        other.is_admin = True
        await db.commit()

    await verify(env["client"], STORE.transaction(expires=now() + timedelta(days=30)))

    admin = env["other"]
    stats = (await admin.get("/admin/stats")).json()
    assert stats["subscribed_users"] == 1

    listed = (await admin.get("/admin/users", params={"access": "subscription"})).json()
    assert listed["total"] == 1
    assert listed["items"][0]["access_state"] == "subscription"
    assert listed["items"][0]["apple_sub_until"] is not None

    lapsed = (await admin.get("/admin/users", params={"access": "lapsed"})).json()
    assert all(item["id"] != str(env["user_id"]) for item in lapsed["items"])
