"""floor12_p1: extend llm_models with tier/cost/capability/latency/health columns

Revision ID: floor12_p1_extend_llm_models
Revises: h1_s7_retire_lifecycle
Create Date: 2026-05-22

Floor #12 Phase 1 Step 1: extend the existing tenant-scoped llm_models table
with pool-tier metadata and observability columns required for LCR routing.

All columns are nullable; existing rows are backfilled by the Step 6 base_url
correction + Step 8 pool seeding. health_status defaults to NULL on add and
will be populated by the circuit-breaker telemetry path (Phase 2+).

Additive migration. Reversible via downgrade.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "floor12_p1_extend_llm_models"
down_revision: Union[str, None] = "h1_s7_retire_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE llm_models
          ADD COLUMN IF NOT EXISTS tier TEXT,
          ADD COLUMN IF NOT EXISTS cost_per_1k_input_cents NUMERIC,
          ADD COLUMN IF NOT EXISTS cost_per_1k_output_cents NUMERIC,
          ADD COLUMN IF NOT EXISTS capability_tags JSONB,
          ADD COLUMN IF NOT EXISTS latency_p50_ms INTEGER,
          ADD COLUMN IF NOT EXISTS latency_p95_ms INTEGER,
          ADD COLUMN IF NOT EXISTS health_status TEXT,
          ADD COLUMN IF NOT EXISTS health_last_check TIMESTAMP WITH TIME ZONE
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_models_tier ON llm_models(tier)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_models_health_status ON llm_models(health_status)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_llm_models_health_status")
    op.execute("DROP INDEX IF EXISTS ix_llm_models_tier")
    op.execute("""
        ALTER TABLE llm_models
          DROP COLUMN IF EXISTS health_last_check,
          DROP COLUMN IF EXISTS health_status,
          DROP COLUMN IF EXISTS latency_p95_ms,
          DROP COLUMN IF EXISTS latency_p50_ms,
          DROP COLUMN IF EXISTS capability_tags,
          DROP COLUMN IF EXISTS cost_per_1k_output_cents,
          DROP COLUMN IF EXISTS cost_per_1k_input_cents,
          DROP COLUMN IF EXISTS tier
    """)
