"""add expo_push_token to users

Revision ID: d4e5f6a7b8c9
Revises: b9f1c4a7d2e8
Create Date: 2026-05-21
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4e5f6a7b8c9'
down_revision = 'b9f1c4a7d2e8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('expo_push_token', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'expo_push_token')
