import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    String, Boolean, Integer, DateTime, Text, JSON,
    ForeignKey, Numeric, UniqueConstraint, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    plan: Mapped[str] = mapped_column(String(50), default="free", nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    sessions: Mapped[list["Session"]] = relationship("Session", back_populates="user")
    trial_counter: Mapped["TrialCounter | None"] = relationship("TrialCounter", back_populates="user", uselist=False)
    calculator_usages: Mapped[list["CalculatorUsage"]] = relationship("CalculatorUsage", back_populates="user")
    subscriptions: Mapped[list["Subscription"]] = relationship("Subscription", back_populates="user")
    audit_logs: Mapped[list["AuditLog"]] = relationship("AuditLog", back_populates="user")


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
    result_units: Mapped[float] = mapped_column(Numeric(10, 2))
    result_ml: Mapped[float] = mapped_column(Numeric(10, 4))
    platform: Mapped[str] = mapped_column(String(50), default="web")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    user: Mapped["User | None"] = relationship("User", back_populates="calculator_usages")
    session: Mapped["Session | None"] = relationship("Session", back_populates="calculator_usages")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    stripe_subscription_id: Mapped[str] = mapped_column(String(255), unique=True)
    stripe_price_id: Mapped[str] = mapped_column(String(255))
    plan_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50), default="active")
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship("User", back_populates="subscriptions")

    __table_args__ = (
        Index("ix_sub_stripe_id", "stripe_subscription_id"),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    user: Mapped["User | None"] = relationship("User", back_populates="audit_logs")
