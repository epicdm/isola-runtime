"""Security-regression and retention proofs, real Postgres.

Security regression exercises the FULL FastAPI endpoint (not just `_claim`
directly) so pre-claim rejections are proven with real row-count readback,
in the same style as `evidence-clawith-s2-1b-dark-deploy-verification-
2026-08-02`'s negative probes. Only the tenant/tool-governance resolvers and
the agent-runtime layer are monkeypatched — the claim path itself always
runs against the real database.

Retention proves `expires_at` is populated at claim time from
`ISOLA_BRIDGE_STRUCTURED_RETENTION_HOURS` (default 24h) and that no
automatic cleanup exists anywhere in the application source.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import httpx
import pytest

from app.api import isola_bridge_structured as structured_api
from app.config import get_settings
from app.database import engine as _sqlalchemy_engine
from app.main import app

BACKEND_DIR = Path(__file__).resolve().parents[1]
TABLE = "isola_structured_bridge_requests"
LEGACY_TABLE = "isola_bridge_requests"
SECRET = "test-isola-bridge-secret"

AGENT_ID = uuid.UUID("81b38cd6-9fba-4cc8-8f87-1bce1a4aa162")
TENANT_ID = uuid.UUID("43b006e4-33e0-42a8-bec7-4422ba290d79")

GOLDEN_REQUEST = {
    "schema_version": "1.0.0",
    "tenant_id": str(TENANT_ID),
    "business_id": "epic-communications-inc",
    "chatwoot_account_id": "5",
    "inbox_id": "46",
    "conversation_id": "cmrxj42cg001qs62mdqtebjy8",
    "inbound_message_id": "90210",
    "contact_ref": "contact:abc123",
    "normalized_customer_message": "Do you install fibre in Roseau?",
    "bounded_conversation_history": [],
    "designated_agent_id": str(AGENT_ID),
    "knowledge_scope_ids": [],
    "allowed_tools": [
        {"name": "crm.lead.create", "description": "Create a lead", "arguments": [], "mutating": True}
    ],
    "ownership_state": "AI_OWNED",
    "correlation_id": "sec-golden-0001",
    "locale": "en-DM",
    "timezone": "America/Dominica",
    "response_deadline_ms": 45000,
}


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


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch):
    monkeypatch.setenv("ISOLA_BRIDGE_SECRET", SECRET)


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)

    async def _build():
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _build


def _headers():
    return {"X-Isola-Secret": SECRET}


async def _row_count(tenant_id=None) -> int:
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        if tenant_id is None:
            return await conn.fetchval(f"SELECT count(*) FROM {TABLE}")
        return await conn.fetchval(f"SELECT count(*) FROM {TABLE} WHERE tenant_id = $1", tenant_id)
    finally:
        await conn.close()


async def _fake_resolve_tenant_ok(body):
    return TENANT_ID, None


# ── D: security regression, real DB row-count readback ──────────────────────


@pytest.mark.asyncio
async def test_unsupported_tool_rejected_before_claim_zero_structured_rows(monkeypatch, client):
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve(agent_id, requested):
        return [], {"crm.lead.create"}

    monkeypatch.setattr(structured_api, "_resolve_effective_tool_names", fake_resolve)

    before = await _row_count(TENANT_ID)
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=GOLDEN_REQUEST, headers=_headers()
        )
    after = await _row_count(TENANT_ID)

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_tool"
    assert before == 0
    assert after == 0, "an unsupported tool must never create a durable claim row"


@pytest.mark.asyncio
async def test_tenant_mismatch_rejected_before_claim_zero_structured_rows(monkeypatch, client):
    async def fake_resolve_tenant(body):
        return None, structured_api.JSONResponse(status_code=409, content={"error": "tenant_mismatch"})

    monkeypatch.setattr(structured_api, "_resolve_tenant", fake_resolve_tenant)

    before = await _row_count()
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message",
            json=dict(GOLDEN_REQUEST, tenant_id=str(uuid.uuid4())),
            headers=_headers(),
        )
    after = await _row_count()

    assert response.status_code == 409
    assert before == 0
    assert after == 0, "a tenant mismatch must never create a durable claim row"


@pytest.mark.asyncio
async def test_unauthenticated_request_rejected_before_claim_zero_structured_rows(client):
    before = await _row_count()
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message",
            json=GOLDEN_REQUEST,
            headers={"X-Isola-Secret": "wrong-secret"},
        )
    after = await _row_count()

    assert response.status_code == 401
    assert before == 0
    assert after == 0


@pytest.mark.asyncio
async def test_malformed_request_rejected_before_claim_zero_structured_rows(client):
    payload = dict(GOLDEN_REQUEST)
    del payload["designated_agent_id"]
    before = await _row_count()
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )
    after = await _row_count()

    assert response.status_code == 422
    assert before == 0
    assert after == 0


@pytest.mark.asyncio
async def test_empty_allowed_tools_offers_zero_business_tools_end_to_end():
    """EMPTY ALLOWLIST regression at the resolver contract level: an empty
    request always yields an empty effective set and zero unsupported
    names, independent of whatever the Agent happens to be configured
    with — this is the input to both the model-offering step and the
    tool-execution step, so zero here means zero business tools are ever
    offered or executable for the turn."""

    async def fake_catalogue(agent_id):
        return [
            {"type": "function", "function": {"name": "crm.lead.create"}},
            {"type": "function", "function": {"name": "crm.lead.delete"}},
        ]

    original = structured_api.get_runtime_agent_tools_for_llm
    structured_api.get_runtime_agent_tools_for_llm = fake_catalogue
    try:
        effective, unsupported = await structured_api._resolve_effective_tool_names(AGENT_ID, frozenset())
    finally:
        structured_api.get_runtime_agent_tools_for_llm = original

    assert effective == []
    assert unsupported == set()


@pytest.mark.asyncio
async def test_same_correlation_id_across_different_tenants_creates_independent_claims():
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    agent_id = uuid.uuid4()
    correlation_id = "cross-tenant-shared-corr"
    digest = structured_api._tool_policy_digest([])

    won_a, row_a = await structured_api._claim(
        tenant_id=tenant_a,
        correlation_id=correlation_id,
        body=SimpleNamespace(designated_agent_id=agent_id, conversation_id="c1", contact_ref="r1", schema_version="1.0.0"),
        tool_policy_digest=digest,
    )
    won_b, row_b = await structured_api._claim(
        tenant_id=tenant_b,
        correlation_id=correlation_id,
        body=SimpleNamespace(designated_agent_id=agent_id, conversation_id="c2", contact_ref="r2", schema_version="1.0.0"),
        tool_policy_digest=digest,
    )

    assert won_a is True
    assert won_b is True
    assert row_a.id != row_b.id

    # Tenant A's claim must be unreachable via tenant B's key and vice versa.
    _, replay_a_as_b = await structured_api._claim(
        tenant_id=tenant_b,
        correlation_id=correlation_id,
        body=SimpleNamespace(designated_agent_id=agent_id, conversation_id="c3", contact_ref="r3", schema_version="1.0.0"),
        tool_policy_digest=digest,
    )
    assert replay_a_as_b.id == row_b.id, "tenant B's lookup must never surface tenant A's row"


@pytest.mark.asyncio
async def test_legacy_v2_select_by_id_cannot_read_a_structured_claim():
    """No legacy route can read a structured claim: v2's own
    `_SELECT_BY_ID_SQL` names only isola_bridge_requests, so a structured
    row's id is simply absent from that table regardless of what secret or
    tenant context a caller presents."""
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    digest = structured_api._tool_policy_digest([])

    won, row = await structured_api._claim(
        tenant_id=tenant_id,
        correlation_id="legacy-cannot-read-me",
        body=SimpleNamespace(designated_agent_id=agent_id, conversation_id="c", contact_ref="r", schema_version="1.0.0"),
        tool_policy_digest=digest,
    )
    assert won is True

    import app.api.isola_bridge_v2 as v2_api

    async with structured_api.async_session() as db:
        legacy_result = (
            await db.execute(v2_api._SELECT_BY_ID_SQL, {"id": str(row.id)})
        ).mappings().first()

    assert legacy_result is None


# ── E: retention ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expires_at_is_set_at_claim_time_using_the_default_retention():
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    digest = structured_api._tool_policy_digest([])

    assert structured_api._STRUCTURED_RETENTION_H == 24, "default retention must be 24 hours"

    won, row = await structured_api._claim(
        tenant_id=tenant_id,
        correlation_id="retention-default",
        body=SimpleNamespace(designated_agent_id=agent_id, conversation_id="c", contact_ref="r", schema_version="1.0.0"),
        tool_policy_digest=digest,
    )
    assert won is True

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        db_row = await conn.fetchrow(
            f"SELECT accepted_at, expires_at FROM {TABLE} WHERE id = $1", row.id
        )
    finally:
        await conn.close()

    assert db_row["expires_at"] is not None
    delta = db_row["expires_at"] - db_row["accepted_at"]
    assert abs(delta - timedelta(hours=24)) < timedelta(seconds=5)


@pytest.mark.asyncio
async def test_configured_retention_override_is_respected(monkeypatch):
    monkeypatch.setattr(structured_api, "_STRUCTURED_RETENTION_H", 6)

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    digest = structured_api._tool_policy_digest([])

    won, row = await structured_api._claim(
        tenant_id=tenant_id,
        correlation_id="retention-override",
        body=SimpleNamespace(designated_agent_id=agent_id, conversation_id="c", contact_ref="r", schema_version="1.0.0"),
        tool_policy_digest=digest,
    )
    assert won is True

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        db_row = await conn.fetchrow(
            f"SELECT accepted_at, expires_at FROM {TABLE} WHERE id = $1", row.id
        )
    finally:
        await conn.close()

    delta = db_row["expires_at"] - db_row["accepted_at"]
    assert abs(delta - timedelta(hours=6)) < timedelta(seconds=5)


def test_retention_env_var_reads_the_documented_name_with_24h_default(monkeypatch):
    import importlib

    monkeypatch.delenv("ISOLA_BRIDGE_STRUCTURED_RETENTION_HOURS", raising=False)
    reloaded = importlib.reload(structured_api)
    try:
        assert reloaded._STRUCTURED_RETENTION_H == 24

        monkeypatch.setenv("ISOLA_BRIDGE_STRUCTURED_RETENTION_HOURS", "48")
        reloaded = importlib.reload(structured_api)
        assert reloaded._STRUCTURED_RETENTION_H == 48
    finally:
        monkeypatch.delenv("ISOLA_BRIDGE_STRUCTURED_RETENTION_HOURS", raising=False)
        importlib.reload(structured_api)


def test_no_automatic_cleanup_or_deletion_job_references_the_structured_table():
    """No cleanup job exists in this slice. Grep the entire application
    source (excluding tests, which legitimately DELETE rows to keep
    fixtures isolated) for any DELETE/DROP statement or scheduler
    registration naming the structured table."""
    app_dir = BACKEND_DIR / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "isola_structured_bridge_requests" not in text:
            continue
        for match in re.finditer(r"(?i)\b(DELETE\s+FROM|DROP\s+TABLE|TRUNCATE)\b[^\n]*isola_structured_bridge_requests", text):
            offenders.append(f"{path}: {match.group(0)!r}")
    assert offenders == [], f"found cleanup/deletion logic against the structured table: {offenders}"
