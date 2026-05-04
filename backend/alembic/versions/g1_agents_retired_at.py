"""g1: add agents.retired_at + retired_by for soft-delete lifecycle

Revision ID: g1_agents_retired_at
Revises: f16_agents_soul_field
Create Date: 2026-05-04

L4 S4 ratification 15: agent retire = soft-delete via retired_at
timestamp + retired_by user FK, kept separate from the runtime status
enum (which carries operational state: creating/running/idle/stopped/error).

NULL retired_at = active.
Non-NULL retired_at = retired (lifecycle end; audit trail preserved).

Hard-delete (planned for L4 S6 alongside Clawith DELETE endpoints) becomes
a simple cleanup pattern: WHERE retired_at IS NOT NULL.

Partial index on (tenant_id) WHERE retired_at IS NULL accelerates the
common "active agents under tenant X" query path used by the operator
console list endpoint.

Additive (both columns nullable, no defaults, no backfill). Idempotent
via IF NOT EXISTS guards. Downgrade drops the columns + index --
destructive but safe (no schema dependencies pre-S4).
"""
from alembic import op


revision = "g1_agents_retired_at"
down_revision = "f16_agents_soul_field"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS retired_by UUID")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_active "
        "ON agents (tenant_id) WHERE retired_at IS NULL"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_agents_active")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS retired_by")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS retired_at")
