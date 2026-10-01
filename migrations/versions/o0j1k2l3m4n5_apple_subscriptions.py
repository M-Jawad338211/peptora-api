"""App Store subscriptions for the iOS app

Peptora Pro is sold in the iOS app as an auto-renewable subscription
(monthly and yearly), bought through Apple In-App Purchase. Two things are
added:

  users.apple_sub_until
        the latest unrevoked expiry across the user's App Store
        subscriptions. has_access() reads it, alongside the lifetime licence,
        the dormant crypto window and the trial.
  apple_subscriptions
        one row per subscription, keyed by Apple's originalTransactionId and
        kept current by signed transactions from the app and by App Store
        Server Notifications.

Nothing existing changes shape and no data is backfilled: no account has an
App Store subscription before this ships.

Revision ID: o0j1k2l3m4n5
Revises: n9i0j1k2l3m4
Create Date: 2026-10-01
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "o0j1k2l3m4n5"
down_revision = "n9i0j1k2l3m4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("apple_sub_until", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_apple_sub_until", "users", ["apple_sub_until"])

    op.create_table(
        "apple_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("original_transaction_id", sa.String(length=64), nullable=False),
        sa.Column("latest_transaction_id", sa.String(length=64), nullable=True),
        sa.Column("product_id", sa.String(length=255), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False, server_default="Production"),
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grace_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_trial_period", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("auto_renew", sa.Boolean(), nullable=True),
        sa.Column("app_account_token", sa.String(length=64), nullable=True),
        sa.Column("last_notification_type", sa.String(length=60), nullable=True),
        sa.Column("last_notification_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("original_transaction_id", name="uq_apple_subscriptions_original_transaction_id"),
    )
    op.create_index("ix_apple_subscriptions_user_id", "apple_subscriptions", ["user_id"])
    op.create_index("ix_apple_subscriptions_expires_at", "apple_subscriptions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_apple_subscriptions_expires_at", table_name="apple_subscriptions")
    op.drop_index("ix_apple_subscriptions_user_id", table_name="apple_subscriptions")
    op.drop_table("apple_subscriptions")
    op.drop_index("ix_users_apple_sub_until", table_name="users")
    op.drop_column("users", "apple_sub_until")
