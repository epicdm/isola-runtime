"""f12_p3: create tenant_subscription + backfill existing tenants

Revision ID: f12_p3_tenant_sub
Revises: floor12_p2_create_plan
Create Date: 2026-05-22

Floor #12 Phase 1 Step 3: per-tenant billing + bucket-state row.

Short revision ID per Alembic 32-char limit (carry-forward from failed
attempt with verbose floor12_p3_create_tenant_subscription = 37 chars).
Future Floor #12 migrations use f12_pN_* style; p1/p2 retained as-landed.

Schema decisions:
- tenant_id FK -> tenants(id) ON DELETE CASCADE
- plan_id FK -> plan(id) ON DELETE RESTRICT (deprecate plans, do not delete)
- bucket_state defaults to {} (empty JSONB); Phase 2 logic populates
- credit_balance_cents defaults to 0
- created_at/updated_at audit pair per Step 2 pattern
- bucket_renews_at = now() + 1 month on backfill (anniversary-based per
  Eric ratification option a)

ON CONFLICT idempotent.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f12_p3_tenant_sub"
down_revision: Union[str, None] = "floor12_p2_create_plan"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS tenant_subscription (
            tenant_id            UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            plan_id              TEXT NOT NULL REFERENCES plan(id) ON DELETE RESTRICT,
            bucket_state         JSONB NOT NULL DEFAULT '{}'::jsonb,
            credit_balance_cents NUMERIC NOT NULL DEFAULT 0,
            bucket_renews_at     TIMESTAMP WITH TIME ZONE,
            byo_keys             JSONB,
            created_at           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_tenant_subscription_plan_id ON tenant_subscription(plan_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_tenant_subscription_bucket_renews_at ON tenant_subscription(bucket_renews_at)")
    op.execute("""
        INSERT INTO tenant_subscription (tenant_id, plan_id, bucket_state, credit_balance_cents, bucket_renews_at, byo_keys)
        SELECT id, 'solo', '{}'::jsonb, 0, now() + interval '1 month', NULL
        FROM tenants
        ON CONFLICT (tenant_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_subscription")
