"""email_verification_otps

Revision ID: b9f1c4a7d2e8
Revises: 7a36870f8a9d
Create Date: 2026-05-06 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "b9f1c4a7d2e8"
down_revision: Union[str, None] = "7a36870f8a9d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_verification_otps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("otp_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_email_verification_user_created",
        "email_verification_otps",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_verification_otps_created_at"),
        "email_verification_otps",
        ["created_at"],
        unique=False,
    )
    op.execute("UPDATE users SET email_verified = true WHERE email_verified = false")
    op.alter_column("email_verification_otps", "attempts", server_default=None)


def downgrade() -> None:
    op.drop_index(op.f("ix_email_verification_otps_created_at"), table_name="email_verification_otps")
    op.drop_index("ix_email_verification_user_created", table_name="email_verification_otps")
    op.drop_table("email_verification_otps")
