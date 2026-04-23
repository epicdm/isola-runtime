"""narrow channel_type_enum (Phase A.1b-follow)

Revision ID: narrow_phase_a_enums
Revises: increase_api_key_length
Create Date: 2026-04-23

OD-49 Phase A.1b narrowed the Python-side channel_type enum after
stripping Chinese/enterprise channels. On a fresh isolaruntime DB the
actual starting state of the Postgres enum is:

    channel_type_enum = (slack, discord, microsoft_teams, atlassian, agentbay)

The 'atlassian' and 'agentbay' values survive because older upstream
migrations (add_agentbay_enum_value, etc.) ADD VALUE them onto the
enum that SQLAlchemy create_all built from the current narrow model.
The 'feishu', 'wecom', 'dingtalk' values never existed in this fresh
DB — create_all built the narrow model, and no migration tries to
ADD VALUE those.

im_provider_enum is already at the target set (slack, discord,
microsoft_teams, whatsapp, web_only) for the same reason — no
migration survives upstream that added the Chinese im_providers.

This migration only has work to do on channel_type_enum:
    (slack, discord, microsoft_teams, atlassian, agentbay)
    -> (slack, discord, microsoft_teams)

Rows in channel_configs with channel_type = 'atlassian' or 'agentbay'
are DELETED (no business case to keep their configuration after the
code that consumed them is gone in A.1b).
"""
from alembic import op


revision = "narrow_phase_a_enums"
down_revision = "increase_api_key_length"
branch_labels = None
depends_on = None


def upgrade():
    # Drop rows that can't be mapped into the new enum.
    op.execute(
        "DELETE FROM channel_configs "
        "WHERE channel_type::text IN ('atlassian','agentbay')"
    )

    # Pivot column to VARCHAR so we can drop+recreate the enum type.
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


def downgrade():
    # Restore the post-create_all pre-narrowing shape
    # (slack, discord, microsoft_teams, atlassian, agentbay).
    # Rows that upgrade() deleted cannot be recovered.
    op.execute("ALTER TABLE channel_configs ALTER COLUMN channel_type DROP DEFAULT")
    op.execute(
        "ALTER TABLE channel_configs "
        "ALTER COLUMN channel_type TYPE VARCHAR(32) USING channel_type::text"
    )
    op.execute("DROP TYPE channel_type_enum")
    op.execute(
        "CREATE TYPE channel_type_enum AS ENUM "
        "('slack','discord','microsoft_teams','atlassian','agentbay')"
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
