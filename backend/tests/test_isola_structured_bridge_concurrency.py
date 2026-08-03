"""Concurrency, idempotency and restart-replay proofs, real Postgres
(`dec-clawith-structured-claim-isolation-dedicated-table-2026-08-02`).

These prove the database's own UNIQUE constraint —
`uq_isola_structured_bridge_requests_tenant_correlation` on
`(tenant_id, correlation_id)` — is the true arbiter of "exactly one claim
wins", both from concurrent asyncio tasks inside one process and from
genuinely separate OS processes (a real multi-process race, not merely
cooperative-scheduling luck).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import pytest

from app.api import isola_bridge_structured as structured_api
from app.config import get_settings
from app.database import engine as _sqlalchemy_engine

TABLE = "isola_structured_bridge_requests"


def _asyncpg_dsn() -> str:
    return get_settings().DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


@pytest.fixture(scope="module", autouse=True)
def _require_local_database():
    dsn = get_settings().DATABASE_URL
    allowed_hosts = ("localhost", "127.0.0.1", "@postgres:", "//postgres:")
    if not any(host in dsn for host in allowed_hosts):
        pytest.skip(f"refusing to run against non-local DATABASE_URL host: {dsn}")


@pytest.fixture(autouse=True)
async def _clean_state():
    # See test_isola_structured_bridge_claim_isolation.py for why this is
    # necessary under pytest-asyncio's per-test event loops.
    await _sqlalchemy_engine.dispose()
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(f"DELETE FROM {TABLE}")
    finally:
        await conn.close()
    yield
    await _sqlalchemy_engine.dispose()
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(f"DELETE FROM {TABLE}")
    finally:
        await conn.close()


def _fake_body(*, designated_agent_id, conversation_id="conv-1", contact_ref="contact:x", schema_version="1.0.0"):
    return SimpleNamespace(
        designated_agent_id=designated_agent_id,
        conversation_id=conversation_id,
        contact_ref=contact_ref,
        schema_version=schema_version,
    )


# ── Same tenant, same correlation_id, same policy: exactly one winner ──────


@pytest.mark.asyncio
async def test_concurrent_asyncio_claims_for_the_same_key_produce_exactly_one_winner():
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    correlation_id = "race-same-policy"
    digest = structured_api._tool_policy_digest(["crm.lead.create"])

    results = await asyncio.gather(
        *[
            structured_api._claim(
                tenant_id=tenant_id,
                correlation_id=correlation_id,
                body=_fake_body(designated_agent_id=agent_id),
                tool_policy_digest=digest,
            )
            for _ in range(20)
        ]
    )

    winners = [r for r in results if r[0] is True]
    losers = [r for r in results if r[0] is False]
    assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"
    assert len(losers) == 19

    winning_id = winners[0][1].id
    # Every loser must see the SAME row the winner created — no second row,
    # no second reasoning run's worth of state anywhere.
    for _won, row in losers:
        assert row is not None
        assert row.id == winning_id
        assert row.correlation_id == correlation_id


@pytest.mark.asyncio
async def test_concurrent_claims_repeated_across_rounds_never_produce_more_than_one_winner():
    """Repeats the race across several independent correlation_ids to catch
    flaky timing-dependent races that a single trial could miss."""
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    digest = structured_api._tool_policy_digest([])

    for round_index in range(8):
        correlation_id = f"race-round-{round_index}"
        results = await asyncio.gather(
            *[
                structured_api._claim(
                    tenant_id=tenant_id,
                    correlation_id=correlation_id,
                    body=_fake_body(designated_agent_id=agent_id),
                    tool_policy_digest=digest,
                )
                for _ in range(12)
            ]
        )
        winners = [r for r in results if r[0] is True]
        assert len(winners) == 1, f"round {round_index}: expected 1 winner, got {len(winners)}"


# ── True multi-process race: separate OS processes, DB is the sole arbiter ─


_SUBPROCESS_CLAIM_SCRIPT = """
import asyncio, asyncpg, sys, uuid

async def main():
    dsn, tenant_id, correlation_id, agent_id, digest = sys.argv[1:6]
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            '''
            INSERT INTO isola_structured_bridge_requests
            (id, tenant_id, correlation_id, agent_id, tool_policy_digest, schema_version, state, accepted_at)
            VALUES ($1, $2, $3, $4, $5, '1.0.0', 'accepted', now())
            ON CONFLICT (tenant_id, correlation_id) DO NOTHING
            RETURNING id
            ''',
            uuid.uuid4(), uuid.UUID(tenant_id), correlation_id, uuid.UUID(agent_id), digest,
        )
        print("WON" if row is not None else "LOST")
    finally:
        await conn.close()

asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_true_multi_process_race_has_exactly_one_winner():
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    correlation_id = "multiprocess-race"
    digest = structured_api._tool_policy_digest([])
    dsn = _asyncpg_dsn()

    n_processes = 8
    procs = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _SUBPROCESS_CLAIM_SCRIPT,
            dsn,
            str(tenant_id),
            correlation_id,
            str(agent_id),
            digest,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(n_processes)
    ]
    outputs = await asyncio.gather(*[proc.communicate() for proc in procs])

    results = []
    for i, (stdout, stderr) in enumerate(outputs):
        assert procs[i].returncode == 0, f"subprocess {i} failed: {stderr.decode()}"
        results.append(stdout.decode().strip())

    assert results.count("WON") == 1, f"expected exactly one WON across {n_processes} processes, got {results}"
    assert results.count("LOST") == n_processes - 1

    conn = await asyncpg.connect(dsn)
    try:
        count = await conn.fetchval(
            f"SELECT count(*) FROM {TABLE} WHERE tenant_id = $1 AND correlation_id = $2",
            tenant_id,
            correlation_id,
        )
    finally:
        await conn.close()
    assert count == 1


# ── Loser polls before winner populates session/run ─────────────────────────


@pytest.mark.asyncio
async def test_loser_wait_returns_promptly_when_winner_has_not_populated_yet(monkeypatch):
    monkeypatch.setattr(structured_api, "_CLAIM_POPULATE_TIMEOUT_S", 0.6)
    monkeypatch.setattr(structured_api, "_CLAIM_POPULATE_INTERVAL_S", 0.1)

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    correlation_id = "loser-not-yet-populated"
    digest = structured_api._tool_policy_digest([])

    won, row = await structured_api._claim(
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        body=_fake_body(designated_agent_id=agent_id),
        tool_policy_digest=digest,
    )
    assert won is True
    assert row.session_id is None and row.run_id is None

    start = asyncio.get_event_loop().time()
    populated = await structured_api._wait_for_claim_population(row.id)
    elapsed = asyncio.get_event_loop().time() - start

    # Gives up within the (shortened) grace window rather than hanging.
    assert elapsed < 2.0
    assert populated.session_id is None
    assert populated.run_id is None
    assert populated.state == "accepted"


@pytest.mark.asyncio
async def test_loser_wait_picks_up_the_winners_populated_session_and_run():
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    correlation_id = "loser-sees-populated-winner"
    digest = structured_api._tool_policy_digest([])

    won, row = await structured_api._claim(
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        body=_fake_body(designated_agent_id=agent_id),
        tool_policy_digest=digest,
    )
    assert won is True

    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(
            f"""
            UPDATE {TABLE} SET session_id = $1, run_id = $2, state = 'running'
             WHERE id = $3
            """,
            session_id,
            run_id,
            row.id,
        )
    finally:
        await conn.close()

    populated = await structured_api._wait_for_claim_population(row.id)
    assert populated.session_id == session_id
    assert populated.run_id == run_id
    assert populated.state == "running"


# ── Restart-safe replay: a fresh claim attempt sees the existing terminal row ─


@pytest.mark.asyncio
async def test_replay_after_simulated_restart_sees_the_existing_terminal_row_not_a_new_one():
    """Simulates a process restart: the claim row for a completed turn
    already exists from "before" (a prior process); a fresh `_claim` call —
    representing a brand-new process/session with no in-memory state — must
    find that SAME row (won=False) rather than creating a second one."""
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    correlation_id = "restart-replay"
    digest = structured_api._tool_policy_digest(["crm.lead.create"])
    terminal_message_id = uuid.uuid4()

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        pre_existing_id = await conn.fetchval(
            f"""
            INSERT INTO {TABLE}
            (id, tenant_id, correlation_id, agent_id, tool_policy_digest, schema_version,
             state, accepted_at, completed_at, terminal_message_id)
            VALUES ($1, $2, $3, $4, $5, '1.0.0', 'completed', now(), now(), $6)
            RETURNING id
            """,
            uuid.uuid4(),
            tenant_id,
            correlation_id,
            agent_id,
            digest,
            terminal_message_id,
        )
    finally:
        await conn.close()

    won, row = await structured_api._claim(
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        body=_fake_body(designated_agent_id=agent_id),
        tool_policy_digest=digest,
    )

    assert won is False, "a restart must never re-win a claim that already exists"
    assert row.id == pre_existing_id
    assert row.state == "completed"
    assert str(row.terminal_message_id) == str(terminal_message_id)

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        count = await conn.fetchval(
            f"SELECT count(*) FROM {TABLE} WHERE tenant_id = $1 AND correlation_id = $2",
            tenant_id,
            correlation_id,
        )
    finally:
        await conn.close()
    assert count == 1, "restart replay must never create a second row / second reasoning run"
