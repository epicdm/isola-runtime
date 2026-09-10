"""i1: paperclip_runs -- idempotency + state ledger for the INT-001 receiver

Revision ID: i1_paperclip_run_ledger
Revises: f12_p6_agent_vertical
Create Date: 2026-09-10

Additive only. Creates one new table; alters nothing existing, so it cannot
break a running deployment and down() is a clean drop.

down_revision is f12_p6_agent_vertical, which is the single head on main at
the time of writing. This deliberately does NOT chain onto the PR #43/#44
branches: if either of those merges first, rebase this revision's
down_revision onto the new head before deploying. Deploying a revision whose
down_revision names a revision absent from the deployed branch raises KeyError
in alembic, and entrypoint.sh continues startup anyway -- the F5 failure mode.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "i1_paperclip_run_ledger"
down_revision = "f12_p6_agent_vertical"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paperclip_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("run_id", sa.String(length=200), nullable=False),
        sa.Column("paperclip_agent_id", sa.String(length=200), nullable=False),
        sa.Column("paperclip_company_id", sa.String(length=200), nullable=False),
        sa.Column("issue_id", sa.String(length=200), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="accepted"),
        sa.Column("principal_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("principal_label", sa.String(length=120), nullable=True),
        sa.Column("provider_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("denied_tools", sa.Text(), nullable=True),
        sa.Column(
            "comment_posted", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("comment_id", sa.String(length=200), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The idempotency primitive. Concurrent duplicate deliveries of one runId
    # race here; exactly one wins.
    op.create_index(
        "ix_paperclip_runs_run_id", "paperclip_runs", ["run_id"], unique=True
    )
    op.create_index(
        "ix_paperclip_runs_paperclip_agent_id", "paperclip_runs", ["paperclip_agent_id"]
    )
    op.create_index(
        "ix_paperclip_runs_paperclip_company_id", "paperclip_runs", ["paperclip_company_id"]
    )
    op.create_index("ix_paperclip_runs_state", "paperclip_runs", ["state"])


def downgrade() -> None:
    op.drop_index("ix_paperclip_runs_state", table_name="paperclip_runs")
    op.drop_index("ix_paperclip_runs_paperclip_company_id", table_name="paperclip_runs")
    op.drop_index("ix_paperclip_runs_paperclip_agent_id", table_name="paperclip_runs")
    op.drop_index("ix_paperclip_runs_run_id", table_name="paperclip_runs")
    op.drop_table("paperclip_runs")
