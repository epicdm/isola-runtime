"""f16: add agents.soul column for Paperclip pull-on-invocation

Revision ID: f16_agents_soul_field
Revises: f15a_owner_ask_in_flight
Create Date: 2026-04-27

ADR-0070 action items #2, #3, #5 / Day 4b BFF->Clawith dispatch.

Adds a nullable Text column on `agents` to hold the SOUL.md content
pulled from Paperclip at dispatch time. The column is the runtime
cache of `paperclip.agents.runtimeConfig.soul`; populated by the new
/api/internal/dispatch endpoint each time it serves a request.

Additive (nullable, no default, no backfill). Idempotent on re-run
(ADD COLUMN IF NOT EXISTS / Postgres 9.6+). Downgrade is destructive
(drops the column) but safe -- nothing depends on it pre-Day-4b.
"""
from alembic import op


revision = "f16_agents_soul_field"
down_revision = "f15a_owner_ask_in_flight"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS soul TEXT")


def downgrade():
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS soul")
