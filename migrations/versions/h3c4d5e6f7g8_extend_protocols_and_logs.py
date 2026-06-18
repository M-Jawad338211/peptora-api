"""extend user_protocols and cycle_logs for protocol-centric architecture

Revision ID: h3c4d5e6f7g8
Revises: g2b3c4d5e6f7
Create Date: 2026-06-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'h3c4d5e6f7g8'
down_revision = 'g2b3c4d5e6f7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Extend user_protocols
    op.add_column('user_protocols', sa.Column('peptide_name', sa.String(255), nullable=True))
    op.add_column('user_protocols', sa.Column('status', sa.String(20), nullable=False, server_default='active'))
    op.add_column('user_protocols', sa.Column('start_date', sa.Date(), nullable=True))
    op.add_column('user_protocols', sa.Column('duration_weeks', sa.Integer(), nullable=True))
    op.add_column('user_protocols', sa.Column('notes', sa.Text(), nullable=True))
    op.add_column('user_protocols', sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        'ck_user_protocols_status',
        'user_protocols',
        "status IN ('active','paused','completed')",
    )

    # Backfill updated_at for existing rows
    op.execute("UPDATE user_protocols SET updated_at = created_at WHERE updated_at IS NULL")

    # Add protocol_id FK to cycle_logs
    op.add_column(
        'cycle_logs',
        sa.Column(
            'protocol_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('user_protocols.id', ondelete='CASCADE'),
            nullable=True,
        ),
    )
    op.create_index('ix_cycle_logs_protocol_id', 'cycle_logs', ['protocol_id'])


def downgrade() -> None:
    op.drop_index('ix_cycle_logs_protocol_id', table_name='cycle_logs')
    op.drop_column('cycle_logs', 'protocol_id')

    op.drop_constraint('ck_user_protocols_status', 'user_protocols', type_='check')
    op.drop_column('user_protocols', 'updated_at')
    op.drop_column('user_protocols', 'notes')
    op.drop_column('user_protocols', 'duration_weeks')
    op.drop_column('user_protocols', 'start_date')
    op.drop_column('user_protocols', 'status')
    op.drop_column('user_protocols', 'peptide_name')
