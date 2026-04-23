"""drop dead OpenClaw columns from agents table

Revision ID: drop_openclaw_columns
Revises: add_whatsapp_channel_type
Create Date: 2026-04-23

OD-49 Phase A.2-follow DB cleanup — bundled into B.1 because it was
blocking seed_default_agents on boot (agent_type had NOT NULL without
a DEFAULT once the Python model stopped mapping it, so SQLAlchemy's
implicit-omit INSERT raised IntegrityError).

Fields removed here match the Python model fields deleted in A.2-follow
(commit 1f85d54). entrypoint.sh's column-patch block is updated in the
same commit so these columns don't get re-added on every restart.
"""
from alembic import op


revision = "drop_openclaw_columns"
down_revision = "add_whatsapp_channel_type"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS agent_type")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS api_key_hash")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS openclaw_last_seen")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS container_id")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS container_port")


def downgrade():
    # Restore the pre-A.2-follow column shape. Row data is not recovered.
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS container_port INTEGER")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS container_id VARCHAR(100)")
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS "
        "openclaw_last_seen TIMESTAMP WITH TIME ZONE"
    )
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(128)")
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS "
        "agent_type VARCHAR(20) NOT NULL DEFAULT 'native'"
    )
