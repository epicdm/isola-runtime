"""Add Feishu group delivery targets to schedules and triggers.

Revision ID: f065_feishu_group_target
Revises: f064_tool_call_tenants
Create Date: 2026-08-18 16:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f065_feishu_group_target"
down_revision: str | Sequence[str] | None = "f064_tool_call_tenants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    # initial_schema builds fresh databases from the current models, so the
    # column may already exist when this revision runs.
    return column_name in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    for table_name in ("agent_schedules", "agent_triggers"):
        if not _column_exists(table_name, "delivery_target_id"):
            op.add_column(
                table_name,
                sa.Column("delivery_target_id", postgresql.UUID(as_uuid=True), nullable=True),
            )


def downgrade() -> None:
    op.drop_column("agent_triggers", "delivery_target_id")
    op.drop_column("agent_schedules", "delivery_target_id")
