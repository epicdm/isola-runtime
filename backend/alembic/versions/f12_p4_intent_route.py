"""f12_p4: create intent_routing table

Revision ID: f12_p4_intent_route
Revises: f12_p3_tenant_sub
Create Date: 2026-05-22

Floor #12 Phase 1 Step 4: per-(agent|vertical) intent->tier routing rules.

Structure only — actual row seeding (pharmacy + agriculture vertical defaults)
is Step 9.

Schema decisions:
- id UUID PK with gen_random_uuid() default (matches house convention with
  agents.id, llm_models.id, etc.)
- agent_id FK -> agents(id) ON DELETE CASCADE (when this is an agent override)
- vertical TEXT NULL (free text — "agriculture", "pharmacy", etc.; no FK)
- XOR CHECK: exactly one of (agent_id, vertical) must be set. A row with both
  NULL would be ambiguous; a row with both set is contradictory.
- difficulty CHECK in (low, medium, high, critical) per spec line 134
- preferred_tier / fallback_tier are TEXT (reference llm_models.tier values
  but no FK — tier is a column not a table)
- capability_required TEXT[] DEFAULT {} (empty array = no capability constraint)
- max_cost_cents INTEGER NULL (NULL = no per-call cap)
- Partial UNIQUE indexes prevent duplicate (agent_id, intent) and
  (vertical, intent) rows
- created_at/updated_at audit pair per Step 2 pattern

Additive migration. Reversible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f12_p4_intent_route"
down_revision: Union[str, None] = "f12_p3_tenant_sub"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS intent_routing (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            agent_id            UUID REFERENCES agents(id) ON DELETE CASCADE,
            vertical            TEXT,
            intent_pattern      TEXT NOT NULL,
            difficulty          TEXT NOT NULL,
            preferred_tier      TEXT NOT NULL,
            fallback_tier       TEXT,
            capability_required TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
            max_cost_cents      INTEGER,
            notes               TEXT,
            created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT intent_routing_scope_xor CHECK (
                (agent_id IS NOT NULL AND vertical IS NULL)
                OR
                (agent_id IS NULL AND vertical IS NOT NULL)
            ),
            CONSTRAINT intent_routing_difficulty_check CHECK (
                difficulty IN ('low','medium','high','critical')
            )
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_intent_routing_agent_intent ON intent_routing(agent_id, intent_pattern) WHERE agent_id IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_intent_routing_vertical_intent ON intent_routing(vertical, intent_pattern) WHERE vertical IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_intent_routing_intent_pattern ON intent_routing(intent_pattern)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS intent_routing")
