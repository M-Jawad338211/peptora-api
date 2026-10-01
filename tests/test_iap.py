"""App Store purchase verification: the parts that give away access if wrong.

No database needed. The end-to-end flow (verify endpoint, notifications,
account deletion) is in tests/test_iap_integration.py.

Run: pytest tests/test_iap.py -q
"""

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.middleware.auth import has_access
from app.routers.iap import _apply_fields, _status_of
from app.routers.subscriptions import access_info
from app.utils import apple_iap
from tests.apple_fakes import MONTHLY, YEARLY, FakeAppStore

NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def store():
    return FakeAppStore()


@pytest.fixture
def trusted(store, monkeypatch):
    """Point the verifier at the test certificate authority."""
    apple_iap.reset_caches()
    monkeypatch.setattr(apple_iap, "_root_certificates", lambda: (store.root_der,))
    apple_iap._verifier.cache_clear()
    yield store
    apple_iap._verifier.cache_clear()


def _user(**kw):
    return SimpleNamespace(**{
        "paid_until": None,
        "trial_ends_at": None,
        "lifetime_access_at": None,
        "access_revoked_at": None,
        "apple_sub_until": None,
        **kw,
    })


def _row(**kw):
    return SimpleNamespace(**{
        "latest_transaction_id": None,
        "product_id": MONTHLY,
        "environment": "Sandbox",
        "purchased_at": None,
        "expires_at": None,
        "revoked_at": None,
        "is_trial_period": False,
        "app_account_token": None,
        "raw": None,
        **kw,
    })


# ── the certificate that production trusts ──────────────────────────────────

def test_bundled_root_is_apples_root_ca_g3():
    """The file shipped in app/certs must be Apple's real root, byte for byte.

    If someone swaps it, every signature check in this module starts trusting
    a different signer. The fingerprint is the one Apple publishes.
    """
    apple_iap.reset_caches()
    (der,) = apple_iap._root_certificates()
    assert hashlib.sha256(der).hexdigest() == (
        "63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179"
    )

    from cryptography import x509
    cert = x509.load_der_x509_certificate(der)
    assert "Apple Root CA - G3" in cert.subject.rfc4514_string()
    assert cert.not_valid_after_utc > NOW


def test_a_swapped_root_certificate_is_refused(tmp_path, monkeypatch):
    fake = tmp_path / "AppleRootCA-G3.cer"
    fake.write_bytes(FakeAppStore().root_der)
    apple_iap.reset_caches()
    monkeypatch.setattr(apple_iap, "ROOT_CERT_PATH", fake)
    with pytest.raises(RuntimeError):
        apple_iap._root_certificates()
    apple_iap.reset_caches()


def test_a_transaction_signed_by_another_authority_is_refused(store):
    """With Apple's real root in place, the test store's signature means nothing."""
    apple_iap.reset_caches()
    with pytest.raises(apple_iap.AppleVerificationError):
        apple_iap.verify_transaction(store.transaction())


# ── transactions ────────────────────────────────────────────────────────────

def test_valid_sandbox_transaction_is_accepted(trusted):
    token = "0b1c2d3e-4f50-4a61-8b72-9c8d7e6f5a4b"
    expires = NOW + timedelta(days=30)
    tx = apple_iap.verify_transaction(
        trusted.transaction(product_id=YEARLY, expires=expires, app_account_token=token.upper())
    )
    assert tx.product_id == YEARLY
    assert tx.environment == "Sandbox"
    assert tx.original_transaction_id == "2000000000000001"
    assert abs((tx.expires_at - expires).total_seconds()) < 1
    assert tx.revoked_at is None
    assert tx.is_active(NOW)
    # Normalised to lower case so it compares equal to a Python UUID string.
    assert tx.app_account_token == token


def test_valid_production_transaction_is_accepted(trusted):
    tx = apple_iap.verify_transaction(trusted.transaction(environment="Production"))
    assert tx.environment == "Production"


def test_free_trial_period_is_recognised(trusted):
    assert apple_iap.verify_transaction(trusted.transaction(free_trial=True)).is_trial_period is True
    assert apple_iap.verify_transaction(trusted.transaction()).is_trial_period is False


def test_tampered_payload_is_refused(trusted):
    """Extending your own expiry date is the obvious attack."""
    header, payload, signature = trusted.transaction().split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["expiresDate"] = claims["expiresDate"] + 10 * 365 * 86_400_000
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(apple_iap.AppleVerificationError):
        apple_iap.verify_transaction(f"{header}.{forged}.{signature}")


def test_another_apps_transaction_is_refused(trusted):
    """A genuine purchase of some other app must not unlock this one."""
    with pytest.raises(apple_iap.AppleVerificationError):
        apple_iap.verify_transaction(trusted.transaction(bundle_id="com.example.other"))


def test_unknown_product_is_refused(trusted):
    with pytest.raises(apple_iap.AppleVerificationError) as exc:
        apple_iap.verify_transaction(trusted.transaction(product_id="app.peptora.something.else"))
    assert exc.value.reason == "unknown_product"


def test_non_subscription_purchase_is_refused(trusted):
    with pytest.raises(apple_iap.AppleVerificationError) as exc:
        apple_iap.verify_transaction(trusted.transaction(type_="Consumable"))
    assert exc.value.reason == "not_a_subscription"


@pytest.mark.parametrize("environment", ["Xcode", "LocalTesting", "", None, "production"])
def test_unsigned_test_environments_are_never_accepted(trusted, environment):
    """Apple's library skips signature checks for Xcode and LocalTesting.

    A client must never be able to pick one of those by claiming it, or a
    hand-written JSON blob would be accepted as a purchase.
    """
    import jwt
    forged = jwt.encode(
        {
            "transactionId": "1", "originalTransactionId": "1",
            "bundleId": "app.peptora", "productId": MONTHLY,
            "type": "Auto-Renewable Subscription",
            "expiresDate": int((NOW + timedelta(days=3650)).timestamp() * 1000),
            "environment": environment,
        },
        "not-apples-key-and-long-enough-to-keep-pyjwt-quiet",
        algorithm="HS256",
    )
    with pytest.raises(apple_iap.AppleVerificationError):
        apple_iap.verify_transaction(forged)


def test_sandbox_can_be_switched_off(trusted, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "APPLE_IAP_ALLOW_SANDBOX", False)
    with pytest.raises(apple_iap.AppleVerificationError) as exc:
        apple_iap.verify_transaction(trusted.transaction(environment="Sandbox"))
    assert exc.value.reason == "sandbox_not_allowed"
    # Production is unaffected.
    assert apple_iap.verify_transaction(trusted.transaction(environment="Production"))


@pytest.mark.parametrize("junk", ["", "not-a-jws", "a.b.c", "x" * 30_000, None, 12345])
def test_junk_is_refused_without_crashing(trusted, junk):
    with pytest.raises(apple_iap.AppleVerificationError):
        apple_iap.verify_transaction(junk)


def test_environment_mismatch_between_claim_and_signature_is_refused(trusted):
    """The environment is peeked at before verification to choose a verifier.
    Lying about it must only ever lead to a refusal."""
    header, payload, signature = trusted.transaction(environment="Sandbox").split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["environment"] = "Production"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(apple_iap.AppleVerificationError):
        apple_iap.verify_transaction(f"{header}.{forged}.{signature}")


# ── notifications ───────────────────────────────────────────────────────────

def test_valid_notification_is_decoded(trusted):
    expires = NOW + timedelta(days=30)
    note = apple_iap.verify_notification(trusted.notification(
        "DID_RENEW",
        transaction=trusted.transaction(expires=expires, transaction_id="2000000000000002"),
        renewal=trusted.renewal_info(auto_renew=False),
    ))
    assert note.notification_type == "DID_RENEW"
    assert note.transaction.transaction_id == "2000000000000002"
    assert note.auto_renew is False
    assert note.environment == "Sandbox"


def test_production_notification_checks_the_app_id(trusted):
    note = apple_iap.verify_notification(trusted.notification(
        "SUBSCRIBED", subtype="INITIAL_BUY", environment="Production",
        transaction=trusted.transaction(environment="Production"),
    ))
    assert note.subtype == "INITIAL_BUY"


def test_notification_for_another_app_is_refused(trusted):
    with pytest.raises(apple_iap.AppleVerificationError):
        apple_iap.verify_notification(trusted.notification(
            "DID_RENEW", bundle_id="com.example.other", transaction=trusted.transaction(),
        ))


def test_test_notification_has_no_transaction(trusted):
    note = apple_iap.verify_notification(trusted.notification("TEST"))
    assert note.notification_type == "TEST"
    assert note.transaction is None


def test_notification_about_an_unknown_product_is_acknowledged_not_applied(trusted):
    note = apple_iap.verify_notification(trusted.notification(
        "DID_RENEW", transaction=trusted.transaction(product_id="app.peptora.retired"),
    ))
    assert note.transaction is None


def test_grace_period_is_read_from_renewal_info(trusted):
    grace = NOW + timedelta(days=16)
    note = apple_iap.verify_notification(trusted.notification(
        "DID_FAIL_TO_RENEW", subtype="GRACE_PERIOD",
        transaction=trusted.transaction(),
        renewal=trusted.renewal_info(grace_expires=grace),
    ))
    assert abs((note.grace_expires_at - grace).total_seconds()) < 1


# ── applying a transaction to a subscription row ────────────────────────────

def _tx(**kw):
    base = dict(
        original_transaction_id="1", transaction_id="1", product_id=MONTHLY,
        environment="Sandbox", purchased_at=NOW, expires_at=NOW + timedelta(days=30),
        revoked_at=None, signed_at=NOW, app_account_token=None, is_trial_period=False, raw={},
    )
    base.update(kw)
    return apple_iap.AppleTransaction(**base)


def test_first_transaction_fills_the_row():
    row = _row()
    tx = _tx(app_account_token="abc")
    _apply_fields(row, tx)
    assert row.latest_transaction_id == "1"
    assert row.expires_at == tx.expires_at
    assert row.app_account_token == "abc"


def test_renewal_moves_the_expiry_forward():
    row = _row()
    _apply_fields(row, _tx())
    later = NOW + timedelta(days=60)
    _apply_fields(row, _tx(transaction_id="2", expires_at=later, product_id=YEARLY))
    assert row.expires_at == later
    assert row.latest_transaction_id == "2"
    assert row.product_id == YEARLY


def test_an_older_transaction_never_moves_the_row_backwards():
    """Notifications arrive out of order, and a client can replay an old one."""
    row = _row()
    later = NOW + timedelta(days=60)
    _apply_fields(row, _tx(transaction_id="2", expires_at=later))
    _apply_fields(row, _tx(transaction_id="1", expires_at=NOW + timedelta(days=30)))
    assert row.expires_at == later
    assert row.latest_transaction_id == "2"


def test_refund_of_the_current_period_revokes():
    row = _row()
    _apply_fields(row, _tx())
    _apply_fields(row, _tx(revoked_at=NOW))
    assert row.revoked_at == NOW


def test_refund_of_an_earlier_period_does_not_revoke_the_current_one():
    row = _row()
    _apply_fields(row, _tx(transaction_id="2", expires_at=NOW + timedelta(days=60)))
    _apply_fields(row, _tx(transaction_id="1", expires_at=NOW + timedelta(days=30), revoked_at=NOW))
    assert row.revoked_at is None


def test_replaying_the_pre_refund_transaction_does_not_undo_the_refund():
    row = _row()
    _apply_fields(row, _tx())
    _apply_fields(row, _tx(revoked_at=NOW))
    _apply_fields(row, _tx())  # the copy the device held before the refund
    assert row.revoked_at == NOW


def test_subscribing_again_after_a_refund_restores_access():
    row = _row()
    _apply_fields(row, _tx())
    _apply_fields(row, _tx(revoked_at=NOW))
    _apply_fields(row, _tx(transaction_id="3", expires_at=NOW + timedelta(days=90)))
    assert row.revoked_at is None
    assert row.latest_transaction_id == "3"


def test_status_labels():
    assert _status_of(_tx(), NOW) == "active"
    assert _status_of(_tx(expires_at=NOW - timedelta(seconds=1)), NOW) == "expired"
    assert _status_of(_tx(revoked_at=NOW), NOW) == "revoked"


# ── the gate ────────────────────────────────────────────────────────────────

def test_live_subscription_grants_access():
    assert has_access(_user(apple_sub_until=NOW + timedelta(days=1))) is True


def test_lapsed_subscription_denies_access():
    assert has_access(_user(apple_sub_until=NOW - timedelta(seconds=1))) is False


def test_revocation_outranks_a_live_subscription():
    assert has_access(_user(
        apple_sub_until=NOW + timedelta(days=300),
        access_revoked_at=NOW,
    )) is False


def test_subscriber_inside_trial_window_is_not_reported_as_trialling():
    """Otherwise a paying customer gets a 'your trial is ending' banner."""
    info = access_info(_user(
        trial_ends_at=NOW + timedelta(days=5),
        apple_sub_until=NOW + timedelta(days=30),
    ))
    assert info.has_access is True
    assert info.is_trial is False
    assert info.is_subscription is True
    assert info.subscription_store == "app_store"
    assert info.days_remaining in (29, 30)


def test_subscription_details_come_from_the_row():
    sub = SimpleNamespace(product_id=YEARLY, auto_renew=False, is_trial_period=True)
    info = access_info(_user(apple_sub_until=NOW + timedelta(days=7)), None, sub)
    assert info.subscription_product == YEARLY
    assert info.subscription_auto_renew is False
    assert info.subscription_is_trial is True


def test_lapsed_subscriber_reports_no_subscription():
    sub = SimpleNamespace(product_id=YEARLY, auto_renew=False, is_trial_period=False)
    info = access_info(_user(apple_sub_until=NOW - timedelta(days=1)), None, sub)
    assert info.has_access is False
    assert info.is_subscription is False
    assert info.subscription_product is None


# ── library content punctuation ─────────────────────────────────────────────

def test_dashes_become_plain_punctuation():
    from app.utils.text import clean_content, plain_punctuation

    assert plain_punctuation("Typical range — NOT validated") == "Typical range, NOT validated"
    assert plain_punctuation("250–500 mcg") == "250-500 mcg"
    assert plain_punctuation("26 – 52 weeks") == "26-52 weeks"
    assert plain_punctuation("dose–response") == "dose-response"
    assert plain_punctuation("Tesamorelin — compound summary", title=True) == "Tesamorelin: compound summary"
    assert plain_punctuation("nothing to change") == "nothing to change"
    # The typewriter spelling of the same dash, as stored in older entries.
    assert (
        plain_punctuation("hormone (rhGH) -- a bioidentical copy")
        == "hormone (rhGH), a bioidentical copy"
    )
    # Hyphens that are part of a name or a number are not punctuation.
    assert plain_punctuation("long-acting GLP-1 at -20 C") == "long-acting GLP-1 at -20 C"

    cleaned = clean_content({
        "title": "DSIP — compound summary",
        "summary": "Short — and to the point",
        "url": "https://example.org/a—b",
        "doi": "10.1000/x–y",
        "dose_ranges": [{"context": "Range — reported", "low": 1.5, "citation_refs": [1, 2]}],
        "human_trials": True,
        "note": None,
    })
    assert cleaned["title"] == "DSIP: compound summary"
    assert cleaned["summary"] == "Short, and to the point"
    # Links and identifiers are data and are never rewritten.
    assert cleaned["url"] == "https://example.org/a—b"
    assert cleaned["doi"] == "10.1000/x–y"
    assert cleaned["dose_ranges"][0] == {"context": "Range, reported", "low": 1.5, "citation_refs": [1, 2]}
    assert cleaned["human_trials"] is True and cleaned["note"] is None


def test_stray_line_break_tags_are_removed_from_library_text():
    from app.utils.text import clean_content

    cleaned = clean_content({
        "summary": "It is not FDA-approved.</br>",
        "overview": "First line.<br>Second line.<BR />Third line.",
        "notes": "Less than 5 mg, i.e. <5 mg, is unchanged",
        "url": "https://example.org/?q=<br>",
    })
    assert cleaned["summary"] == "It is not FDA-approved."
    assert cleaned["overview"] == "First line. Second line. Third line."
    # Only the line-break tag is touched; a bare "<" in prose is data.
    assert cleaned["notes"] == "Less than 5 mg, i.e. <5 mg, is unchanged"
    assert cleaned["url"] == "https://example.org/?q=<br>"
