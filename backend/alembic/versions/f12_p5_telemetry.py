"""f12_p5: create llm_call_telemetry table (write-once analytics)

Revision ID: f12_p5_telemetry
Revises: f12_p4a_relax_xor
Create Date: 2026-05-22

Floor #12 Phase 1 Step 5: per-call observability/billing record.

FK behavior is asymmetric on purpose:
- tenant_id, agent_id  -> CASCADE  (hard-delete of a tenant/agent removes
  their telemetry too -- privacy + storage hygiene)
- selected_model_id    -> SET NULL (historical reporting must still work
  for periods when a now-removed pool model was in use)

No updated_at: telemetry rows are write-once, never updated. created_at is
the only timestamp.

PM rec (c) applied: total_tokens dropped as a stored column. Compute on query
as (input_tokens + output_tokens). Removes a redundant column + a CHECK
constraint surface area for those two values to disagree.

Partitioning deferred to post-Wave-2 (CARRY-FORWARD). Single-table is fine
for our scale; revisit when row count or query latency justifies monthly
time-series partitions on created_at.

Indexes target analytics-query patterns (time-series + grouping), distinct
from intent_routing's per-call lookup pattern.

Empty initial state -- no backfill (forward-write-only).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f12_p5_telemetry"
down_revision: Union[str, None] = "f12_p4a_relax_xor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS llm_call_telemetry (
            id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            agent_id                UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            intent                  TEXT,
            difficulty              TEXT,
            selected_model_id       UUID REFERENCES llm_models(id) ON DELETE SET NULL,
            selected_provider       TEXT,
            selected_model          TEXT,
            byo_used                BOOLEAN NOT NULL DEFAULT FALSE,
            fallback_used           BOOLEAN NOT NULL DEFAULT FALSE,
            input_tokens            INTEGER,
            output_tokens           INTEGER,
            cost_cents              NUMERIC,
            cost_billed_to          TEXT NOT NULL DEFAULT 'epic',
            latency_ms              INTEGER,
            success                 BOOLEAN NOT NULL,
            error_class             TEXT,
            classified_by           TEXT,
            classifier_confidence   NUMERIC,
            created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT llm_call_telemetry_cost_billed_to_check CHECK (
                cost_billed_to IN ('epic','customer')
            ),
            CONSTRAINT llm_call_telemetry_classified_by_check CHECK (
                classified_by IS NULL OR classified_by IN ('rules','llm')
            ),
            CONSTRAINT llm_call_telemetry_difficulty_check CHECK (
                difficulty IS NULL OR difficulty IN ('low','medium','high','critical')
            )
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_call_telemetry_created_at ON llm_call_telemetry(created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_call_telemetry_tenant_id_created_at ON llm_call_telemetry(tenant_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_call_telemetry_agent_id_created_at ON llm_call_telemetry(agent_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_call_telemetry_errors ON llm_call_telemetry(created_at) WHERE success = FALSE")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS llm_call_telemetry")
