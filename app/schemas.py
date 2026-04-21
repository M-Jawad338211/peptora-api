from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional
from datetime import datetime
import uuid


# ── Auth ────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    device_fingerprint: str

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


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
    result_units: float
    result_ml: float


class RecordUseResponse(BaseModel):
    recorded: bool
    new_count: int


class CalculatorHistoryItem(BaseModel):
    id: uuid.UUID
    peptide_name: str
    vial_mg: float
    bac_water_ml: float
    target_mcg: float
    result_units: float
    result_ml: float
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
