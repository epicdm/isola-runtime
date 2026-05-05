"""h1: S7 retire lifecycle — tenants/users retired_at + audit_logs FK SET NULL

Revision ID: h1_s7_retire_lifecycle
Revises: g1_agents_retired_at
Create Date: 2026-05-05

L4 S7 ratifications R39 + R41:

R39: Soft-delete primary path for tenants + users (mirrors S4 agents pattern).
     retired_at TIMESTAMPTZ NULL = active; non-NULL = retired (lifecycle end,
     audit trail preserved). Hard-delete deferred to W2-HW.

R39 (audit_logs FK migration pulled forward): the FK
    audit_logs.agent_id REFERENCES agents.id ON DELETE NO ACTION (RESTRICT)
hits us during smoke fixture revert (Phase 3) and would block any future
hard-delete on agents (W2-HW). Same applies to audit_logs.user_id when users
get retire endpoint in S7. Migrating BOTH FKs to ON DELETE SET NULL preserves
audit row content (action, details, timestamp, ip_address) while detaching
the FK on parent retirement, matching the audit-history-preservation contract
established at Phase 3 revert.

Other 20 RESTRICT FKs documented in W2-HW with the FK-inventory query
(scripts/L4-S6-phase3 era).

Additive migration (NULL columns + index + FK swap). Idempotent via IF NOT
EXISTS guards on columns/indexes; FK swap uses DROP CONSTRAINT IF EXISTS +
re-add. Downgrade reverses cleanly.
"""
from alembic import op


revision = "h1_s7_retire_lifecycle"
down_revision = "g1_agents_retired_at"
branch_labels = None
depends_on = None


def upgrade():
    # ─── tenants soft-delete columns + active index ─────────────────
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS retired_by UUID")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tenants_active "
        "ON tenants (id) WHERE retired_at IS NULL"
    )

    # ─── users soft-delete columns + active index ───────────────────
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS retired_by UUID")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_active "
        "ON users (tenant_id) WHERE retired_at IS NULL"
    )

    # ─── audit_logs FK migration: NO ACTION → SET NULL ──────────────
    # Postgres requires DROP + ADD (no ALTER CONSTRAINT for delete_rule).
    # IF EXISTS guards make this idempotent across re-runs.
    # FK names taken from default Postgres convention: <table>_<col>_fkey.
    op.execute("ALTER TABLE audit_logs DROP CONSTRAINT IF EXISTS audit_logs_agent_id_fkey")
    op.execute(
        "ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_agent_id_fkey "
        "FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE SET NULL"
    )
    op.execute("ALTER TABLE audit_logs DROP CONSTRAINT IF EXISTS audit_logs_user_id_fkey")
    op.execute(
        "ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_user_id_fkey "
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL"
    )


def downgrade():
    # Reverse FK rules (back to NO ACTION = the implicit Postgres default).
    op.execute("ALTER TABLE audit_logs DROP CONSTRAINT IF EXISTS audit_logs_user_id_fkey")
    op.execute(
        "ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_user_id_fkey "
        "FOREIGN KEY (user_id) REFERENCES users(id)"
    )
    op.execute("ALTER TABLE audit_logs DROP CONSTRAINT IF EXISTS audit_logs_agent_id_fkey")
    op.execute(
        "ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_agent_id_fkey "
        "FOREIGN KEY (agent_id) REFERENCES agents(id)"
    )

    # Drop indexes + columns (destructive but recoverable from this revision).
    op.execute("DROP INDEX IF EXISTS idx_users_active")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS retired_by")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS retired_at")

    op.execute("DROP INDEX IF EXISTS idx_tenants_active")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS retired_by")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS retired_at")
