"""f1.5a: owner_ask_in_flight table for live owner-ask loop

Revision ID: f15a_owner_ask_in_flight
Revises: f1_agent_sync_odoo
Create Date: 2026-04-24

OD-49 Phase F.1.5a / Self-learning Tier 1.5a (#109).

One row per in-flight question Rex is awaiting an owner answer for.
Created when the LLM emits `[ask_owner: ...]`, resolved when the owner
WAs back or 24h passes.

All-IF-NOT-EXISTS so re-runs on a partially-applied install are safe.
"""
from alembic import op


revision = "f15a_owner_ask_in_flight"
down_revision = "f1_agent_sync_odoo"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS owner_ask_in_flight (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            agent_id UUID NOT NULL REFERENCES agents(id),
            customer_phone VARCHAR(32) NOT NULL,
            customer_name VARCHAR(200),
            customer_conversation_id UUID,
            question TEXT NOT NULL,
            owner_phone VARCHAR(32) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'awaiting',
            asked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            answered_at TIMESTAMPTZ,
            owner_reply TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS owner_ask_in_flight_agent_id_idx "
        "ON owner_ask_in_flight (agent_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS owner_ask_in_flight_owner_phone_idx "
        "ON owner_ask_in_flight (owner_phone)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS owner_ask_in_flight_status_idx "
        "ON owner_ask_in_flight (status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS owner_ask_in_flight_agent_status_asked_at_idx "
        "ON owner_ask_in_flight (agent_id, status, asked_at)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS owner_ask_in_flight_agent_status_asked_at_idx")
    op.execute("DROP INDEX IF EXISTS owner_ask_in_flight_status_idx")
    op.execute("DROP INDEX IF EXISTS owner_ask_in_flight_owner_phone_idx")
    op.execute("DROP INDEX IF EXISTS owner_ask_in_flight_agent_id_idx")
    op.execute("DROP TABLE IF EXISTS owner_ask_in_flight")
