"""Tests for the parts of billing that lose money or give away access.

Run: pytest tests/ -q
"""

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.middleware.auth import has_access
from app.utils import nowpayments as np


def _user(**kw):
    return SimpleNamespace(**{"paid_until": None, "trial_ends_at": None, **kw})


NOW = datetime.now(timezone.utc)


# ── access windows ──────────────────────────────────────────────────────────

def test_no_windows_means_no_access():
    assert has_access(_user()) is False


def test_anonymous_has_no_access():
    assert has_access(None) is False


def test_live_trial_grants_access():
    assert has_access(_user(trial_ends_at=NOW + timedelta(days=3))) is True


def test_expired_trial_denies_access():
    assert has_access(_user(trial_ends_at=NOW - timedelta(seconds=1))) is False


def test_live_payment_grants_access_even_with_dead_trial():
    assert has_access(_user(
        trial_ends_at=NOW - timedelta(days=30),
        paid_until=NOW + timedelta(days=1),
    )) is True


def test_expired_payment_denies_access():
    assert has_access(_user(paid_until=NOW - timedelta(seconds=1))) is False


# ── IPN signature ───────────────────────────────────────────────────────────

SECRET = "test-ipn-secret"


def _sign(body: dict, secret: str = SECRET) -> tuple[bytes, str]:
    """Sign the way NOWPayments does: sorted keys, no whitespace."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(secret.encode(), canonical.encode(), hashlib.sha512).hexdigest()
    # Send the body in a DIFFERENT key order than the canonical form — that is
    # the real-world case, and it is what catches an implementation that signs
    # the bytes as received instead of re-serialising them.
    raw = json.dumps({k: body[k] for k in reversed(list(body))}).encode()
    return raw, sig


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "NOWPAYMENTS_IPN_SECRET", SECRET)


PAYLOAD = {
    "payment_id": 5524759814,
    "payment_status": "finished",
    "order_id": "b3f1c2d4-0000-4000-8000-000000000000:annual:abc123def456",
    "price_amount": 49,
    "price_currency": "usd",
    "actually_paid": 49.0,
    "pay_currency": "usdttrc20",
}


def test_valid_signature_accepted():
    raw, sig = _sign(PAYLOAD)
    assert np.verify_ipn_signature(raw, sig) is True


def test_tampered_amount_rejected():
    raw, sig = _sign(PAYLOAD)
    tampered = json.dumps({**PAYLOAD, "price_amount": 1}).encode()
    assert np.verify_ipn_signature(tampered, sig) is False


def test_wrong_secret_rejected():
    raw, _ = _sign(PAYLOAD)
    _, bad_sig = _sign(PAYLOAD, secret="attacker-secret")
    assert np.verify_ipn_signature(raw, bad_sig) is False


def test_missing_signature_rejected():
    raw, _ = _sign(PAYLOAD)
    assert np.verify_ipn_signature(raw, "") is False


def test_unconfigured_secret_rejects_everything(monkeypatch):
    """No secret must mean no access, never an open door."""
    from app.config import settings
    monkeypatch.setattr(settings, "NOWPAYMENTS_IPN_SECRET", None)
    raw, sig = _sign(PAYLOAD)
    assert np.verify_ipn_signature(raw, sig) is False


def test_malformed_body_rejected():
    assert np.verify_ipn_signature(b"not json{{", "deadbeef") is False


def test_nested_objects_sort_recursively():
    """sort_keys must reach nested dicts, matching Node's replacer array."""
    nested = {**PAYLOAD, "meta": {"z": 1, "a": {"y": 2, "b": 3}}}
    raw, sig = _sign(nested)
    assert np.verify_ipn_signature(raw, sig) is True


# ── order_id round-trip ─────────────────────────────────────────────────────

def test_order_id_round_trips():
    uid = uuid.uuid4()
    parsed = np.parse_order_id(np.build_order_id(uid, "monthly"))
    assert parsed == (str(uid), "monthly")


def test_order_ids_are_unique_per_invoice():
    uid = uuid.uuid4()
    assert np.build_order_id(uid, "annual") != np.build_order_id(uid, "annual")


@pytest.mark.parametrize("bad", [
    "", "garbage", "a:b", "not-a-uuid:monthly:abc",
    f"{uuid.uuid4()}:lifetime:abc",   # plan not in PLAN_DAYS
    f"{uuid.uuid4()}:monthly",         # missing nonce
])
def test_malformed_order_ids_return_none_not_raise(bad):
    assert np.parse_order_id(bad) is None


# ── pricing ─────────────────────────────────────────────────────────────────

def test_plan_prices_match_the_advertised_offer():
    assert np.plan_price("monthly") == 5.0
    assert np.plan_price("annual") == 49.0


def test_monthly_is_restricted_to_low_fee_chains():
    """A $5 invoice in BTC or ETH is unpayable — the network minimum exceeds it."""
    assert "btc" not in np.LOW_FEE_CURRENCIES
    assert "eth" not in np.LOW_FEE_CURRENCIES
    assert "usdttrc20" in np.LOW_FEE_CURRENCIES


def test_only_finished_is_treated_as_paid():
    assert np.STATUS_PAID == "finished"
    assert "confirmed" != np.STATUS_PAID
    assert np.STATUS_PARTIAL == "partially_paid"
