"""f12_p6: add vertical column to agents table

Revision ID: f12_p6_agent_vertical
Revises: f12_p5_telemetry
Create Date: 2026-05-22

Floor #12 Phase 2 Slice S0: agents.vertical enables intent classification
routing. NULL = no classification (pass-through to primary_model_id).
Non-NULL values must match intent_routing.vertical values in use:
  'pharmacy', 'agriculture' for Phase 2.
Partial index accelerates the routing query (only 2 non-NULL rows in Phase 2).
"""

from alembic import op
import sqlalchemy as sa

revision = 'f12_p6_agent_vertical'
down_revision = 'f12_p5_telemetry'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents',
        sa.Column('vertical', sa.Text(), nullable=True))
    op.create_index(
        'ix_agents_vertical',
        'agents',
        ['vertical'],
        postgresql_where=sa.text('vertical IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('ix_agents_vertical', table_name='agents')
    op.drop_column('agents', 'vertical')
