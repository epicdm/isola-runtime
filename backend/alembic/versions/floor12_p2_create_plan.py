"""floor12_p2: create plan table + seed solo/team/suite

Revision ID: floor12_p2_create_plan
Revises: floor12_p1_extend_llm_models
Create Date: 2026-05-22

Floor #12 Phase 1 Step 2: introduce the per-plan billing/entitlement table.
Three plans seeded with Wave-1.5 pricing (EC cents).

included_minutes left NULL for all three plans — voice is not in Wave-1.5
scope (pure WhatsApp text); Wave-2 can UPDATE per-plan. Schema slot reserved.

All three plans default overage_policy="degrade" per spec line 121-123.

Tier access:
  solo, team   : free-router, free-content, standard
  suite        : free-router, free-content, standard, premium, batch
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "floor12_p2_create_plan"
down_revision: Union[str, None] = "floor12_p1_extend_llm_models"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS plan (
            id                  TEXT PRIMARY KEY,
            monthly_price_cents NUMERIC NOT NULL,
            included_tokens     NUMERIC NOT NULL,
            included_messages   INTEGER NOT NULL,
            included_minutes    INTEGER,
            tier_access         TEXT[] NOT NULL,
            overage_policy      TEXT NOT NULL DEFAULT 'degrade',
            created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        INSERT INTO plan (id, monthly_price_cents, included_tokens, included_messages, included_minutes, tier_access, overage_policy)
        VALUES
          ('solo',  14900, 500000,  200,  NULL, ARRAY['free-router','free-content','standard']::TEXT[],                                  'degrade'),
          ('team',  24900, 2000000, 1000, NULL, ARRAY['free-router','free-content','standard']::TEXT[],                                  'degrade'),
          ('suite', 44900, 8000000, 5000, NULL, ARRAY['free-router','free-content','standard','premium','batch']::TEXT[],     'degrade')
        ON CONFLICT (id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS plan")
