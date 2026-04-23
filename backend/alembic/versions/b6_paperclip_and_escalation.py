"""b6: paperclip + escalation columns (agents, chat_sessions)

Revision ID: b6_paperclip_and_escalation
Revises: drop_openclaw_columns
Create Date: 2026-04-23

OD-49 Phase B.6 — port BFF v2's paperclip mirror + escalation keyword
detection into isola-runtime's WhatsApp adapter. Adds:

  agents.owner_phone              VARCHAR(32)
  agents.paperclip_agent_id       VARCHAR(64)
  agents.paperclip_company_id     VARCHAR(64)
  agents.escalation_keywords      JSONB  (list[str], default [])

  chat_sessions.paperclip_issue_id VARCHAR(64)

All columns are nullable so existing rows keep working. The service
layer handles missing values gracefully (Paperclip mirror is a no-op,
escalation falls back to global defaults).
"""
from alembic import op
import sqlalchemy as sa


revision = "b6_paperclip_and_escalation"
down_revision = "drop_openclaw_columns"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS owner_phone VARCHAR(32)"
    )
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS paperclip_agent_id VARCHAR(64)"
    )
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS paperclip_company_id VARCHAR(64)"
    )
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS escalation_keywords JSONB DEFAULT '[]'::jsonb"
    )
    op.execute(
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS paperclip_issue_id VARCHAR(64)"
    )


def downgrade():
    op.execute("ALTER TABLE chat_sessions DROP COLUMN IF EXISTS paperclip_issue_id")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS escalation_keywords")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS paperclip_company_id")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS paperclip_agent_id")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS owner_phone")
