"""create the bootstrap Peptora administrator

Revision ID: n9i0j1k2l3m4
Revises: m8h9i0j1k2l3
Create Date: 2026-09-08

This is deliberately an upsert: it works whether the address already exists
or a fresh production database is being deployed.  The stored value is a
bcrypt hash of the bootstrap password, never the plaintext password.
"""

from alembic import op


revision = "n9i0j1k2l3m4"
down_revision = "m8h9i0j1k2l3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO users (
            id, email, password_hash, full_name, plan, is_admin,
            email_verified, consent_accepted, created_at, updated_at
        )
        VALUES (
            gen_random_uuid(),
            'admin@peptora.io',
            '$2y$12$vjtwN817.LETMYHeubS45O2xwEli7GXPDOrLIEJnAFplMXXiE3t/G',
            'Peptora Admin',
            'free',
            TRUE,
            TRUE,
            TRUE,
            now(),
            now()
        )
        ON CONFLICT (email) DO UPDATE
        SET password_hash = EXCLUDED.password_hash,
            is_admin = TRUE,
            email_verified = TRUE,
            updated_at = now()
    """)


def downgrade() -> None:
    # Do not delete or demote an account on downgrade: it may have been used
    # legitimately after this data migration ran.
    pass
