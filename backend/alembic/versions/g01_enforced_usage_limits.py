"""g01: make the per-agent daily LLM call ceiling a real constraint

Revision ID: g01_enforced_usage_limits
Revises: f12_p6_agent_vertical
Create Date: 2026-09-10

PAIRED MIGRATION for app/services/usage_meter.py.

Measured state this migration addresses (isolaruntime, 2026-09-10, read-only):

  * agents.max_llm_calls_per_day is nullable. NULL is read by the new
    reservation as "platform default", but leaving the column nullable keeps
    the old ambiguity alive on every INSERT path that omits it. All 444
    production rows already hold 100, so the backfill below is a no-op there
    and the NOT NULL is free.
  * agents.llm_calls_reset_at last moved 2026-05-06 and
    agents.last_daily_reset last moved 2026-06-07. The reset job in
    usage_meter.reset_daily_counters() scans on those two columns; without
    indexes it is a seq scan of the agents table every tick.

DATA IMPACT
  UPDATE on agents rows where max_llm_calls_per_day IS NULL. Zero rows in
  the measured production database (444/444 already = 100). Non-zero only on
  environments seeded after the column was added without a default.

  No rows are deleted. No column is dropped. tokens_used_today and
  llm_calls_today are NOT zeroed here -- that is the reset job's business,
  deliberately kept out of a schema migration so a deploy never silently
  hands every agent a fresh budget.

DOWNGRADE
  Fully reversible: drops the NOT NULL, the server default, the check
  constraint and the two indexes. Backfilled values are left in place (they
  are valid data, and restoring NULLs would restore the defect).
"""
from typing import Sequence, Union

from alembic import op


revision: str = "g01_enforced_usage_limits"
down_revision: Union[str, None] = "f12_p6_agent_vertical"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_CAP = 100


def upgrade() -> None:
    # 1. No agent may carry an absent ceiling.
    op.execute(
        f"UPDATE agents SET max_llm_calls_per_day = {DEFAULT_CAP} "
        f"WHERE max_llm_calls_per_day IS NULL"
    )
    op.execute(
        f"ALTER TABLE agents ALTER COLUMN max_llm_calls_per_day SET DEFAULT {DEFAULT_CAP}"
    )
    op.execute("ALTER TABLE agents ALTER COLUMN max_llm_calls_per_day SET NOT NULL")

    # 2. A non-negative ceiling only. 0 is legal and means "this agent makes
    #    no provider calls" -- the safest possible containment value.
    op.execute(
        "ALTER TABLE agents ADD CONSTRAINT agents_max_llm_calls_per_day_check "
        "CHECK (max_llm_calls_per_day >= 0) NOT VALID"
    )
    op.execute("ALTER TABLE agents VALIDATE CONSTRAINT agents_max_llm_calls_per_day_check")

    # 3. Make the daily reset scan cheap.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agents_llm_calls_reset_at "
        "ON agents (llm_calls_reset_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agents_last_daily_reset "
        "ON agents (last_daily_reset)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agents_last_daily_reset")
    op.execute("DROP INDEX IF EXISTS ix_agents_llm_calls_reset_at")
    op.execute(
        "ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_max_llm_calls_per_day_check"
    )
    op.execute("ALTER TABLE agents ALTER COLUMN max_llm_calls_per_day DROP NOT NULL")
    op.execute("ALTER TABLE agents ALTER COLUMN max_llm_calls_per_day DROP DEFAULT")
