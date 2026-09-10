"""privacy: chat_sessions.visibility — private by default

Revision ID: p1_privacy_session_visibility
Revises: f12_p6_agent_vertical
Create Date: 2026-09-10

Adds a per-session visibility marker so a manager's `manage` right over an
agent stops implying a window into other people's conversations with it.

Backfill is deliberately conservative: EVERY existing row becomes 'private'.
Nothing that was readable by its owner stops being readable by its owner;
what changes is that nobody else inherits it. Group sessions and
agent-to-agent sessions are marked 'shared' because they never carried a
single private principal to begin with.

Reversible: the downgrade drops the column and restores the previous
(unbounded) behaviour.
"""

from alembic import op
import sqlalchemy as sa

revision = "p1_privacy_session_visibility"
down_revision = "f12_p6_agent_vertical"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column(
            "visibility",
            sa.String(length=16),
            nullable=False,
            server_default="private",
        ),
    )
    op.execute(
        "UPDATE chat_sessions SET visibility = 'shared' "
        "WHERE is_group = true OR source_channel = 'agent'"
    )
    op.create_index(
        "ix_chat_sessions_agent_visibility",
        "chat_sessions",
        ["agent_id", "visibility"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_sessions_agent_visibility", table_name="chat_sessions")
    op.drop_column("chat_sessions", "visibility")
