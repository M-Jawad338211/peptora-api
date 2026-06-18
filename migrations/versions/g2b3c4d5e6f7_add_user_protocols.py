"""add user_protocols table

Revision ID: g2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'g2b3c4d5e6f7'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_protocols',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('peptide_id', sa.String(), sa.ForeignKey('peptides.id', ondelete='SET NULL'), nullable=True),
        sa.Column('label', sa.Text(), nullable=True),
        sa.Column('vial_mg', sa.Numeric(10, 3), nullable=False),
        sa.Column('reconstituted', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('bac_water_ml', sa.Numeric(10, 3), nullable=True),
        sa.Column('target_dose_mcg', sa.Numeric(12, 4), nullable=False),
        sa.Column('unit', sa.String(20), nullable=False, server_default='mcg'),
        sa.Column('syringe_type', sa.String(20), nullable=False, server_default='U-100'),
        sa.Column('frequency', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('vial_mg > 0', name='ck_user_protocols_vial_mg_positive'),
        sa.CheckConstraint('target_dose_mcg > 0', name='ck_user_protocols_dose_positive'),
    )
    op.create_index('ix_user_protocols_user_id', 'user_protocols', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_user_protocols_user_id', table_name='user_protocols')
    op.drop_table('user_protocols')
