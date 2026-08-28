"""replace Stripe subscriptions with crypto prepaid access windows

Drops the Stripe columns and the `subscriptions` table, adds the two
timestamps that now decide access (`trial_ends_at`, `paid_until`), and creates
`crypto_payments` for the NOWPayments invoice lifecycle.

Backfills a 14-day trial for every existing verified user. Without it, the
deploy that turns gating on would lock out the entire current userbase at the
moment it lands — nobody has a paid_until yet, so has_access() would be false
for all of them. This gives them the same runway a new signup gets, counted
from the migration rather than from their signup date.

Revision ID: k6f7g8h9i0j1
Revises: j5e6f7g8h9i0
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'k6f7g8h9i0j1'
down_revision = 'j5e6f7g8h9i0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('trial_ends_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('paid_until', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_users_paid_until', 'users', ['paid_until'])

    # Existing users keep working: same 14 days a new signup gets.
    op.execute("""
        UPDATE users
           SET trial_ends_at = NOW() + INTERVAL '14 days'
         WHERE email_verified = true
           AND trial_ends_at IS NULL
    """)

    op.create_table(
        'crypto_payments',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('order_id', sa.String(255), nullable=False, unique=True),
        sa.Column('plan', sa.String(50), nullable=False),
        sa.Column('np_invoice_id', sa.String(255), nullable=True),
        sa.Column('np_payment_id', sa.String(255), nullable=True, unique=True),
        sa.Column('price_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('price_currency', sa.String(20), nullable=False, server_default='usd'),
        sa.Column('pay_currency', sa.String(40), nullable=True),
        sa.Column('pay_amount', sa.Numeric(24, 8), nullable=True),
        sa.Column('actually_paid', sa.Numeric(24, 8), nullable=True),
        sa.Column('status', sa.String(40), nullable=False, server_default='waiting'),
        sa.Column('credited_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('raw_ipn', postgresql.JSONB, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_crypto_payments_np_invoice_id', 'crypto_payments', ['np_invoice_id'])
    op.create_index('ix_crypto_payments_created_at', 'crypto_payments', ['created_at'])
    op.create_index('ix_crypto_payment_user_created', 'crypto_payments', ['user_id', 'created_at'])

    op.drop_table('subscriptions')
    op.drop_column('users', 'stripe_customer_id')
    op.drop_column('users', 'stripe_subscription_id')


def downgrade() -> None:
    op.add_column('users', sa.Column('stripe_customer_id', sa.String(255), nullable=True))
    op.add_column('users', sa.Column('stripe_subscription_id', sa.String(255), nullable=True))

    op.create_table(
        'subscriptions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('stripe_subscription_id', sa.String(255), nullable=False, unique=True),
        sa.Column('stripe_price_id', sa.String(255), nullable=False),
        sa.Column('plan_name', sa.String(100), nullable=False),
        sa.Column('status', sa.String(50), nullable=False, server_default='active'),
        sa.Column('current_period_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancel_at_period_end', sa.Boolean, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_sub_stripe_id', 'subscriptions', ['stripe_subscription_id'])

    op.drop_index('ix_crypto_payment_user_created', table_name='crypto_payments')
    op.drop_index('ix_crypto_payments_created_at', table_name='crypto_payments')
    op.drop_index('ix_crypto_payments_np_invoice_id', table_name='crypto_payments')
    op.drop_table('crypto_payments')

    op.drop_index('ix_users_paid_until', table_name='users')
    op.drop_column('users', 'paid_until')
    op.drop_column('users', 'trial_ends_at')
