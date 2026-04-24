"""f1: agent-sync external_id + tone/welcome + tenant Odoo fields

Revision ID: f1_agent_sync_odoo
Revises: e6a_tenant_runtime_mode
Create Date: 2026-04-24

OD-49 Phase F.1.a-1 per docs/PHASE-F1-DESIGN-BRIEF.md. Adds the schema
needed to bridge apps/isola agents into runtime (external_id + tone +
welcome_message) and to link each tenant to its Odoo company record
(odoo_company_id + odoo_api_key encrypted).

All columns nullable for backfill safety. External_id indexed for
idempotent ensure-agent lookups. Migration is idempotent via IF NOT
EXISTS on the ALTERs so re-runs on existing installs are safe.
"""
from alembic import op


revision = "f1_agent_sync_odoo"
down_revision = "e6a_tenant_runtime_mode"
branch_labels = None
depends_on = None


def upgrade():
    # agents — apps/isola external id + tenant-configurable defaults
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS external_id VARCHAR(200)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS agents_external_id_idx ON agents (external_id)"
    )
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS tone INTEGER DEFAULT 1")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS welcome_message TEXT")

    # tenants — Odoo company link + encrypted API key
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS odoo_company_id INTEGER")
    op.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS odoo_api_key VARCHAR(500)"
    )


def downgrade():
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS odoo_api_key")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS odoo_company_id")
    op.execute("DROP INDEX IF EXISTS agents_external_id_idx")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS welcome_message")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS tone")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS external_id")
