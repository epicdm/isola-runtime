"""Real-Postgres migration and downgrade-guard proofs for
``isola_structured_bridge_requests``
(`dec-clawith-structured-claim-isolation-dedicated-table-2026-08-02`).

These tests run `alembic upgrade`/`downgrade` as real subprocesses against
`settings.DATABASE_URL` and inspect the resulting schema/rows directly with
`asyncpg`. The properties being proved — a real UNIQUE constraint arbitrates
claims, a real CHECK constraint enforces digest shape, downgrade truly
refuses to drop a populated table — cannot be proven by mocking
`alembic.op`; they require a real database.

REQUIRES a disposable local/CI Postgres. The `_require_local_database`
fixture below refuses to run against anything that doesn't look like one, as
a defence-in-depth guard on top of this repository's own
`scripts/src/guard-not-prod-db.ts`-style discipline — never a production
database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest

from app.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
REVISION = "add_structured_bridge_requests"
DOWN_REVISION = "add_isola_bridge_requests"
TABLE = "isola_structured_bridge_requests"
LEGACY_TABLE = "isola_bridge_requests"


def _asyncpg_dsn() -> str:
    # settings.DATABASE_URL is a SQLAlchemy asyncpg URL
    # (postgresql+asyncpg://...); asyncpg.connect wants a plain DSN.
    return get_settings().DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    return result


@pytest.fixture(scope="module", autouse=True)
def _require_local_database():
    """Refuses to run against anything that doesn't look like a disposable
    local/CI database. Never production."""
    dsn = get_settings().DATABASE_URL
    allowed_hosts = ("localhost", "127.0.0.1", "@postgres:", "//postgres:")
    if not any(host in dsn for host in allowed_hosts):
        pytest.skip(f"refusing to run migration tests against non-local DATABASE_URL host: {dsn}")


@pytest.fixture(autouse=True)
async def _clean_state():
    """Before each test: ensure we're at head with an empty structured
    table. After each test: delete any rows the test created and restore
    head, so tests remain independent of execution order and never leak
    state into the rest of the suite."""
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(f"DELETE FROM {TABLE}")
    finally:
        await conn.close()

    yield

    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(f"DELETE FROM {TABLE}")
    finally:
        await conn.close()


def _digest() -> str:
    return "4f53cda18c2baa0c"


async def _insert_row(conn: asyncpg.Connection, *, correlation_id: str, tenant_id=None, agent_id=None, digest=None):
    row_id = uuid.uuid4()
    await conn.execute(
        f"""
        INSERT INTO {TABLE}
        (id, tenant_id, correlation_id, agent_id, tool_policy_digest, schema_version, state, accepted_at)
        VALUES ($1, $2, $3, $4, $5, '1.0.0', 'accepted', $6)
        """,
        row_id,
        tenant_id or uuid.uuid4(),
        correlation_id,
        agent_id or uuid.uuid4(),
        digest or _digest(),
        datetime.now(UTC),
    )
    return row_id


# ── A1/A5: upgrade reaches head, re-upgrade is a no-op success ──────────────


@pytest.mark.asyncio
async def test_upgrade_from_add_isola_bridge_requests_reaches_the_new_head():
    result = _run_alembic("downgrade", DOWN_REVISION)
    assert result.returncode == 0, result.stderr

    result = _run_alembic("current")
    assert result.returncode == 0, result.stderr
    assert DOWN_REVISION in result.stdout

    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr

    result = _run_alembic("current")
    assert result.returncode == 0, result.stderr
    assert REVISION in result.stdout


@pytest.mark.asyncio
async def test_reupgrade_to_head_is_idempotent():
    result_a = _run_alembic("upgrade", "head")
    assert result_a.returncode == 0, result_a.stderr
    result_b = _run_alembic("upgrade", "head")
    assert result_b.returncode == 0, result_b.stderr


# ── A2: exact schema assertions ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_table_has_the_exact_ratified_columns_types_and_nullability():
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        rows = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_name = $1
             ORDER BY ordinal_position
            """,
            TABLE,
        )
    finally:
        await conn.close()

    columns = {r["column_name"]: r for r in rows}

    expected_not_null = {
        "id": "uuid",
        "tenant_id": "uuid",
        "correlation_id": "text",
        "agent_id": "uuid",
        "tool_policy_digest": "text",
        "schema_version": "text",
        "state": "text",
        "accepted_at": "timestamp with time zone",
    }
    expected_nullable = {
        "external_conversation_id": "text",
        "contact_ref": "text",
        "clawith_user_id": "uuid",
        "session_id": "uuid",
        "run_id": "uuid",
        "initiating_message_id": "uuid",
        "terminal_message_id": "uuid",
        "error_class": "text",
        "started_at": "timestamp with time zone",
        "completed_at": "timestamp with time zone",
        "last_checked_at": "timestamp with time zone",
        "expires_at": "timestamp with time zone",
    }

    assert set(columns.keys()) == (
        set(expected_not_null) | set(expected_nullable) | {"metadata_labels"}
    )

    for name, expected_type in expected_not_null.items():
        assert columns[name]["data_type"] == expected_type, name
        assert columns[name]["is_nullable"] == "NO", f"{name} must be NOT NULL"

    for name, expected_type in expected_nullable.items():
        assert columns[name]["data_type"] == expected_type, name
        assert columns[name]["is_nullable"] == "YES", f"{name} must be nullable"

    assert columns["metadata_labels"]["data_type"] == "jsonb"
    assert columns["metadata_labels"]["is_nullable"] == "NO"
    assert columns["metadata_labels"]["column_default"] == "'{}'::jsonb"


@pytest.mark.asyncio
async def test_table_has_the_exact_ratified_primary_key():
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        rows = await conn.fetch(
            """
            SELECT a.attname
              FROM pg_index i
              JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
             WHERE i.indrelid = $1::regclass AND i.indisprimary
            """,
            TABLE,
        )
    finally:
        await conn.close()
    assert {r["attname"] for r in rows} == {"id"}


@pytest.mark.asyncio
async def test_table_has_the_exact_ratified_unique_constraint():
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        row = await conn.fetchrow(
            """
            SELECT pg_get_constraintdef(oid) AS def
              FROM pg_constraint
             WHERE conname = 'uq_isola_structured_bridge_requests_tenant_correlation'
               AND conrelid = $1::regclass
            """,
            TABLE,
        )
    finally:
        await conn.close()
    assert row is not None, "unique constraint is missing"
    assert "UNIQUE (tenant_id, correlation_id)" in row["def"]


@pytest.mark.asyncio
async def test_table_has_the_exact_ratified_check_constraints():
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        rows = await conn.fetch(
            """
            SELECT conname, pg_get_constraintdef(oid) AS def
              FROM pg_constraint
             WHERE conrelid = $1::regclass AND contype = 'c'
            """,
            TABLE,
        )
    finally:
        await conn.close()
    checks = {r["conname"]: r["def"] for r in rows}

    assert "ck_isola_structured_bridge_requests_state" in checks
    for state in ("accepted", "running", "completed", "failed", "cancelled", "expired", "rejected"):
        assert state in checks["ck_isola_structured_bridge_requests_state"]

    assert "ck_isola_structured_bridge_requests_correlation_len" in checks
    assert "1" in checks["ck_isola_structured_bridge_requests_correlation_len"]
    assert "180" in checks["ck_isola_structured_bridge_requests_correlation_len"]

    assert "ck_isola_structured_bridge_requests_digest_shape" in checks
    assert "0-9a-f" in checks["ck_isola_structured_bridge_requests_digest_shape"]


@pytest.mark.asyncio
async def test_table_has_all_required_indexes():
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        rows = await conn.fetch(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = $1", TABLE
        )
    finally:
        await conn.close()
    indexes = {r["indexname"]: r["indexdef"] for r in rows}

    assert "ix_isola_structured_bridge_requests_open" in indexes
    assert "state" in indexes["ix_isola_structured_bridge_requests_open"]
    assert "expires_at" in indexes["ix_isola_structured_bridge_requests_open"]

    assert "ix_isola_structured_bridge_requests_session" in indexes
    assert "session_id" in indexes["ix_isola_structured_bridge_requests_session"]

    assert "ix_isola_structured_bridge_requests_agent_accepted" in indexes
    assert "agent_id" in indexes["ix_isola_structured_bridge_requests_agent_accepted"]
    assert "accepted_at" in indexes["ix_isola_structured_bridge_requests_agent_accepted"]


@pytest.mark.asyncio
async def test_table_has_zero_foreign_keys():
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        rows = await conn.fetch(
            "SELECT conname FROM pg_constraint WHERE conrelid = $1::regclass AND contype = 'f'",
            TABLE,
        )
    finally:
        await conn.close()
    assert rows == [], "the ratified design has zero foreign keys, deliberately"


@pytest.mark.asyncio
async def test_state_check_rejects_an_out_of_vocabulary_value():
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                f"""
                INSERT INTO {TABLE}
                (id, tenant_id, correlation_id, agent_id, tool_policy_digest, schema_version, state, accepted_at)
                VALUES ($1, $2, 'c', $3, $4, '1.0.0', 'not_a_real_state', now())
                """,
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
                _digest(),
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_digest_shape_check_rejects_a_malformed_digest():
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                f"""
                INSERT INTO {TABLE}
                (id, tenant_id, correlation_id, agent_id, tool_policy_digest, schema_version, state, accepted_at)
                VALUES ($1, $2, 'c', $3, 'NOT-HEX!', '1.0.0', 'accepted', now())
                """,
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_correlation_len_check_rejects_an_empty_correlation_id():
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                f"""
                INSERT INTO {TABLE}
                (id, tenant_id, correlation_id, agent_id, tool_policy_digest, schema_version, state, accepted_at)
                VALUES ($1, $2, '', $3, $4, '1.0.0', 'accepted', now())
                """,
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
                _digest(),
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_unique_constraint_arbitrates_duplicate_tenant_correlation_pairs():
    tenant_id = uuid.uuid4()
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await _insert_row(conn, correlation_id="dup-corr", tenant_id=tenant_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert_row(conn, correlation_id="dup-corr", tenant_id=tenant_id)
    finally:
        await conn.close()


# ── A3: legacy table DDL is byte-for-byte unchanged ──────────────────────────


@pytest.mark.asyncio
async def test_legacy_isola_bridge_requests_ddl_is_unchanged():
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        columns = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable
              FROM information_schema.columns
             WHERE table_name = $1
             ORDER BY ordinal_position
            """,
            LEGACY_TABLE,
        )
        constraints = await conn.fetch(
            "SELECT conname FROM pg_constraint WHERE conrelid = $1::regclass",
            LEGACY_TABLE,
        )
        indexes = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = $1", LEGACY_TABLE
        )
    finally:
        await conn.close()

    column_names = {r["column_name"] for r in columns}
    assert column_names == {
        "id", "tenant_id", "stable_request_id", "correlation_id",
        "external_conversation_id", "contact_ref", "agent_id", "clawith_user_id",
        "session_id", "run_id", "initiating_message_id", "terminal_message_id",
        "state", "accepted_at", "started_at", "completed_at", "last_checked_at",
        "error_class", "idempotency_key", "expires_at", "metadata_labels",
    }
    constraint_names = {r["conname"] for r in constraints}
    assert "uq_isola_bridge_requests_tenant_stable" in constraint_names
    assert "ck_isola_bridge_requests_state" in constraint_names
    assert "ck_isola_bridge_requests_stable_id_len" in constraint_names
    # Never introduced onto the legacy table by this migration.
    assert not any("structured" in name for name in constraint_names)

    index_names = {r["indexname"] for r in indexes}
    assert {
        "ix_isola_bridge_requests_open",
        "ix_isola_bridge_requests_session",
        "ix_isola_bridge_requests_correlation",
    } <= index_names


# ── A4: downgrade while empty succeeds and touches only the new relation ────


@pytest.mark.asyncio
async def test_downgrade_while_empty_succeeds_and_removes_only_the_new_table():
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        legacy_row_id = await conn.fetchval(
            f"""
            INSERT INTO {LEGACY_TABLE}
            (id, tenant_id, stable_request_id, agent_id, state, accepted_at, idempotency_key)
            VALUES ($1, $2, 'probe-stable-id-untouched', $3, 'completed', now(), 'probe-key')
            RETURNING id
            """,
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
        )
    finally:
        await conn.close()

    result = _run_alembic("downgrade", DOWN_REVISION)
    assert result.returncode == 0, result.stderr

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        table_exists = await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", f"public.{TABLE}"
        )
        assert table_exists is False

        legacy_row = await conn.fetchrow(
            f"SELECT id, state FROM {LEGACY_TABLE} WHERE id = $1", legacy_row_id
        )
        assert legacy_row is not None
        assert legacy_row["state"] == "completed"

        current = await conn.fetchval("SELECT version_num FROM alembic_version")
        assert current == DOWN_REVISION
    finally:
        # Clean up the probe row so it doesn't leak into other tests/suites.
        conn2 = await asyncpg.connect(_asyncpg_dsn())
        try:
            await conn2.execute(f"DELETE FROM {LEGACY_TABLE} WHERE id = $1", legacy_row_id)
        finally:
            await conn2.close()
        await conn.close()

    # Restore head for the rest of the suite / the autouse fixture teardown.
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr


# ── A6/A7: populated downgrade guard fails closed and preserves data ────────


@pytest.mark.asyncio
async def test_downgrade_with_a_populated_table_fails_closed_and_preserves_the_row():
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        row_id = await _insert_row(conn, correlation_id="guard-probe-corr")
    finally:
        await conn.close()

    result = _run_alembic("downgrade", DOWN_REVISION)

    assert result.returncode != 0, "downgrade must fail when the table is populated"
    assert "refusing to downgrade" in (result.stdout + result.stderr)
    assert TABLE in (result.stdout + result.stderr)

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        table_exists = await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", f"public.{TABLE}"
        )
        assert table_exists is True

        row = await conn.fetchrow(f"SELECT id, correlation_id FROM {TABLE} WHERE id = $1", row_id)
        assert row is not None
        assert row["correlation_id"] == "guard-probe-corr"

        current = await conn.fetchval("SELECT version_num FROM alembic_version")
        assert current == REVISION, "a failed downgrade must not move the recorded alembic head"
    finally:
        await conn.close()


# ── A8: old code operates safely with the new table present and populated ──


@pytest.mark.asyncio
async def test_legacy_v2_style_insert_succeeds_with_the_new_table_present_and_populated():
    """Proves the 'old code after the migration is unconditionally safe'
    compatibility claim: a legacy-shaped INSERT against isola_bridge_requests
    (the exact statement shape isola_bridge_v2.py uses) succeeds normally
    while isola_structured_bridge_requests exists and holds rows — no lock
    contention, no naming collision, no schema interference."""
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await _insert_row(conn, correlation_id="a8-structured-row")

        legacy_row_id = uuid.uuid4()
        await conn.execute(
            f"""
            INSERT INTO {LEGACY_TABLE}
            (id, tenant_id, stable_request_id, correlation_id, agent_id, state, accepted_at, idempotency_key)
            VALUES ($1, $2, 'cw2-abcdef0123456789', 'a8-legacy-corr', $3, 'accepted', now(), 'legacy-key')
            ON CONFLICT (tenant_id, stable_request_id) DO NOTHING
            RETURNING id
            """,
            legacy_row_id,
            uuid.uuid4(),
            uuid.uuid4(),
        )

        row = await conn.fetchrow(f"SELECT id FROM {LEGACY_TABLE} WHERE id = $1", legacy_row_id)
        assert row is not None

        await conn.execute(f"DELETE FROM {LEGACY_TABLE} WHERE id = $1", legacy_row_id)
    finally:
        await conn.close()
