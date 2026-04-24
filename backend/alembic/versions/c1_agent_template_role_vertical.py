"""c1: add role + vertical to agent_templates

Revision ID: c1_agent_template_role_vertical
Revises: b6_paperclip_and_escalation
Create Date: 2026-04-23

OD-49 Phase C.1 — Isola ships role × vertical AgentTemplates (Rex/Mara/
Joey × restaurant/hotel/clinic/retail/service). This migration adds two
nullable columns so the existing generic upstream templates keep
working while we seed the Isola matrix on top.
"""
from alembic import op


revision = "c1_agent_template_role_vertical"
down_revision = "b6_paperclip_and_escalation"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE agent_templates ADD COLUMN IF NOT EXISTS role VARCHAR(32)"
    )
    op.execute(
        "ALTER TABLE agent_templates ADD COLUMN IF NOT EXISTS vertical VARCHAR(32)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_templates_role_vertical "
        "ON agent_templates (role, vertical)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_agent_templates_role_vertical")
    op.execute("ALTER TABLE agent_templates DROP COLUMN IF EXISTS vertical")
    op.execute("ALTER TABLE agent_templates DROP COLUMN IF EXISTS role")
