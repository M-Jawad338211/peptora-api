from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Any, Literal, Optional
from datetime import date, datetime
import uuid


# ── Peptide encyclopedia ────────────────────────────────────────────────────

class PeptideCard(BaseModel):
    id: str
    name: str
    aliases: list[str]
    tags: list[str]
    category: str
    usage_category: Optional[str]
    approval_category: Optional[str]
    summary: str
    evidence_level: str
    fda_status: str
    compounding_status: Optional[str]
    wada_status: Optional[str]
    research_only: bool
    data_completeness: str
    default_dose_unit: Optional[str] = None
    iu_per_mg: Optional[float] = None

    model_config = {"from_attributes": True}


class PeptideReferenceOut(BaseModel):
    ref_id: int
    type: str
    title: str
    first_author: Optional[str]
    year: Optional[str]
    source: Optional[str]
    pmid: Optional[str]
    doi: Optional[str]
    url: Optional[str]

    model_config = {"from_attributes": True}


class PeptideDoseRangeOut(BaseModel):
    id: int
    context: str
    low: Optional[float]
    high: Optional[float]
    unit: str
    route: Optional[str]
    frequency: Optional[str]
    note: Optional[str]
    citation_refs: list[int]

    model_config = {"from_attributes": True}


class PeptideProtocolOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    phase: Optional[str]
    duration_weeks: Optional[Any]
    dosing: Optional[Any]
    cycling_notes: Optional[str]
    is_recommendation: bool
    disclaimer: Optional[str]
    citation_refs: list[int]

    model_config = {"from_attributes": True}


class PeptideRelatedOut(BaseModel):
    related_peptide_id: str
    relation_type: str
    note: Optional[str]

    model_config = {"from_attributes": True}


class StackFeaturedIn(BaseModel):
    stack_id: str
    stack_name: str
    stack_type: str
    role: Optional[str]
    ratio_parts: Optional[float]

    model_config = {"from_attributes": True}


class PeptideDetail(BaseModel):
    id: str
    name: str
    aliases: list[str]
    tags: list[str]
    category: str
    usage_category: Optional[str]
    approval_category: Optional[str]

    summary: str
    description: Optional[str]
    mechanism_of_action: Optional[str]
    mechanism_citation_refs: list[int]

    molecular_weight: Optional[float]
    molecular_formula: Optional[str]
    cas_number: Optional[str]
    pubchem_cid: Optional[int]
    sequence: Optional[str]
    sequence_type: Optional[str]

    half_life: Optional[Any]
    bioavailability: Optional[Any]
    routes: list[str]
    default_dose_unit: Optional[str]
    iu_per_mg: Optional[float] = None

    evidence_level: str
    human_trials: bool
    clinical_trials_count: int
    evidence_note: Optional[str]

    fda_status: str
    fda_status_note: Optional[str]
    compounding_status: Optional[str]
    compounding_note: Optional[str]
    wada_status: Optional[str]
    scheduled_controlled: bool
    research_only: bool
    regulatory_citation_refs: list[int]

    benefits: list[Any]
    risks: list[Any]
    side_effects: list[Any]
    contraindications: list[Any]
    interactions: list[Any]

    reconstitution: Optional[Any]
    storage: Optional[Any]

    last_reviewed: date
    reviewed_by: Optional[str]
    content_version: int
    data_completeness: str
    disclaimer: Optional[str]

    references: list[PeptideReferenceOut]
    dose_ranges: list[PeptideDoseRangeOut]
    protocols: list[PeptideProtocolOut]
    related_peptides: list[PeptideRelatedOut]
    featured_in_stacks: list[StackFeaturedIn]

    model_config = {"from_attributes": True}


# ── Peptide stacks / blends ─────────────────────────────────────────────────

class StackCard(BaseModel):
    id: str
    name: str
    aliases: list[str]
    stack_type: str
    category: Optional[str]
    positioning: Optional[str]
    evidence_level: str
    data_completeness: str

    model_config = {"from_attributes": True}


class StackComponentDoseRangeOut(BaseModel):
    context: str
    low: Optional[float]
    high: Optional[float]
    unit: str
    route: Optional[str]
    frequency: Optional[str]
    note: Optional[str]
    citation_refs: list[int]

    model_config = {"from_attributes": True}


class StackComponentOut(BaseModel):
    sort_order: int
    peptide_id: str
    peptide_name: str
    peptide_category: str
    peptide_evidence_level: str
    ratio_parts: Optional[float]
    typical_mg_share: Optional[float]
    role: Optional[str]
    dose_note: Optional[str]
    reference_dose_ranges: list[StackComponentDoseRangeOut]

    model_config = {"from_attributes": True}


class StackReferenceOut(BaseModel):
    ref_id: int
    type: str
    title: str
    first_author: Optional[str]
    year: Optional[str]
    source: Optional[str]
    pmid: Optional[str]
    doi: Optional[str]
    url: Optional[str]

    model_config = {"from_attributes": True}


class StackDetail(BaseModel):
    id: str
    name: str
    aliases: list[str]
    stack_type: str
    category: Optional[str]
    positioning: Optional[str]
    rationale: Optional[str]
    evidence_level: str
    is_recommendation: bool

    ratio_source_type: Optional[str]
    ratio_source_note: Optional[str]
    ratio_source_urls: list[str]
    common_total_mg_options: list[float]

    caution_notes: list[str]
    disclaimer: Optional[str]
    last_reviewed: date
    reviewed_by: Optional[str]
    content_version: int
    data_completeness: str

    components: list[StackComponentOut]
    stack_references: list[StackReferenceOut]

    model_config = {"from_attributes": True}


# ── Auth ────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    confirm_password: str
    full_name: str
    device_fingerprint: str

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

    @field_validator("full_name")
    @classmethod
    def full_name_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Full name is required")
        return v.strip()

    @model_validator(mode="after")
    def passwords_match(self) -> "RegisterRequest":
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    otp: str

    @field_validator("otp")
    @classmethod
    def otp_format(cls, v: str) -> str:
        clean = v.strip()
        if len(clean) != 6 or not clean.isdigit():
            raise ValueError("OTP must be a 6-digit code")
        return clean


class ResendVerificationOTPRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class TrialCountInfo(BaseModel):
    anonymous_uses: int
    free_uses: int
    signup_bonus_granted: bool


class AccessInfo(BaseModel):
    """The user's access state, flattened for the client.

    `has_access` is computed server-side and is what the UI must gate on —
    a client that recomputes it from the dates will disagree with the API
    across clock skew and get stuck showing a paywall to a paying user.
    """
    has_access: bool
    is_trial: bool
    trial_ends_at: Optional[datetime] = None
    paid_until: Optional[datetime] = None
    days_remaining: Optional[int] = None

    # Purchased outright. Suppresses every countdown and renewal nudge —
    # there is nothing left to expire.
    is_lifetime: bool = False
    is_revoked: bool = False

    # The user's open or most recently resolved claim, so the paywall can
    # render the right state without a second request on first paint.
    claim_status: Optional[str] = None
    claim_id: Optional[uuid.UUID] = None
    # False while a claim is pending, so the form cannot be submitted twice.
    can_submit_claim: bool = True


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: Optional[str]
    plan: str
    is_admin: bool
    email_verified: bool
    consent_accepted: bool = False
    trial_count: Optional[TrialCountInfo] = None
    access: Optional[AccessInfo] = None

    model_config = {"from_attributes": True}


# ── Calculator ──────────────────────────────────────────────────────────────

class TrialCheckRequest(BaseModel):
    device_fingerprint: str
    platform: str = "web"


class TrialCheckResponse(BaseModel):
    allowed: bool
    reason: str
    remaining: Optional[int] = None
    uses_so_far: Optional[int] = None


class RecordUseRequest(BaseModel):
    device_fingerprint: str
    platform: str = "web"
    peptide_name: str
    vial_mg: float
    bac_water_ml: float
    target_mcg: float
    result_units: Optional[float] = None
    result_ml: Optional[float] = None
    draw_ml: Optional[float] = None


class RecordUseResponse(BaseModel):
    recorded: bool
    new_count: int


class CalculatorHistoryItem(BaseModel):
    id: uuid.UUID
    peptide_name: str
    vial_mg: float
    bac_water_ml: float
    target_mcg: float
    result_units: Optional[float] = None
    result_ml: Optional[float] = None
    draw_ml: Optional[float] = None
    platform: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Subscriptions ───────────────────────────────────────────────────────────

class CreateCheckoutRequest(BaseModel):
    # Defaults to annual: at $5 the monthly plan sits at or under the minimum
    # payment amount on most chains. The native app posts this field
    # explicitly, so both values stay accepted.
    plan: Literal["monthly", "annual"] = "annual"


class CheckoutResponse(BaseModel):
    # Named checkout_url, not invoice_url, so peptora-android/app/paywall.js
    # keeps working unchanged across the Stripe → NOWPayments switch.
    checkout_url: str
    order_id: str


class PlanOption(BaseModel):
    id: str
    label: str
    price_usd: float
    days: int


class SubscriptionStatusResponse(BaseModel):
    plan: str
    access: AccessInfo
    plans: list[PlanOption]
    payments_enabled: bool


# ── AI ──────────────────────────────────────────────────────────────────────

class ConversationMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class AIAssistantRequest(BaseModel):
    message: str
    conversation_history: list[ConversationMessage] = []


class AIAssistantResponse(BaseModel):
    reply: str


class StackCheckRequest(BaseModel):
    peptides: list[str]


class StackCheckResponse(BaseModel):
    compatibility: str
    analysis: str
    timing_recommendations: str
    known_conflicts: list[str]


# ── Tracker ─────────────────────────────────────────────────────────────────

class CycleLogCreate(BaseModel):
    peptide_name: str
    dose: str
    notes: Optional[str] = None
    taken_at: Optional[datetime] = None


class CycleLogItem(BaseModel):
    id: uuid.UUID
    peptide_name: str
    dose: str
    notes: Optional[str] = None
    taken_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Push Notifications ───────────────────────────────────────────────────────

class PushTokenUpdate(BaseModel):
    token: str

    @field_validator("token")
    @classmethod
    def token_format(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("ExponentPushToken["):
            raise ValueError("Must be a valid Expo push token")
        return v


class CronReminderResult(BaseModel):
    sent: int
    failed: int
    skipped: int


# ── Admin ───────────────────────────────────────────────────────────────────

class AdminStatsResponse(BaseModel):
    # Leads with the numbers that represent someone currently waiting.
    pending_claims: int
    oldest_pending_hours: Optional[float] = None
    overdue_claims: int

    total_users: int
    lifetime_users: int
    trialing_users: int
    lapsed_users: int
    trials_ending_this_week: int

    calcs_today: int
    calcs_this_week: int
    calcs_this_month: int
    new_signups_today: int

    approved_last_7d: int
    rejected_last_7d: int
    # Real, from approved claims — the old field was hardcoded to 0.0 with a
    # comment pointing at a Stripe dashboard that never existed.
    revenue_last_30d: float
    revenue_all_time: float


class AdminUserItem(BaseModel):
    id: uuid.UUID
    email: str
    full_name: Optional[str]
    plan: str
    is_admin: bool
    created_at: datetime
    last_login: Optional[datetime]
    calc_uses_anonymous: int
    calc_uses_free: int

    # Derived server-side from has_access(), never recomputed in the UI.
    has_access: bool = False
    access_state: str = "none"  # lifetime | trial | crypto | lapsed | revoked | none
    lifetime_access_at: Optional[datetime] = None
    trial_ends_at: Optional[datetime] = None
    access_revoked_at: Optional[datetime] = None
    open_claim_id: Optional[uuid.UUID] = None

    model_config = {"from_attributes": True}


class AdminUserDetail(AdminUserItem):
    """One account, with everything needed to decide what to do about it."""
    email_verified: bool = False
    consent_accepted: bool = False
    paid_until: Optional[datetime] = None
    access_revoked_reason: Optional[str] = None
    signup_fingerprint: Optional[str] = None
    claims: list["AdminClaimItem"] = []


class AdminUserListResponse(BaseModel):
    """Paginated. The old endpoint returned a bare list with no total, so a UI
    could not render honest pagination."""
    items: list[AdminUserItem]
    total: int
    limit: int
    offset: int


# ── Manual billing ──────────────────────────────────────────────────────────

class PaymentInstructions(BaseModel):
    """Everything the paywall needs to render, in one call."""
    price: float
    currency: str
    bank_details_md: str
    payment_instructions_md: str
    support_email: str
    review_sla_hours: int
    manual_payments_enabled: bool
    crypto_payments_enabled: bool
    receipts_enabled: bool


class CreateClaimRequest(BaseModel):
    amount_claimed: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    currency: str = Field(default="USD", max_length=10)
    reference: Optional[str] = Field(default=None, max_length=255)
    payer_name: Optional[str] = Field(default=None, max_length=255)
    paid_at: Optional[date] = None
    user_note: Optional[str] = Field(default=None, max_length=2000)
    method: str = Field(default="bank_transfer")

    @field_validator("method")
    @classmethod
    def known_method(cls, v: str) -> str:
        if v not in ("bank_transfer", "payment_link", "other"):
            raise ValueError("Unknown payment method")
        return v


class PaymentClaimResponse(BaseModel):
    """The customer-facing view of a claim. Deliberately omits
    `internal_note` — that field is for admins only."""
    id: uuid.UUID
    status: str
    method: str
    amount_claimed: Optional[float] = None
    currency: str
    reference: Optional[str] = None
    payer_name: Optional[str] = None
    paid_at: Optional[date] = None
    user_note: Optional[str] = None
    has_receipt: bool = False
    rejection_reason: Optional[str] = None
    review_note: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AdminClaimItem(BaseModel):
    id: uuid.UUID
    status: str
    method: str
    user_id: uuid.UUID
    user_email: str
    user_full_name: Optional[str] = None
    amount_claimed: Optional[float] = None
    currency: str
    reference: Optional[str] = None
    payer_name: Optional[str] = None
    paid_at: Optional[date] = None
    has_receipt: bool = False
    created_at: datetime
    age_hours: float
    is_overdue: bool = False

    model_config = {"from_attributes": True}


class AdminClaimListResponse(BaseModel):
    items: list[AdminClaimItem]
    total: int
    limit: int
    offset: int


class AdminClaimDetail(AdminClaimItem):
    """Claim plus the account context needed to verify identity without
    navigating away from the receipt."""
    user_note: Optional[str] = None
    internal_note: Optional[str] = None
    review_note: Optional[str] = None
    rejection_reason: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    reviewer_email: Optional[str] = None
    receipt_mime: Optional[str] = None
    receipt_bytes: Optional[int] = None
    receipt_deleted_at: Optional[datetime] = None

    user_created_at: datetime
    user_has_access: bool
    user_access_state: str
    user_trial_ends_at: Optional[datetime] = None
    user_lifetime_access_at: Optional[datetime] = None
    # Prior claims by this user — a second rejected claim is a very different
    # situation from a first submission.
    prior_claims: int = 0
    prior_rejections: int = 0
    # Cheap identity signal, computed server-side so the admin does not have
    # to eyeball two strings in different parts of the page.
    payer_name_matches: Optional[bool] = None


class ApproveClaimRequest(BaseModel):
    internal_note: Optional[str] = Field(default=None, max_length=2000)


class RejectClaimRequest(BaseModel):
    reason: str
    review_note: Optional[str] = Field(default=None, max_length=2000)
    internal_note: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("reason")
    @classmethod
    def known_reason(cls, v: str) -> str:
        allowed = (
            "amount_mismatch", "receipt_unreadable", "reference_not_found",
            "duplicate_claim", "not_received", "other",
        )
        if v not in allowed:
            raise ValueError(f"reason must be one of {', '.join(allowed)}")
        return v


class GrantAccessRequest(BaseModel):
    """The payment-link path: the money moved entirely outside the app, so
    the admin files the record after the fact."""
    amount: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    currency: str = Field(default="USD", max_length=10)
    reference: Optional[str] = Field(default=None, max_length=255)
    note: Optional[str] = Field(default=None, max_length=2000)


class RevokeAccessRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class AdminSettingsResponse(BaseModel):
    one_time_price_usd: float
    currency: str
    bank_details_md: str
    payment_instructions_md: str
    support_email: str
    claims_notify_email: Optional[str] = None
    review_sla_hours: int
    manual_payments_enabled: bool
    crypto_payments_enabled: bool
    updated_at: datetime

    model_config = {"from_attributes": True}


class AdminSettingsUpdate(BaseModel):
    one_time_price_usd: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    currency: Optional[str] = Field(default=None, max_length=10)
    bank_details_md: Optional[str] = Field(default=None, max_length=20000)
    payment_instructions_md: Optional[str] = Field(default=None, max_length=20000)
    support_email: Optional[EmailStr] = None
    claims_notify_email: Optional[EmailStr] = None
    review_sla_hours: Optional[int] = Field(default=None, ge=1, le=720)
    manual_payments_enabled: Optional[bool] = None
    crypto_payments_enabled: Optional[bool] = None


class AdminAuditItem(BaseModel):
    id: uuid.UUID
    action: str
    user_id: Optional[uuid.UUID] = None
    user_email: Optional[str] = None
    extra_data: Optional[dict] = None
    platform: Optional[str] = None
    created_at: datetime


class VendorUpdate(BaseModel):
    name: str
    status: str  # "active" | "warning" | "shutdown" | "scam"
    notes: Optional[str] = None


class RegulatoryUpdate(BaseModel):
    peptide: str
    fda_category: str
    compounding_legal: bool
    wada_banned: bool
    notes: Optional[str] = None


# ── User Protocols ────────────────────────────────────────────────────────────

class UserProtocolCreate(BaseModel):
    peptide_id: Optional[str] = None
    peptide_name: Optional[str] = None
    stack_id: Optional[str] = None
    stack_name: Optional[str] = None
    label: Optional[str] = None
    status: str = "active"
    vial_mg: float
    reconstituted: bool
    bac_water_ml: Optional[float] = None
    target_dose_mcg: float
    unit: str = "mcg"
    syringe_type: str = "U-100"
    frequency: Optional[str] = None
    start_date: Optional[date] = None
    duration_weeks: Optional[int] = None
    notes: Optional[str] = None

    @field_validator("vial_mg")
    @classmethod
    def vial_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("vial_mg must be > 0")
        return v

    @field_validator("target_dose_mcg")
    @classmethod
    def dose_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("target_dose_mcg must be > 0")
        return v

    @field_validator("status")
    @classmethod
    def status_valid(cls, v: str) -> str:
        if v not in ("active", "paused", "completed"):
            raise ValueError("status must be active, paused, or completed")
        return v

    @model_validator(mode="after")
    def single_target(self) -> "UserProtocolCreate":
        if self.peptide_id and self.stack_id:
            raise ValueError("A protocol can target a peptide or a stack, not both")
        return self


class UserProtocolUpdate(BaseModel):
    label: Optional[str] = None
    stack_id: Optional[str] = None
    stack_name: Optional[str] = None
    status: Optional[str] = None
    frequency: Optional[str] = None
    start_date: Optional[date] = None
    duration_weeks: Optional[int] = None
    notes: Optional[str] = None
    vial_mg: Optional[float] = None
    reconstituted: Optional[bool] = None
    bac_water_ml: Optional[float] = None
    target_dose_mcg: Optional[float] = None
    unit: Optional[str] = None
    syringe_type: Optional[str] = None

    @field_validator("status")
    @classmethod
    def status_valid(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("active", "paused", "completed"):
            raise ValueError("status must be active, paused, or completed")
        return v


class DoseLogCreate(BaseModel):
    peptide_name: str
    dose: str
    notes: Optional[str] = None
    taken_at: Optional[datetime] = None


class DoseLogItem(BaseModel):
    id: uuid.UUID
    protocol_id: Optional[uuid.UUID]
    peptide_name: str
    dose: str
    notes: Optional[str]
    taken_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class UserProtocolItem(BaseModel):
    id: uuid.UUID
    peptide_id: Optional[str]
    peptide_name: Optional[str]
    stack_id: Optional[str]
    stack_name: Optional[str]
    label: Optional[str]
    status: str
    vial_mg: float
    reconstituted: bool
    bac_water_ml: Optional[float]
    target_dose_mcg: float
    unit: str
    syringe_type: str
    frequency: Optional[str]
    start_date: Optional[date]
    duration_weeks: Optional[int]
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UserProtocolDetail(UserProtocolItem):
    dose_logs: list[DoseLogItem] = []

    model_config = {"from_attributes": True}
