"""add whatsapp to channel_type_enum (Phase B.1)

Revision ID: add_whatsapp_channel_type
Revises: narrow_phase_a_enums
Create Date: 2026-04-23

OD-49 Phase B.1 scaffolding. After Phase A narrowed channel_type_enum to
(slack, discord, microsoft_teams), this migration widens it to include
whatsapp so the new WhatsApp Cloud API adapter can persist ChannelConfig
rows.

Postgres permits ADD VALUE on enums in a transactional migration as of PG
12+, which the isolaruntime stack uses (postgres:15-alpine).
"""
from alembic import op


revision = "add_whatsapp_channel_type"
down_revision = "narrow_phase_a_enums"
branch_labels = None
depends_on = None


def upgrade():
    # Postgres 12+: ADD VALUE IF NOT EXISTS inside a transaction works.
    op.execute("ALTER TYPE channel_type_enum ADD VALUE IF NOT EXISTS 'whatsapp'")


def downgrade():
    # Enum VALUE removal in Postgres requires a full enum rebuild
    # (see narrow_phase_a_enums.py for the dance). Migrate any whatsapp rows
    # to slack before dropping the value.
    op.execute(
        "DELETE FROM channel_configs WHERE channel_type::text = 'whatsapp'"
    )
    op.execute("ALTER TABLE channel_configs ALTER COLUMN channel_type DROP DEFAULT")
    op.execute(
        "ALTER TABLE channel_configs "
        "ALTER COLUMN channel_type TYPE VARCHAR(32) USING channel_type::text"
    )
    op.execute("DROP TYPE channel_type_enum")
    op.execute(
        "CREATE TYPE channel_type_enum AS ENUM ('slack','discord','microsoft_teams')"
    )
    op.execute(
        "ALTER TABLE channel_configs "
        "ALTER COLUMN channel_type TYPE channel_type_enum "
        "USING channel_type::channel_type_enum"
    )
    op.execute(
        "ALTER TABLE channel_configs ALTER COLUMN channel_type SET DEFAULT 'slack'"
    )
    op.execute("ALTER TABLE channel_configs ALTER COLUMN channel_type SET NOT NULL")
