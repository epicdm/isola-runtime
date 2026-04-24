"""e6a: add tenants.runtime_mode (hosted | edge)

Revision ID: e6a_tenant_runtime_mode
Revises: e1_restore_openclaw_columns
Create Date: 2026-04-24

OD-49 Phase E.6a — tenant-level Runtime Mode setting. Default 'hosted'
(platform-managed). 'edge' flips new agents to agent_type='openclaw'
by default so tenants that bring their own runtime get Edge agents
without having to set agent_type per-call.

Synthesis doc decision 3: tenant-level for v1, per-agent later if asked.
"""
from alembic import op


revision = "e6a_tenant_runtime_mode"
down_revision = "e1_restore_openclaw_columns"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "
        "runtime_mode VARCHAR(20) NOT NULL DEFAULT 'hosted'"
    )


def downgrade():
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS runtime_mode")
