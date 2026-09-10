"""Proofs for the enforced usage meter.

Four behaviours are proved, because four are what a spending bound needs:

  1. permitted    -- calls below the cap are granted and counted
  2. refused      -- the call AT the cap is refused, and refusal does not
                     increment (no counter leak that would let a retry storm
                     walk past the boundary)
  3. retry        -- each retry attempt reserves separately, so N retries
                     consume N units and the (cap+1)th attempt is refused
  4. concurrency  -- K tasks racing at the boundary grant exactly the
                     remaining budget, never more

Plus rollover: a new UTC day re-permits, which the production estate has
never done (llm_calls_reset_at last moved 2026-05-06).

These run against SQLite in-memory. The reservation SQL is deliberately
dialect-neutral (bound timestamps, no now(), no FOR UPDATE) so the exact
statement under test is the statement that runs on Postgres.

Honest limitation, stated rather than hidden: SQLite serialises writers, so
the concurrency test proves the check-and-increment is indivisible and
cannot over-grant. It does not exercise Postgres row locking. What makes
that safe on Postgres is that admission and increment are a SINGLE UPDATE
statement -- the row lock is held across the predicate and the write, so a
second transaction re-evaluates the predicate against the committed
counter. A read-then-write guard (what quota_guard.py does today) has no
such property.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# sqlite3 has no native Decimal binding; Postgres NUMERIC does. This is a
# harness detail only -- production still binds Decimal straight through.
sqlite3.register_adapter(Decimal, str)

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.usage_meter import (
    UsageLimitExceeded,
    compute_cost_cents,
    record_llm_call,
    reserve_llm_call,
    reset_daily_counters,
    spend_since,
)

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)

_SCHEMA = [
    """
    CREATE TABLE agents (
        id TEXT PRIMARY KEY,
        tenant_id TEXT,
        name TEXT,
        llm_calls_today INTEGER NOT NULL DEFAULT 0,
        max_llm_calls_per_day INTEGER,
        llm_calls_reset_at TIMESTAMP,
        tokens_used_today INTEGER NOT NULL DEFAULT 0,
        last_daily_reset TIMESTAMP
    )
    """,
    """
    CREATE TABLE llm_call_telemetry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        intent TEXT,
        difficulty TEXT,
        selected_model_id TEXT,
        selected_provider TEXT,
        selected_model TEXT,
        byo_used BOOLEAN NOT NULL DEFAULT 0,
        fallback_used BOOLEAN NOT NULL DEFAULT 0,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cost_cents NUMERIC,
        cost_billed_to TEXT NOT NULL DEFAULT 'epic',
        latency_ms INTEGER,
        success BOOLEAN NOT NULL,
        error_class TEXT,
        classified_by TEXT,
        classifier_confidence NUMERIC,
        created_at TIMESTAMP NOT NULL
    )
    """,
]

AGENT = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        for stmt in _SCHEMA:
            await conn.execute(text(stmt))
    yield maker
    await engine.dispose()


async def _seed(maker, *, cap: int | None, used: int = 0, reset_at: datetime | None = NOW):
    async with maker() as db:
        await db.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, llm_calls_today, "
                "max_llm_calls_per_day, llm_calls_reset_at, tokens_used_today, last_daily_reset) "
                "VALUES (:i, :t, 'Rex', :u, :c, :r, 0, :r)"
            ),
            {"i": str(AGENT), "t": str(TENANT), "u": used, "c": cap, "r": reset_at},
        )
        await db.commit()


async def _counter(maker) -> int:
    async with maker() as db:
        row = (
            await db.execute(
                text("SELECT llm_calls_today FROM agents WHERE id = :i"), {"i": str(AGENT)}
            )
        ).first()
        return int(row[0])


# -- 1. permitted -----------------------------------------------------------

@pytest.mark.asyncio
async def test_permitted_below_limit(sessions):
    await _seed(sessions, cap=5)
    for expected in (1, 2, 3, 4, 5):
        async with sessions() as db:
            res = await reserve_llm_call(AGENT, db, now=NOW)
        assert res.calls_today == expected
        assert res.cap == 5
    assert await _counter(sessions) == 5


@pytest.mark.asyncio
async def test_null_cap_uses_platform_default_not_unlimited(sessions):
    """NULL is the production value on all 444 rows. It must mean 'default',
    not 'unlimited' -- that inversion is the whole defect."""
    await _seed(sessions, cap=None)
    async with sessions() as db:
        res = await reserve_llm_call(AGENT, db, now=NOW, default_cap=2)
    assert res.cap == 2
    async with sessions() as db:
        await reserve_llm_call(AGENT, db, now=NOW, default_cap=2)
    async with sessions() as db:
        with pytest.raises(UsageLimitExceeded):
            await reserve_llm_call(AGENT, db, now=NOW, default_cap=2)


# -- 2. refused at the limit ------------------------------------------------

@pytest.mark.asyncio
async def test_refused_at_limit(sessions):
    await _seed(sessions, cap=3, used=3)
    async with sessions() as db:
        with pytest.raises(UsageLimitExceeded) as exc:
            await reserve_llm_call(AGENT, db, now=NOW)
    assert exc.value.used == 3 and exc.value.cap == 3


@pytest.mark.asyncio
async def test_refusal_does_not_increment(sessions):
    """A refused reservation must not move the counter -- otherwise a client
    that retries on refusal drives the counter arbitrarily high and any
    reporting built on it is wrong."""
    await _seed(sessions, cap=3, used=3)
    for _ in range(10):
        async with sessions() as db:
            with pytest.raises(UsageLimitExceeded):
                await reserve_llm_call(AGENT, db, now=NOW)
    assert await _counter(sessions) == 3


@pytest.mark.asyncio
async def test_unknown_agent_raises_lookup_not_permit(sessions):
    async with sessions() as db:
        with pytest.raises(LookupError):
            await reserve_llm_call(uuid.uuid4(), db, now=NOW)


# -- 3. retry ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_retries_consume_budget_and_are_refused_at_the_cap(sessions):
    """Simulates a caller retrying a failing provider call. Each attempt is a
    real provider call, so each attempt reserves. Cap 2 => attempts 1 and 2
    proceed, attempt 3 is refused before the provider is touched."""
    await _seed(sessions, cap=2)
    attempts, refused = 0, 0
    for _ in range(5):
        try:
            async with sessions() as db:
                await reserve_llm_call(AGENT, db, now=NOW)
            attempts += 1
        except UsageLimitExceeded:
            refused += 1
    assert (attempts, refused) == (2, 3)
    assert await _counter(sessions) == 2


# -- 4. concurrency ---------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_callers_never_over_grant(sessions):
    """20 tasks race for 5 remaining units. Exactly 5 are granted."""
    await _seed(sessions, cap=5)

    async def attempt():
        try:
            async with sessions() as db:
                await reserve_llm_call(AGENT, db, now=NOW)
            return True
        except UsageLimitExceeded:
            return False

    results = await asyncio.gather(*(attempt() for _ in range(20)))
    assert sum(results) == 5
    assert await _counter(sessions) == 5


@pytest.mark.asyncio
async def test_two_agents_at_the_boundary_are_independent(sessions):
    """Two agents at their own boundaries must not consume each other's
    budget -- the reservation is per-row."""
    other = uuid.UUID("33333333-3333-3333-3333-333333333333")
    await _seed(sessions, cap=1)
    async with sessions() as db:
        await db.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, llm_calls_today, "
                "max_llm_calls_per_day, llm_calls_reset_at, tokens_used_today, last_daily_reset) "
                "VALUES (:i, :t, 'Rex2', 0, 1, :r, 0, :r)"
            ),
            {"i": str(other), "t": str(TENANT), "r": NOW},
        )
        await db.commit()

    async def attempt(a):
        try:
            async with sessions() as db:
                await reserve_llm_call(a, db, now=NOW)
            return True
        except UsageLimitExceeded:
            return False

    results = await asyncio.gather(
        attempt(AGENT), attempt(other), attempt(AGENT), attempt(other)
    )
    assert sum(results) == 2


# -- rollover / reset -------------------------------------------------------

@pytest.mark.asyncio
async def test_new_day_rolls_the_counter_over(sessions):
    await _seed(sessions, cap=2, used=2)
    async with sessions() as db:
        with pytest.raises(UsageLimitExceeded):
            await reserve_llm_call(AGENT, db, now=NOW)
    tomorrow = NOW + timedelta(days=1)
    async with sessions() as db:
        res = await reserve_llm_call(AGENT, db, now=tomorrow)
    assert res.calls_today == 1


@pytest.mark.asyncio
async def test_reset_job_is_idempotent(sessions):
    await _seed(sessions, cap=2, used=2, reset_at=NOW - timedelta(days=95))
    async with sessions() as db:
        first = await reset_daily_counters(db, now=NOW)
    assert first["calls_reset"] == 1 and first["tokens_reset"] == 1
    async with sessions() as db:
        second = await reset_daily_counters(db, now=NOW)
    assert second["calls_reset"] == 0 and second["tokens_reset"] == 0
    assert await _counter(sessions) == 0


# -- accounting -------------------------------------------------------------

@pytest.mark.asyncio
async def test_telemetry_write_path_records_cost(sessions):
    await _seed(sessions, cap=10)
    async with sessions() as db:
        cost = await record_llm_call(
            db, tenant_id=TENANT, agent_id=AGENT,
            provider="deepseek", model="deepseek-chat",
            input_tokens=10_000, output_tokens=2_000,
            latency_ms=800, success=True, now=NOW,
        )
    expected = (Decimal(10_000) / 1000 * Decimal("0.027")) + (Decimal(2_000) / 1000 * Decimal("0.110"))
    assert cost == expected
    async with sessions() as db:
        summary = await spend_since(db, NOW - timedelta(hours=1))
    assert summary["calls"] == 1
    assert summary["input_tokens"] == 10_000
    assert summary["unpriced_calls"] == 0
    assert Decimal(str(summary["cost_cents"])) > 0


@pytest.mark.asyncio
async def test_failed_calls_are_also_billed_and_recorded(sessions):
    await _seed(sessions, cap=10)
    async with sessions() as db:
        await record_llm_call(
            db, tenant_id=TENANT, agent_id=AGENT,
            provider="deepseek", model="deepseek-chat",
            input_tokens=500, output_tokens=0,
            success=False, error_class="LLMError", now=NOW,
        )
    async with sessions() as db:
        summary = await spend_since(db, NOW - timedelta(hours=1))
    assert summary["calls"] == 1


def test_unpriced_model_reports_none_not_zero():
    """'We don't know' and 'it was free' must not look the same."""
    assert compute_cost_cents(1000, 1000, "mystery-vendor", "mystery-model") is None
    assert compute_cost_cents(1000, 1000, "ollama", "qwen2.5:1.5b") == Decimal("0")
