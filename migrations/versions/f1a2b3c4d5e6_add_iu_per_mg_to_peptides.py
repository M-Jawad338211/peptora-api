"""add iu_per_mg to peptides

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa

revision = 'f1a2b3c4d5e6'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('peptides', sa.Column('iu_per_mg', sa.Numeric(10, 4), nullable=True))


def downgrade() -> None:
    op.drop_column('peptides', 'iu_per_mg')
