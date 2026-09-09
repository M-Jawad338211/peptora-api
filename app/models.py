import uuid
from datetime import date, datetime, timezone
from typing import Any
from sqlalchemy import (
    String, Boolean, Integer, BigInteger, DateTime, Date, Text, JSON,
    ForeignKey, Numeric, UniqueConstraint, Index, CheckConstraint, text,
    Enum as SAEnum
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSONB, TSVECTOR
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Denormalised access flag, kept in sync with paid_until/trial_ends_at by
    # the IPN handler and the nightly lapse sweep. The native app and
    # admin.py both compare against the literal "pro", so the values stay
    # "free" | "pro". `has_access()` in app/middleware/auth.py is the real
    # authority — never gate on `plan` alone, it can lag by up to a day.
    plan: Mapped[str] = mapped_column(String(50), default="free", nullable=False)

    # Crypto cannot auto-charge, so access is a prepaid window rather than a
    # subscription status: a payment pushes paid_until forward, and when it
    # passes, access simply stops. Both are nullable — null means "never had
    # one", which is not the same as an expired one.
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    # The one-time purchase. Set once, when an admin approves a payment claim;
    # non-null means permanent access and no expiry to track. A far-future
    # `paid_until` would have worked and lied everywhere it was read — the
    # sweep would count a lifetime user as expiring and `days_remaining` would
    # report a five-figure number to the UI.
    lifetime_access_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    # Refunds, chargebacks and abuse. Kept as a timestamp rather than clearing
    # the grant so the record of what happened survives. `has_access()` checks
    # this FIRST — a refunded user may still be inside their original trial
    # window, and checking it last would make revocation silently do nothing.
    access_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    access_revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The device fingerprint seen at registration. Submitted at /auth/register
    # but not at /auth/verify-email, where the trial is actually granted, so
    # without persisting it here the grant has nothing to bind against.
    signup_fingerprint: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    expo_push_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    consent_accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consent_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    sessions: Mapped[list["Session"]] = relationship("Session", back_populates="user")
    trial_counter: Mapped["TrialCounter | None"] = relationship("TrialCounter", back_populates="user", uselist=False)
    calculator_usages: Mapped[list["CalculatorUsage"]] = relationship("CalculatorUsage", back_populates="user")
    payments: Mapped[list["CryptoPayment"]] = relationship("CryptoPayment", back_populates="user")
    payment_claims: Mapped[list["PaymentClaim"]] = relationship(
        "PaymentClaim", back_populates="user", foreign_keys="PaymentClaim.user_id"
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship("AuditLog", back_populates="user")
    verification_otps: Mapped[list["EmailVerificationOTP"]] = relationship("EmailVerificationOTP", back_populates="user")
    cycle_logs: Mapped[list["CycleLog"]] = relationship("CycleLog", back_populates="user")


class EmailVerificationOTP(Base):
    __tablename__ = "email_verification_otps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    otp_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    user: Mapped["User"] = relationship("User", back_populates="verification_otps")

    __table_args__ = (
        Index("ix_email_verification_user_created", "user_id", "created_at"),
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    device_fingerprint: Mapped[str] = mapped_column(String(255), index=True)
    platform: Mapped[str] = mapped_column(String(50), default="web")
    ip_hash: Mapped[str] = mapped_column(String(255))
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_active: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped["User | None"] = relationship("User", back_populates="sessions")
    calculator_usages: Mapped[list["CalculatorUsage"]] = relationship("CalculatorUsage", back_populates="session")


class TrialCounter(Base):
    __tablename__ = "trial_counters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, unique=True)
    device_fingerprint: Mapped[str] = mapped_column(String(255), index=True)
    calc_uses_anonymous: Mapped[int] = mapped_column(Integer, default=0)
    calc_uses_free: Mapped[int] = mapped_column(Integer, default=0)
    signup_bonus_granted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped["User | None"] = relationship("User", back_populates="trial_counter")

    __table_args__ = (
        Index("ix_trial_device_fp", "device_fingerprint"),
    )


class CalculatorUsage(Base):
    __tablename__ = "calculator_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True)
    peptide_name: Mapped[str] = mapped_column(String(255))
    vial_mg: Mapped[float] = mapped_column(Numeric(10, 3))
    bac_water_ml: Mapped[float] = mapped_column(Numeric(10, 3))
    target_mcg: Mapped[float] = mapped_column(Numeric(10, 2))
    result_units: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    result_ml: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    platform: Mapped[str] = mapped_column(String(50), default="web")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    user: Mapped["User | None"] = relationship("User", back_populates="calculator_usages")
    session: Mapped["Session | None"] = relationship("Session", back_populates="calculator_usages")


class CryptoPayment(Base):
    """One NOWPayments invoice and its lifecycle.

    A row is created when we hand the user an invoice URL, then updated by IPN
    callbacks as the payment moves through waiting → confirming → finished.
    Rows are never deleted: they are the audit trail for why a given user has
    the access window they have.
    """

    __tablename__ = "crypto_payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    # Our reference, echoed back on every callback: "<user_id>:<plan>:<nonce>".
    # This is what ties an anonymous-looking IPN to an account — NOWPayments
    # has no concept of our users.
    order_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    plan: Mapped[str] = mapped_column(String(50), nullable=False)  # "monthly" | "annual"

    # NOWPayments ids. The invoice exists as soon as we create it; payment_id
    # only appears once the user picks a coin and a payment is generated, and
    # a single invoice can spawn several payments if the first expires.
    np_invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # Indexed, deliberately NOT unique. Uniqueness here bought nothing —
    # rows are found by order_id and double-crediting is prevented by
    # credited_at — while any collision raised IntegrityError inside the IPN
    # handler, which is a 500, which makes NOWPayments retry that callback
    # forever. Caught by tests/test_ipn_integration.py.
    np_payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    price_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    price_currency: Mapped[str] = mapped_column(String(20), nullable=False, default="usd")
    pay_currency: Mapped[str | None] = mapped_column(String(40), nullable=True)
    pay_amount: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)
    actually_paid: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)

    status: Mapped[str] = mapped_column(String(40), nullable=False, default="waiting")

    # Set exactly once, when this payment extends the user's access window.
    # The idempotency guard: NOWPayments retries callbacks, and a duplicate
    # "finished" must not buy a second month.
    credited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    raw_ipn: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship("User", back_populates="payments")

    __table_args__ = (
        Index("ix_crypto_payment_user_created", "user_id", "created_at"),
    )


# ---------------------------------------------------------------------------
# Manual billing
# ---------------------------------------------------------------------------

# Non-terminal statuses. A user may hold only one claim in these states at a
# time — enforced by a partial unique index, so nobody floods the review queue
# by resubmitting.
OPEN_CLAIM_STATUSES = ("submitted", "under_review")

CLAIM_REJECTION_REASONS = (
    "amount_mismatch",
    "receipt_unreadable",
    "reference_not_found",
    "duplicate_claim",
    "not_received",
    "other",
)


class PaymentClaim(Base):
    """One attempt to pay, verified by a human.

    This table is the answer to "why does this account have access?", and it
    outlives revocation — rows are never deleted, exactly like crypto_payments.
    A claim also exists for the payment-link flow, where the money moved
    entirely outside the app and the admin creates the record after the fact;
    without that, half of all grants would carry no evidence at all.
    """

    __tablename__ = "payment_claims"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    # "bank_transfer" — the user transferred and uploaded a receipt.
    # "payment_link" — we emailed them a link; the admin files the claim.
    # "other" — anything else, explained in review_note.
    method: Mapped[str] = mapped_column(String(30), nullable=False, default="bank_transfer")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="submitted", index=True)

    # What the user says they sent, checked by eye against the receipt.
    amount_claimed: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")

    # Bank transaction id or transfer reference — the single most useful field
    # when reconciling against a bank statement.
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Often differs from the account holder: a family member's account, a
    # friend paying on someone's behalf. That mismatch is normal and is what
    # user_note exists to explain.
    payer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    paid_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    user_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Object key in the bucket. Never a URL — the bucket is private and reads
    # are short-lived presigned links generated per request.
    receipt_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    receipt_mime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    receipt_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    receipt_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the object is purged under the retention policy; the metadata
    # above stays as the record that a receipt once existed.
    receipt_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Shown to the user.
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Internal only, never serialised to the customer-facing API.
    internal_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship("User", back_populates="payment_claims", foreign_keys=[user_id])
    reviewer: Mapped["User | None"] = relationship("User", foreign_keys=[reviewed_by])

    __table_args__ = (
        # The queue read: pending claims, oldest first. It runs constantly.
        Index("ix_payment_claims_status_created", "status", "created_at"),
        Index("ix_payment_claims_user_created", "user_id", "created_at"),
        # One open claim per user. Partial, so a rejected claim never blocks a
        # corrected resubmission.
        Index(
            "uq_payment_claims_one_open_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('submitted', 'under_review')"),
        ),
        CheckConstraint(
            "status IN ('submitted','under_review','approved','rejected','cancelled')",
            name="ck_payment_claims_status",
        ),
    )


class TrialGrant(Base):
    """Insert-once record that a device has had its free trial.

    Deliberately not folded into TrialCounter: that table's `user_id` is
    UNIQUE and gets *reassigned* when a second account registers on the same
    fingerprint (app/routers/auth.py), so it can never testify that a device
    was already granted a trial. Here the unique constraint on the fingerprint
    is the arbiter, which also makes the grant race-safe without a
    read-then-write.
    """

    __tablename__ = "trial_grants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_fingerprint: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSettings(Base):
    """Runtime-editable billing configuration. Exactly one row, id = 1.

    In the database rather than config.py on purpose: bank accounts get frozen
    and numbers change, and neither a price change nor a new account number
    should require `railway up` and a cold start.
    """

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    one_time_price_usd: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=99.00)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")

    # Markdown, rendered on the paywall.
    bank_details_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payment_instructions_md: Mapped[str] = mapped_column(Text, nullable=False, default="")

    support_email: Mapped[str] = mapped_column(String(255), nullable=False, default="support@peptora.io")
    # Where new-claim notifications go. Here rather than in config so it can
    # change without a deploy.
    claims_notify_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Drives both the copy on the form and the overdue highlight in the queue.
    review_sla_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)

    manual_payments_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # The flag that parks the NOWPayments rail without deleting it.
    crypto_payments_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CycleLog(Base):
    __tablename__ = "cycle_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    protocol_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("user_protocols.id", ondelete="CASCADE"), nullable=True)
    peptide_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dose: Mapped[str] = mapped_column(String(100), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship("User", back_populates="cycle_logs")
    protocol: Mapped["UserProtocol | None"] = relationship("UserProtocol", back_populates="dose_logs")

    __table_args__ = (
        Index("ix_cycle_logs_user_taken", "user_id", "taken_at"),
        Index("ix_cycle_logs_protocol_id", "protocol_id"),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    extra_data: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    user: Mapped["User | None"] = relationship("User", back_populates="audit_logs")


# ---------------------------------------------------------------------------
# Peptide knowledge base
# ---------------------------------------------------------------------------

# Shared enum type objects — reused across multiple tables to avoid duplicate
# PostgreSQL type creation. create_type=False on repeated usages.
_evidence_level_enum = SAEnum(
    "preclinical", "early-human", "established", "anecdotal", "unknown",
    name="peptide_evidence_level_enum",
)


class Peptide(Base):
    __tablename__ = "peptides"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")

    category: Mapped[str] = mapped_column(
        SAEnum("healing", "growth-hormone", "metabolic", "cognitive", "cosmetic",
               "longevity", "immune", "sexual-health", "other",
               name="peptide_category_enum"),
        nullable=False,
    )
    usage_category: Mapped[str | None] = mapped_column(
        SAEnum("clinical", "research", "investigational", "banned-in-sport",
               name="peptide_usage_category_enum"),
        nullable=True,
    )
    approval_category: Mapped[str | None] = mapped_column(
        SAEnum("approved-drug", "research-chemical", "compounded", "supplement", "unapproved",
               name="peptide_approval_category_enum"),
        nullable=True,
    )

    # overview
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    mechanism_of_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    mechanism_citation_refs: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False, server_default="{}")

    # chemistry
    molecular_weight: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    molecular_formula: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cas_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pubchem_cid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sequence: Mapped[str | None] = mapped_column(Text, nullable=True)
    sequence_type: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # pharmacology
    half_life: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    bioavailability: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    routes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    default_dose_unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    iu_per_mg: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)

    # evidence
    evidence_level: Mapped[str] = mapped_column(
        SAEnum("preclinical", "early-human", "established", "anecdotal", "unknown",
               name="peptide_evidence_level_enum", create_type=False),
        nullable=False, server_default="unknown",
    )
    human_trials: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    clinical_trials_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    evidence_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # regulatory
    fda_status: Mapped[str] = mapped_column(
        SAEnum("approved", "not-approved", "withdrawn", "investigational", "unknown",
               name="peptide_fda_status_enum"),
        nullable=False, server_default="unknown",
    )
    fda_status_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    compounding_status: Mapped[str | None] = mapped_column(
        SAEnum("503a-listed", "503b-listed", "removed-503a", "not-eligible", "unknown", "not-applicable",
               name="peptide_compounding_enum"),
        nullable=True,
    )
    compounding_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    wada_status: Mapped[str | None] = mapped_column(
        SAEnum("prohibited", "prohibited-in-competition", "not-listed", "unknown",
               name="peptide_wada_status_enum"),
        nullable=True,
    )
    scheduled_controlled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    research_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    regulatory_citation_refs: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False, server_default="{}")

    # effect lists — arrays of structured objects
    benefits: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    risks: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    side_effects: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    contraindications: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    interactions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")

    # storage / reconstitution
    reconstitution: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    storage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # meta
    last_reviewed: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    content_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    data_completeness: Mapped[str] = mapped_column(
        SAEnum("stub", "partial", "complete", name="peptide_data_completeness_enum"),
        nullable=False, server_default="stub",
    )
    disclaimer: Mapped[str | None] = mapped_column(Text, nullable=True)

    # maintained by DB trigger; never set by application code
    search_tsv: Mapped[Any] = mapped_column(TSVECTOR, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    references: Mapped[list["PeptideReference"]] = relationship(
        "PeptideReference", back_populates="peptide", cascade="all, delete-orphan"
    )
    dose_ranges: Mapped[list["PeptideDoseRange"]] = relationship(
        "PeptideDoseRange", back_populates="peptide", cascade="all, delete-orphan"
    )
    protocols: Mapped[list["PeptideProtocol"]] = relationship(
        "PeptideProtocol", back_populates="peptide", cascade="all, delete-orphan"
    )
    related_peptides: Mapped[list["PeptideRelated"]] = relationship(
        "PeptideRelated", foreign_keys="PeptideRelated.peptide_id",
        back_populates="peptide", cascade="all, delete-orphan"
    )
    stack_memberships: Mapped[list["StackComponent"]] = relationship(
        "StackComponent", back_populates="peptide", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_peptides_category", "category"),
        Index("ix_peptides_evidence_level", "evidence_level"),
        Index("ix_peptides_search_tsv", "search_tsv", postgresql_using="gin"),
        Index("ix_peptides_tags", "tags", postgresql_using="gin"),
        Index("ix_peptides_aliases", "aliases", postgresql_using="gin"),
    )

    @property
    def featured_in_stacks(self) -> list[dict]:
        return [
            {
                "stack_id": m.stack_id,
                "stack_name": m.stack.name,
                "stack_type": m.stack.stack_type,
                "role": m.role,
                "ratio_parts": m.ratio_parts,
            }
            for m in self.stack_memberships
        ]


class PeptideReference(Base):
    __tablename__ = "peptide_references"

    peptide_id: Mapped[str] = mapped_column(
        String, ForeignKey("peptides.id", ondelete="CASCADE"), primary_key=True
    )
    ref_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(
        SAEnum("journal-article", "review", "clinical-trial", "regulatory-document",
               "book", "database", "other", name="peptide_ref_type_enum"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    first_author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    year: Mapped[str | None] = mapped_column(String(10), nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pmid: Mapped[str | None] = mapped_column(String(20), nullable=True)
    doi: Mapped[str | None] = mapped_column(String(200), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)

    peptide: Mapped["Peptide"] = relationship("Peptide", back_populates="references")

    __table_args__ = (
        Index("ix_peptide_references_peptide_id", "peptide_id"),
    )


class PeptideDoseRange(Base):
    __tablename__ = "peptide_dose_ranges"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    peptide_id: Mapped[str] = mapped_column(
        String, ForeignKey("peptides.id", ondelete="CASCADE"), nullable=False
    )
    context: Mapped[str] = mapped_column(Text, nullable=False)
    low: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    high: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    route: Mapped[str | None] = mapped_column(String(50), nullable=True)
    frequency: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    citation_refs: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False, server_default="{}")

    peptide: Mapped["Peptide"] = relationship("Peptide", back_populates="dose_ranges")

    __table_args__ = (
        Index("ix_peptide_dose_ranges_peptide_id", "peptide_id"),
    )


class PeptideProtocol(Base):
    __tablename__ = "peptide_protocols"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    peptide_id: Mapped[str] = mapped_column(
        String, ForeignKey("peptides.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    phase: Mapped[str | None] = mapped_column(String(100), nullable=True)
    duration_weeks: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    dosing: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    cycling_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_recommendation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    disclaimer: Mapped[str | None] = mapped_column(Text, nullable=True)
    citation_refs: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False, server_default="{}")

    peptide: Mapped["Peptide"] = relationship("Peptide", back_populates="protocols")

    __table_args__ = (
        Index("ix_peptide_protocols_peptide_id", "peptide_id"),
    )


class PeptideRelated(Base):
    __tablename__ = "peptide_related"

    peptide_id: Mapped[str] = mapped_column(
        String, ForeignKey("peptides.id", ondelete="CASCADE"), primary_key=True
    )
    related_peptide_id: Mapped[str] = mapped_column(
        String, ForeignKey("peptides.id", ondelete="CASCADE"), primary_key=True
    )
    relation_type: Mapped[str] = mapped_column(
        "relationship",
        SAEnum("commonly-studied-alongside", "same-class", "precursor", "analog", "alternative",
               name="peptide_relationship_enum"),
        primary_key=True, nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    peptide: Mapped["Peptide"] = relationship(
        "Peptide", foreign_keys="[PeptideRelated.peptide_id]", back_populates="related_peptides"
    )

    __table_args__ = (
        CheckConstraint("peptide_id <> related_peptide_id", name="ck_peptide_related_no_self"),
    )


class PeptideStack(Base):
    __tablename__ = "peptide_stacks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    stack_type: Mapped[str] = mapped_column(
        SAEnum("research_pairing", "commercial_blend", name="stack_type_enum"),
        nullable=False,
    )
    category: Mapped[str | None] = mapped_column(
        SAEnum("healing", "growth-hormone", "metabolic", "cognitive", "cosmetic",
               "longevity", "immune", "sexual-health", "other",
               name="peptide_category_enum", create_type=False),
        nullable=True,
    )
    positioning: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_level: Mapped[str] = mapped_column(
        SAEnum("preclinical", "early-human", "established", "anecdotal", "unknown",
               name="peptide_evidence_level_enum", create_type=False),
        nullable=False, server_default="anecdotal",
    )
    is_recommendation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Layer C fields — only populated for commercial_blend (enforced by CHECK constraints below).
    ratio_source_type: Mapped[str | None] = mapped_column(
        SAEnum("vendor-listing", "manufacturer-label", "community-convention",
               name="ratio_source_type_enum"),
        nullable=True,
    )
    ratio_source_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    ratio_source_urls: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    common_total_mg_options: Mapped[list[float]] = mapped_column(ARRAY(Numeric), nullable=False, server_default="{}")

    caution_notes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    disclaimer: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_reviewed: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    content_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    data_completeness: Mapped[str] = mapped_column(String, nullable=False, server_default="stub")

    # maintained by DB trigger; never set by application code
    search_tsv: Mapped[Any] = mapped_column(TSVECTOR, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    components: Mapped[list["StackComponent"]] = relationship(
        "StackComponent", back_populates="stack", cascade="all, delete-orphan",
        order_by="StackComponent.sort_order",
    )
    stack_references: Mapped[list["StackReference"]] = relationship(
        "StackReference", back_populates="stack", cascade="all, delete-orphan",
        order_by="StackReference.ref_id",
    )

    __table_args__ = (
        Index("ix_stacks_type", "stack_type"),
        Index("ix_stacks_category", "category"),
        CheckConstraint("is_recommendation = false", name="peptide_stacks_never_recommendation"),
        CheckConstraint(
            "stack_type <> 'commercial_blend' OR ratio_source_type IS NOT NULL",
            name="peptide_stacks_blend_needs_source",
        ),
        CheckConstraint(
            "stack_type <> 'research_pairing' OR ("
            "ratio_source_type IS NULL AND ratio_source_note IS NULL AND common_total_mg_options = '{}'"
            ")",
            name="peptide_stacks_pairing_no_ratio_source",
        ),
    )


class StackComponent(Base):
    __tablename__ = "stack_components"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stack_id: Mapped[str] = mapped_column(
        String, ForeignKey("peptide_stacks.id", ondelete="CASCADE"), nullable=False
    )
    peptide_id: Mapped[str] = mapped_column(
        String, ForeignKey("peptides.id", ondelete="CASCADE"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # NULL for research_pairing (no vial => no ratio to state).
    ratio_parts: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    # Purely informational (Layer C) — never the source of truth for the calculator.
    typical_mg_share: Mapped[float | None] = mapped_column(Numeric, nullable=True)

    role: Mapped[str | None] = mapped_column(Text, nullable=True)
    dose_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    stack: Mapped["PeptideStack"] = relationship("PeptideStack", back_populates="components")
    peptide: Mapped["Peptide"] = relationship("Peptide", back_populates="stack_memberships")

    __table_args__ = (
        UniqueConstraint("stack_id", "peptide_id"),
        Index("ix_stack_components_stack", "stack_id"),
        Index("ix_stack_components_peptide", "peptide_id"),
        CheckConstraint("ratio_parts IS NULL OR ratio_parts > 0", name="ck_stack_components_ratio_positive"),
        CheckConstraint(
            "typical_mg_share IS NULL OR typical_mg_share > 0", name="ck_stack_components_mg_share_positive"
        ),
    )

    @property
    def peptide_name(self) -> str:
        return self.peptide.name

    @property
    def peptide_category(self) -> str:
        return self.peptide.category

    @property
    def peptide_evidence_level(self) -> str:
        return self.peptide.evidence_level

    @property
    def reference_dose_ranges(self) -> list["PeptideDoseRange"]:
        return self.peptide.dose_ranges


class StackReference(Base):
    __tablename__ = "stack_references"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stack_id: Mapped[str] = mapped_column(
        String, ForeignKey("peptide_stacks.id", ondelete="CASCADE"), nullable=False
    )
    ref_id: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(
        SAEnum("journal-article", "review", "clinical-trial", "regulatory-document",
               "book", "database", "other", name="peptide_ref_type_enum", create_type=False),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    first_author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    year: Mapped[str | None] = mapped_column(String(10), nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pmid: Mapped[str | None] = mapped_column(String(20), nullable=True)
    doi: Mapped[str | None] = mapped_column(String(200), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)

    stack: Mapped["PeptideStack"] = relationship("PeptideStack", back_populates="stack_references")

    __table_args__ = (
        UniqueConstraint("stack_id", "ref_id"),
        Index("ix_stack_references_stack", "stack_id"),
    )


# ---------------------------------------------------------------------------
# User protocols (§10 — save feature)
# ---------------------------------------------------------------------------

class UserProtocol(Base):
    __tablename__ = "user_protocols"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    peptide_id: Mapped[str | None] = mapped_column(String, ForeignKey("peptides.id", ondelete="SET NULL"), nullable=True)
    peptide_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stack_id: Mapped[str | None] = mapped_column(String, ForeignKey("peptide_stacks.id", ondelete="SET NULL"), nullable=True)
    stack_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    vial_mg: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False)
    reconstituted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bac_water_ml: Mapped[float | None] = mapped_column(Numeric(10, 3), nullable=True)
    target_dose_mcg: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False, server_default="mcg")
    syringe_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="U-100")
    frequency: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    duration_weeks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    dose_logs: Mapped[list["CycleLog"]] = relationship("CycleLog", back_populates="protocol", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_user_protocols_user_id", "user_id"),
        CheckConstraint("vial_mg > 0", name="ck_user_protocols_vial_mg_positive"),
        CheckConstraint("target_dose_mcg > 0", name="ck_user_protocols_dose_positive"),
        CheckConstraint("status IN ('active','paused','completed')", name="ck_user_protocols_status"),
        CheckConstraint("peptide_id IS NULL OR stack_id IS NULL", name="ck_user_protocols_single_target"),
    )
