from pydantic import BaseModel, EmailStr, field_validator, model_validator
from typing import Any, Optional
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


class PeptideStackOut(BaseModel):
    partner_id: str
    compatibility: str
    rationale: Optional[str]
    evidence_level: Optional[str]
    citation_refs: list[int]

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
    stack_compatibility: list[PeptideStackOut]

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


class SubscriptionInfo(BaseModel):
    status: str
    current_period_end: Optional[datetime]
    cancel_at_period_end: bool


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: Optional[str]
    plan: str
    is_admin: bool
    email_verified: bool
    consent_accepted: bool = False
    trial_count: Optional[TrialCountInfo] = None
    subscription: Optional[SubscriptionInfo] = None

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
    plan: str  # "monthly" | "annual"


class CheckoutResponse(BaseModel):
    checkout_url: str


class PortalResponse(BaseModel):
    portal_url: str


class SubscriptionStatusResponse(BaseModel):
    plan: str
    status: Optional[str]
    current_period_end: Optional[datetime]
    cancel_at_period_end: bool


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
    total_users: int
    free_users: int
    pro_users: int
    calcs_today: int
    calcs_this_week: int
    calcs_this_month: int
    revenue_today: float
    new_signups_today: int


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

    model_config = {"from_attributes": True}


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


# ── User Protocols (§10) ─────────────────────────────────────────────────────

class UserProtocolCreate(BaseModel):
    peptide_id: Optional[str] = None
    label: Optional[str] = None
    vial_mg: float
    reconstituted: bool
    bac_water_ml: Optional[float] = None
    target_dose_mcg: float
    unit: str = "mcg"
    syringe_type: str = "U-100"
    frequency: Optional[str] = None

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


class UserProtocolItem(BaseModel):
    id: uuid.UUID
    peptide_id: Optional[str]
    label: Optional[str]
    vial_mg: float
    reconstituted: bool
    bac_water_ml: Optional[float]
    target_dose_mcg: float
    unit: str
    syringe_type: str
    frequency: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}
