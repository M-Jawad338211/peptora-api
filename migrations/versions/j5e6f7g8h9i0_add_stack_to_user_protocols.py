"""add stack_id/stack_name to user_protocols

Lets a saved protocol point at a peptide_stacks blend/pairing instead of a
single peptide. Additive only — no data loss.

Revision ID: j5e6f7g8h9i0
Revises: i4d5e6f7g8h9
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = 'j5e6f7g8h9i0'
down_revision = 'i4d5e6f7g8h9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('user_protocols', sa.Column('stack_id', sa.String(), nullable=True))
    op.add_column('user_protocols', sa.Column('stack_name', sa.String(255), nullable=True))
    op.create_foreign_key(
        'fk_user_protocols_stack_id', 'user_protocols', 'peptide_stacks',
        ['stack_id'], ['id'], ondelete='SET NULL',
    )
    op.create_check_constraint(
        'ck_user_protocols_single_target', 'user_protocols',
        'peptide_id IS NULL OR stack_id IS NULL',
    )


def downgrade() -> None:
    op.drop_constraint('ck_user_protocols_single_target', 'user_protocols', type_='check')
    op.drop_constraint('fk_user_protocols_stack_id', 'user_protocols', type_='foreignkey')
    op.drop_column('user_protocols', 'stack_name')
    op.drop_column('user_protocols', 'stack_id')
