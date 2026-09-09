"""Approval semantics — the transition that hands out the product.

Run: pytest tests/ -q
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.middleware.auth import has_access
from app.routers.admin import _access_state, _approve
from app.routers.subscriptions import access_info

NOW = datetime.now(timezone.utc)


def _user(**kw):
    return SimpleNamespace(**{
        "id": uuid.uuid4(),
        "plan": "free",
        "paid_until": None,
        "trial_ends_at": None,
        "lifetime_access_at": None,
        "access_revoked_at": None,
        "access_revoked_reason": None,
        **kw,
    })


def _claim(**kw):
    return SimpleNamespace(**{
        "id": uuid.uuid4(),
        "status": "submitted",
        "reviewed_by": None,
        "reviewed_at": None,
        "rejection_reason": None,
        **kw,
    })


# ── approval ────────────────────────────────────────────────────────────────

def test_approve_grants_a_lifetime_licence():
    user, claim, admin = _user(), _claim(), _user(id=uuid.uuid4())
    _approve(claim, user, admin, NOW)

    assert claim.status == "approved"
    assert claim.reviewed_by == admin.id
    assert claim.reviewed_at == NOW
    assert user.lifetime_access_at == NOW
    assert user.plan == "pro"
    assert has_access(user) is True


def test_approving_twice_never_moves_the_grant_date():
    """A double-clicked button, or a retried request, must not rewrite when
    the licence started."""
    user, claim, admin = _user(), _claim(), _user()
    _approve(claim, user, admin, NOW)
    first = user.lifetime_access_at

    _approve(claim, user, admin, NOW + timedelta(days=5))
    assert user.lifetime_access_at == first


def test_approval_clears_an_earlier_revocation():
    """Someone cut off who has now paid should not stay locked out by a stale
    flag."""
    user = _user(access_revoked_at=NOW - timedelta(days=30), access_revoked_reason="chargeback")
    _approve(_claim(), user, _user(), NOW)

    assert user.access_revoked_at is None
    assert user.access_revoked_reason is None
    assert has_access(user) is True


# ── access_info, which is what the UI renders ───────────────────────────────

def test_lifetime_user_gets_no_countdown():
    """A licence has no end date, so days_remaining must be absent — otherwise
    TrialBanner would nag someone who bought the product outright."""
    info = access_info(_user(
        lifetime_access_at=NOW - timedelta(days=2),
        trial_ends_at=NOW + timedelta(days=3),
    ))
    assert info.is_lifetime is True
    assert info.is_trial is False
    assert info.days_remaining is None
    assert info.has_access is True


def test_trial_user_gets_a_countdown():
    info = access_info(_user(trial_ends_at=NOW + timedelta(days=5)))
    assert info.is_trial is True
    assert info.is_lifetime is False
    assert info.days_remaining is not None


def test_revoked_user_is_reported_as_locked_out():
    info = access_info(_user(
        lifetime_access_at=NOW - timedelta(days=5),
        access_revoked_at=NOW,
    ))
    assert info.has_access is False
    assert info.is_revoked is True
    assert info.is_lifetime is False


def test_pending_claim_blocks_a_second_submission():
    info = access_info(_user(), _claim(status="submitted"))
    assert info.claim_status == "submitted"
    assert info.can_submit_claim is False


def test_rejected_claim_allows_a_corrected_resubmission():
    """A rejected user with no next step files a refund request instead."""
    info = access_info(_user(), _claim(status="rejected"))
    assert info.claim_status == "rejected"
    assert info.can_submit_claim is True


def test_no_claim_allows_submission():
    assert access_info(_user()).can_submit_claim is True


# ── access_state, which drives the admin badge ──────────────────────────────

def test_access_state_matches_the_gate_ordering():
    assert _access_state(_user(access_revoked_at=NOW, lifetime_access_at=NOW)) == "revoked"
    assert _access_state(_user(lifetime_access_at=NOW)) == "lifetime"
    assert _access_state(_user(paid_until=NOW + timedelta(days=2))) == "crypto"
    assert _access_state(_user(trial_ends_at=NOW + timedelta(days=2))) == "trial"
    assert _access_state(_user(trial_ends_at=NOW - timedelta(days=2))) == "lapsed"
    assert _access_state(_user()) == "none"
