"""Cross-route claim-isolation proofs, real Postgres
(`dec-clawith-structured-claim-isolation-dedicated-table-2026-08-02`,
closing `defect-clawith-structured-bridge-v2-request-table-collision-
2026-08-02`).

These exercise `isola_bridge_structured._claim` — the exact function that
performs the durable idempotency claim — directly against a real disposable
database, after seeding `isola_bridge_requests` with rows shaped exactly
like the two proven live attacks
(`evidence-clawith-cross-route-claim-isolation-design-audit-2026-08-02`):

1. Preclaim/DoS: a v2 caller submits
   `stable_request_id="structured:<correlation_id>"` with empty
   `metadata_labels`.
2. Forged-digest join: a v2 caller submits the SAME `stable_request_id`
   shape with the publicly-derivable empty-tool-set digest
   (`4f53cda18c2baa0c`) as `metadata_labels`, AND already has
   `session_id`/`run_id` populated (mirroring v2's real insert-time shape),
   which is exactly the state that let the pre-migration structured route
   silently join and read back the attacker's run.

Both must now be provably inert: the structured claim SQL only ever reads
and writes `isola_structured_bridge_requests`, which a v2 caller has no
request field capable of naming.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest

from app.api import isola_bridge_structured as structured_api
from app.config import get_settings
from app.database import engine as _sqlalchemy_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
TABLE = "isola_structured_bridge_requests"
LEGACY_TABLE = "isola_bridge_requests"
EMPTY_TOOLSET_DIGEST = "4f53cda18c2baa0c"


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
    # pytest-asyncio (function-scoped loops, the default under
    # asyncio_mode="auto") gives each test function its own event loop.
    # app.database.engine is a module-level singleton whose asyncpg
    # connections are bound to whichever loop created them; reusing a
    # pooled connection from a previous test's now-closed loop raises
    # asyncpg "another operation is in progress" / cross-loop errors.
    # Disposing the pool at the start of every test forces fresh
    # connections bound to THIS test's loop.
    await _sqlalchemy_engine.dispose()

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(f"DELETE FROM {TABLE}")
        await conn.execute(f"DELETE FROM {LEGACY_TABLE} WHERE stable_request_id LIKE 'structured:%'")
        await conn.execute(f"DELETE FROM {LEGACY_TABLE} WHERE stable_request_id LIKE 'iso-test-%'")
    finally:
        await conn.close()
    yield
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(f"DELETE FROM {TABLE}")
        await conn.execute(f"DELETE FROM {LEGACY_TABLE} WHERE stable_request_id LIKE 'structured:%'")
        await conn.execute(f"DELETE FROM {LEGACY_TABLE} WHERE stable_request_id LIKE 'iso-test-%'")
    finally:
        await conn.close()


async def _insert_legacy_row(
    conn: asyncpg.Connection,
    *,
    stable_request_id: str,
    tenant_id,
    agent_id,
    correlation_id: str | None = None,
    metadata_labels: str = "{}",
    session_id=None,
    run_id=None,
    state: str = "accepted",
):
    row_id = uuid.uuid4()
    await conn.execute(
        f"""
        INSERT INTO {LEGACY_TABLE}
        (id, tenant_id, stable_request_id, correlation_id, agent_id, state,
         accepted_at, idempotency_key, metadata_labels, session_id, run_id)
        VALUES ($1, $2, $3, $4, $5, $6, now(), $7, CAST($8 AS JSONB), $9, $10)
        """,
        row_id,
        tenant_id,
        stable_request_id,
        correlation_id,
        agent_id,
        state,
        f"{tenant_id}:{stable_request_id}",
        metadata_labels,
        session_id,
        run_id,
    )
    return row_id


def _fake_body(*, designated_agent_id, conversation_id="conv-1", contact_ref="contact:x", schema_version="1.0.0"):
    return SimpleNamespace(
        designated_agent_id=designated_agent_id,
        conversation_id=conversation_id,
        contact_ref=contact_ref,
        schema_version=schema_version,
    )


# ── Attack 1: cross-route preclaim / permanent-denial DoS ───────────────────


@pytest.mark.asyncio
async def test_v2_preclaim_with_empty_metadata_does_not_block_the_structured_claim():
    """The real-world default: a v2 caller pre-claims
    `structured:<correlation_id>` with metadata_labels={} (no caller ever
    supplies labels in production). The structured route's OWN claim must
    succeed anyway — no 409, no collision, no dependency on the v2 row at
    all."""
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    correlation_id = "test-id"

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        legacy_row_id = await _insert_legacy_row(
            conn,
            stable_request_id=f"structured:{correlation_id}",
            tenant_id=tenant_id,
            agent_id=agent_id,
            correlation_id=correlation_id,
            metadata_labels="{}",
        )
    finally:
        await conn.close()

    digest = structured_api._tool_policy_digest([])
    won, row = await structured_api._claim(
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        body=_fake_body(designated_agent_id=agent_id),
        tool_policy_digest=digest,
    )

    assert won is True, "the structured claim must win — the v2 row cannot occupy its key"
    assert row is not None
    assert row.correlation_id == correlation_id
    assert row.tool_policy_digest == digest
    # No join: this is a BRAND NEW row, never touched by the legacy insert.
    assert row.session_id is None
    assert row.run_id is None

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        legacy_row = await conn.fetchrow(
            f"SELECT id, stable_request_id, state FROM {LEGACY_TABLE} WHERE id = $1", legacy_row_id
        )
        structured_rows = await conn.fetch(
            f"SELECT id, correlation_id FROM {TABLE} WHERE tenant_id = $1", tenant_id
        )
    finally:
        await conn.close()

    # Legacy row remains, completely unaffected.
    assert legacy_row is not None
    assert legacy_row["stable_request_id"] == f"structured:{correlation_id}"
    assert legacy_row["state"] == "accepted"
    # Exactly one independent structured row was created.
    assert len(structured_rows) == 1
    assert structured_rows[0]["correlation_id"] == correlation_id


# ── Attack 2: forged-digest join ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forged_empty_toolset_digest_on_a_v2_row_is_inert():
    """The publicly-derivable empty-tool-set digest, even paired with a v2
    row that ALREADY has session_id/run_id populated (mirroring the exact
    shape that let the shared-table design silently join), must not let the
    structured claim see, join or inherit that v2 row's run/session."""
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    correlation_id = "test-id-forged"
    attacker_session_id = uuid.uuid4()
    attacker_run_id = uuid.uuid4()

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await _insert_legacy_row(
            conn,
            stable_request_id=f"structured:{correlation_id}",
            tenant_id=tenant_id,
            agent_id=agent_id,
            correlation_id=correlation_id,
            metadata_labels=f'{{"tool_policy_digest": "{EMPTY_TOOLSET_DIGEST}"}}',
            session_id=attacker_session_id,
            run_id=attacker_run_id,
            state="running",
        )
    finally:
        await conn.close()

    won, row = await structured_api._claim(
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        body=_fake_body(designated_agent_id=agent_id),
        tool_policy_digest=EMPTY_TOOLSET_DIGEST,
    )

    assert won is True
    assert row is not None
    # The structured row is FRESH — it must never inherit the attacker's
    # session_id/run_id from the legacy row, even though the digest matches
    # the fixed public constant exactly.
    assert row.session_id is None
    assert row.run_id is None
    assert row.session_id != attacker_session_id
    assert row.run_id != attacker_run_id


@pytest.mark.asyncio
async def test_forged_digest_variant_with_a_named_tool_is_also_inert():
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    correlation_id = "test-id-forged-2"
    named_digest = structured_api._tool_policy_digest(["escalate_to_human"])

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await _insert_legacy_row(
            conn,
            stable_request_id=f"structured:{correlation_id}",
            tenant_id=tenant_id,
            agent_id=agent_id,
            correlation_id=correlation_id,
            metadata_labels=f'{{"tool_policy_digest": "{named_digest}"}}',
            session_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            state="running",
        )
    finally:
        await conn.close()

    won, row = await structured_api._claim(
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        body=_fake_body(designated_agent_id=agent_id),
        tool_policy_digest=named_digest,
    )

    assert won is True
    assert row.session_id is None
    assert row.run_id is None


# ── Identical correlation values across both relations ──────────────────────


@pytest.mark.asyncio
async def test_identical_correlation_value_in_both_relations_does_not_collide():
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    shared_value = "iso-test-shared-corr-value"

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        # A legacy row whose stable_request_id happens to equal the SAME
        # string the structured route will use as correlation_id (no
        # "structured:" prefix this time) — proves the isolation holds even
        # without the historical namespace convention.
        await _insert_legacy_row(
            conn,
            stable_request_id=shared_value,
            tenant_id=tenant_id,
            agent_id=agent_id,
            correlation_id=shared_value,
        )
    finally:
        await conn.close()

    digest = structured_api._tool_policy_digest([])
    won, row = await structured_api._claim(
        tenant_id=tenant_id,
        correlation_id=shared_value,
        body=_fake_body(designated_agent_id=agent_id),
        tool_policy_digest=digest,
    )
    assert won is True
    assert row.correlation_id == shared_value


# ── Scale: 100+ v2 preclaim rows, every structured correlation independently claimable ──


@pytest.mark.asyncio
async def test_bulk_v2_preclaim_rows_leave_every_structured_correlation_independently_claimable():
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    n = 120

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        for i in range(n):
            await _insert_legacy_row(
                conn,
                stable_request_id=f"structured:bulk-{i:04d}",
                tenant_id=tenant_id,
                agent_id=agent_id,
                correlation_id=f"bulk-{i:04d}",
                metadata_labels="{}",
            )
        legacy_count = await conn.fetchval(
            f"SELECT count(*) FROM {LEGACY_TABLE} WHERE stable_request_id LIKE 'structured:bulk-%'"
        )
    finally:
        await conn.close()
    assert legacy_count == n

    digest = structured_api._tool_policy_digest([])
    for i in range(n):
        won, row = await structured_api._claim(
            tenant_id=tenant_id,
            correlation_id=f"bulk-{i:04d}",
            body=_fake_body(designated_agent_id=agent_id),
            tool_policy_digest=digest,
        )
        assert won is True, f"claim {i} was blocked by a v2 preclaim row"
        assert row.session_id is None
        assert row.run_id is None

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        structured_count = await conn.fetchval(
            f"SELECT count(*) FROM {TABLE} WHERE tenant_id = $1", tenant_id
        )
        legacy_count_after = await conn.fetchval(
            f"SELECT count(*) FROM {LEGACY_TABLE} WHERE stable_request_id LIKE 'structured:bulk-%'"
        )
    finally:
        await conn.close()

    assert structured_count == n
    assert legacy_count_after == n  # untouched


# ── Static source-reference isolation ────────────────────────────────────────


def test_structured_route_source_never_references_the_legacy_table():
    source = (BACKEND_DIR / "app" / "api" / "isola_bridge_structured.py").read_text(encoding="utf-8")
    assert "isola_bridge_requests" not in source


def test_v2_module_source_never_references_the_structured_table():
    source = (BACKEND_DIR / "app" / "api" / "isola_bridge_v2.py").read_text(encoding="utf-8")
    assert "isola_structured_bridge_requests" not in source


def test_legacy_bridge_module_source_never_references_the_structured_table():
    source = (BACKEND_DIR / "app" / "api" / "isola_bridge.py").read_text(encoding="utf-8")
    assert "isola_structured_bridge_requests" not in source


def test_structured_route_sql_literals_name_only_the_dedicated_table():
    for sql in (
        structured_api._CLAIM_SQL,
        structured_api._SELECT_BY_CORRELATION_SQL,
        structured_api._SELECT_BY_ID_SQL,
        structured_api._UPDATE_ENQUEUED_SQL,
        structured_api._UPDATE_TERMINAL_SQL,
    ):
        text = str(sql)
        assert "isola_structured_bridge_requests" in text
        assert "isola_bridge_requests" not in text
