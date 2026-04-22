"""Add wechat to channel_type_enum.

Revision ID: add_wechat_channel_support
Revises: add_primary_chat_sessions_unread
Create Date: 2026-04-16
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "add_wechat_channel_support"
down_revision: Union[str, None] = "add_primary_chat_sessions_unread"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE channel_type_enum ADD VALUE IF NOT EXISTS 'wechat'")
    op.execute("ALTER TYPE channel_type_enum ADD VALUE IF NOT EXISTS 'whatsapp'")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_providers_tenant_type
        ON identity_providers (tenant_id, provider_type)
        WHERE tenant_id IS NOT NULL
        """
    )

def downgrade() -> None:
    # PostgreSQL enums cannot drop values safely in place.
    op.execute("DROP INDEX IF EXISTS uq_identity_providers_tenant_type")
    pass
