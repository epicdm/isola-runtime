"""ISOLA GLUE (not upstream): bridge-owned request mapping for the async Lane-2 bridge.

Revision ID: add_isola_bridge_requests
Revises: add_experience_revision_drafts
Create Date: 2026-07-31

Additive only. Down migration drops the table; nothing else is touched.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "add_isola_bridge_requests"
down_revision: str | None = "add_experience_revision_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "isola_bridge_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stable_request_id", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("external_conversation_id", sa.Text(), nullable=True),
        sa.Column("contact_ref", sa.Text(), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clawith_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("initiating_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("terminal_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata_labels",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint(
            "state IN ('accepted','running','completed','failed','cancelled','expired','rejected')",
            name="ck_isola_bridge_requests_state",
        ),
        sa.CheckConstraint(
            "length(stable_request_id) >= 8", name="ck_isola_bridge_requests_stable_id_len"
        ),
    )
    # One submitted request can never create two runs.
    op.create_unique_constraint(
        "uq_isola_bridge_requests_tenant_stable",
        "isola_bridge_requests",
        ["tenant_id", "stable_request_id"],
    )
    op.create_index(
        "ix_isola_bridge_requests_open",
        "isola_bridge_requests",
        ["state", "expires_at"],
    )
    op.create_index(
        "ix_isola_bridge_requests_session", "isola_bridge_requests", ["session_id"]
    )
    op.create_index(
        "ix_isola_bridge_requests_correlation", "isola_bridge_requests", ["correlation_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_isola_bridge_requests_correlation", table_name="isola_bridge_requests")
    op.drop_index("ix_isola_bridge_requests_session", table_name="isola_bridge_requests")
    op.drop_index("ix_isola_bridge_requests_open", table_name="isola_bridge_requests")
    op.drop_constraint(
        "uq_isola_bridge_requests_tenant_stable", "isola_bridge_requests", type_="unique"
    )
    op.drop_table("isola_bridge_requests")
