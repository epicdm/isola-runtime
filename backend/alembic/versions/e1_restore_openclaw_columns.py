"""e1: restore OpenClaw/Edge columns on agents table

Revision ID: e1_restore_openclaw_columns
Revises: c1_agent_template_role_vertical
Create Date: 2026-04-24

OD-49 Phase E.1 — OpenClaw returns as the 'Edge' runtime tier (see
docs/PHASE-E-PRODUCT-SYNTHESIS.md Section 5). Restores the 5 agent
columns dropped in Phase A.2-follow (migration drop_openclaw_columns,
bundled into B.1).

The UI rebrands OpenClaw -> Edge, but the internal columns and the
internal /api/gateway/* route names stay for churn reduction. Only
user-facing strings change.

Row data from the pre-A.2-follow era is not recovered (no agents used
these fields before deletion anyway - A.2 deleted the routes that
would populate them).
"""
from alembic import op


revision = "e1_restore_openclaw_columns"
down_revision = "c1_agent_template_role_vertical"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS "
        "agent_type VARCHAR(20) NOT NULL DEFAULT 'native'"
    )
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(128)")
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS "
        "openclaw_last_seen TIMESTAMP WITH TIME ZONE"
    )
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS container_id VARCHAR(100)")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS container_port INTEGER")


def downgrade():
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS container_port")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS container_id")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS openclaw_last_seen")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS api_key_hash")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS agent_type")
