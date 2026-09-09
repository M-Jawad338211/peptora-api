"""manual billing: one-time licences, payment claims, device-bound trials

Peptora moves from a prepaid crypto window to a one-time purchase verified by
a human. Three things change shape:

  users        gains a permanent licence (lifetime_access_at), a kill switch
               (access_revoked_at) and the signup fingerprint the trial binds
               against.
  payment_claims
               is the record of every attempt to pay and every decision made
               about it — the answer to "why does this account have access?".
  trial_grants insert-once proof that a device has had its free trial.
  app_settings runtime-editable price and bank details, so neither needs a
               redeploy.

Nothing is dropped. crypto_payments and paid_until stay: the NOWPayments rail
is parked behind app_settings.crypto_payments_enabled, not deleted.

There are no production payers to carry, so no user data is backfilled beyond
signup_fingerprint, which is recovered best-effort from trial_counters.

Revision ID: m8h9i0j1k2l3
Revises: l7g8h9i0j1k2
Create Date: 2026-09-07
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'm8h9i0j1k2l3'
down_revision = 'l7g8h9i0j1k2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── users ───────────────────────────────────────────────────────────────
    op.add_column('users', sa.Column('lifetime_access_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('access_revoked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('access_revoked_reason', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('signup_fingerprint', sa.String(length=255), nullable=True))
    op.create_index('ix_users_lifetime_access_at', 'users', ['lifetime_access_at'])
    op.create_index('ix_users_signup_fingerprint', 'users', ['signup_fingerprint'])

    # Best-effort recovery of the signup device from the existing counters.
    # Fallback fingerprints are shared across many devices, so binding a trial
    # to one would lock out every future user of that browser class — they are
    # deliberately excluded.
    op.execute("""
        UPDATE users u
           SET signup_fingerprint = tc.device_fingerprint
          FROM trial_counters tc
         WHERE tc.user_id = u.id
           AND tc.device_fingerprint NOT LIKE 'fallback-fp-%'
    """)

    # ── payment_claims ──────────────────────────────────────────────────────
    op.create_table(
        'payment_claims',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('method', sa.String(length=30), nullable=False, server_default='bank_transfer'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='submitted'),
        sa.Column('amount_claimed', sa.Numeric(12, 2), nullable=True),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default='USD'),
        sa.Column('reference', sa.String(length=255), nullable=True),
        sa.Column('payer_name', sa.String(length=255), nullable=True),
        sa.Column('paid_at', sa.Date(), nullable=True),
        sa.Column('user_note', sa.Text(), nullable=True),
        sa.Column('receipt_key', sa.String(length=500), nullable=True),
        sa.Column('receipt_mime', sa.String(length=100), nullable=True),
        sa.Column('receipt_bytes', sa.Integer(), nullable=True),
        sa.Column('receipt_uploaded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('receipt_deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reviewed_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('rejection_reason', sa.String(length=40), nullable=True),
        sa.Column('review_note', sa.Text(), nullable=True),
        sa.Column('internal_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint(
            "status IN ('submitted','under_review','approved','rejected','cancelled')",
            name='ck_payment_claims_status',
        ),
    )
    op.create_index('ix_payment_claims_status', 'payment_claims', ['status'])
    op.create_index('ix_payment_claims_created_at', 'payment_claims', ['created_at'])
    op.create_index('ix_payment_claims_reference', 'payment_claims', ['reference'])
    # The queue read: pending claims, oldest first. It runs constantly.
    op.create_index('ix_payment_claims_status_created', 'payment_claims', ['status', 'created_at'])
    op.create_index('ix_payment_claims_user_created', 'payment_claims', ['user_id', 'created_at'])
    # One open claim per user, so nobody floods the review queue by
    # resubmitting. Partial, so a rejected claim never blocks a corrected one.
    op.create_index(
        'uq_payment_claims_one_open_per_user',
        'payment_claims',
        ['user_id'],
        unique=True,
        postgresql_where=sa.text("status IN ('submitted', 'under_review')"),
    )

    # ── trial_grants ────────────────────────────────────────────────────────
    op.create_table(
        'trial_grants',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('device_fingerprint', sa.String(length=255), nullable=False, unique=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('granted_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )

    # Existing trialled users keep their trial, and their device is recorded
    # as spent — otherwise everyone already in the product could mint a second
    # trial the day this ships. ON CONFLICT because two accounts may already
    # share a fingerprint; the first one recorded wins.
    op.execute("""
        INSERT INTO trial_grants (id, device_fingerprint, user_id, granted_at)
        SELECT gen_random_uuid(), u.signup_fingerprint, u.id, COALESCE(u.created_at, now())
          FROM users u
         WHERE u.signup_fingerprint IS NOT NULL
           AND u.trial_ends_at IS NOT NULL
        ON CONFLICT (device_fingerprint) DO NOTHING
    """)

    # ── app_settings ────────────────────────────────────────────────────────
    op.create_table(
        'app_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('one_time_price_usd', sa.Numeric(12, 2), nullable=False, server_default='99.00'),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default='USD'),
        sa.Column('bank_details_md', sa.Text(), nullable=False, server_default=''),
        sa.Column('payment_instructions_md', sa.Text(), nullable=False, server_default=''),
        sa.Column('support_email', sa.String(length=255), nullable=False, server_default='support@peptora.io'),
        sa.Column('claims_notify_email', sa.String(length=255), nullable=True),
        sa.Column('review_sla_hours', sa.Integer(), nullable=False, server_default='24'),
        sa.Column('manual_payments_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('crypto_payments_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('updated_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )

    # Seeded with manual payments OFF. The placeholder bank details below are
    # not something a customer should ever be shown, and the admin panel
    # refuses to enable the rail until real ones are entered.
    op.execute("""
        INSERT INTO app_settings (id, payment_instructions_md, bank_details_md)
        VALUES (
            1,
            E'### How to pay\n\n1. Transfer the exact amount shown above to the account listed.\n2. Save the receipt or take a screenshot of the confirmation.\n3. Come back here, fill in the reference number and upload the receipt.\n\nWe check every payment by hand, so access is not instant. You will get an email the moment your licence is active.',
            E'_Bank details have not been configured yet._'
        )
        ON CONFLICT (id) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_table('app_settings')
    op.drop_table('trial_grants')
    op.drop_index('uq_payment_claims_one_open_per_user', table_name='payment_claims')
    op.drop_index('ix_payment_claims_user_created', table_name='payment_claims')
    op.drop_index('ix_payment_claims_status_created', table_name='payment_claims')
    op.drop_index('ix_payment_claims_reference', table_name='payment_claims')
    op.drop_index('ix_payment_claims_created_at', table_name='payment_claims')
    op.drop_index('ix_payment_claims_status', table_name='payment_claims')
    op.drop_table('payment_claims')
    op.drop_index('ix_users_signup_fingerprint', table_name='users')
    op.drop_index('ix_users_lifetime_access_at', table_name='users')
    op.drop_column('users', 'signup_fingerprint')
    op.drop_column('users', 'access_revoked_reason')
    op.drop_column('users', 'access_revoked_at')
    op.drop_column('users', 'lifetime_access_at')
