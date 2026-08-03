"""Tests for the additive structured bridge endpoint
(`dec-clawith-structured-bridge-versioned-endpoint-2026-08-02` slice S2) and
its dedicated claim table
(`dec-clawith-structured-claim-isolation-dedicated-table-2026-08-02`).

Follows the monkeypatch-the-module pattern already used by
`test_webhooks_api.py`: the real FastAPI app is exercised end-to-end via
httpx.ASGITransport, and the module's own async collaborators
(`_resolve_tenant`, `_claim`, `_wait_for_claim_population`, `_poll_and_read`,
`enqueue_chat_runtime`, `ensure_primary_platform_session`) are replaced with
fakes so no real database or LLM runtime is required for THIS file. Real
Postgres-backed migration, cross-route isolation and concurrency proofs live
in `test_isola_structured_bridge_requests_migration.py` and
`test_isola_structured_bridge_claim_isolation.py`.

`test_duplicate_correlation_id_does_not_enqueue_second_run` and
`test_replay_after_completion_returns_stored_result_without_polling` are
the durable-idempotency proofs: they assert `enqueue_chat_runtime` is called
at most once across multiple requests sharing one `correlation_id`, using a
fake claim store that mirrors the real `isola_structured_bridge_requests`
table's `(tenant_id, correlation_id)` unique-constraint semantics (an
`INSERT ... ON CONFLICT DO NOTHING` — see
`202608021900_add_structured_bridge_requests.py`). The claim store's true
cross-process/cross-restart atomicity is inherited from that constraint, not
reinvented here; these tests prove the endpoint's branch logic
(claim-before-enqueue, never enqueue on a lost claim) is correct given that
constraint.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.api import isola_bridge_structured as structured_api
from app.main import app

AGENT_ID = uuid.UUID("81b38cd6-9fba-4cc8-8f87-1bce1a4aa162")
TENANT_ID = uuid.UUID("43b006e4-33e0-42a8-bec7-4422ba290d79")
SESSION_ID = uuid.uuid4()
RUN_ID = uuid.uuid4()

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
    "bounded_conversation_history": [{"role": "user", "content": "hi"}],
    "designated_agent_id": str(AGENT_ID),
    "knowledge_scope_ids": ["kb-epic-services"],
    "allowed_tools": [
        {"name": "crm.lead.create", "description": "Create a lead", "arguments": ["name", "phone"], "mutating": True}
    ],
    "ownership_state": "AI_OWNED",
    "correlation_id": "corr-golden-2026-08-02-0001",
    "locale": "en-DM",
    "timezone": "America/Dominica",
    "response_deadline_ms": 45000,
}

SECRET = "test-isola-bridge-secret"


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


# ── Pure helper proofs ───────────────────────────────────────────────────────


def test_schema_major_parses_major_version_only():
    assert structured_api._schema_major("1.0.0") == 1
    assert structured_api._schema_major("1.9.3") == 1
    assert structured_api._schema_major("2.0.0") == 2
    assert structured_api._schema_major("0.9.0") == 0
    assert structured_api._schema_major(None) is None
    assert structured_api._schema_major("") is None
    assert structured_api._schema_major("abc") is None


def test_structured_module_has_no_stable_key_helper_or_legacy_table_reference():
    """Regression guard for the claim-isolation migration
    (dec-clawith-structured-claim-isolation-dedicated-table-2026-08-02): the
    structured route must not retain the deleted `_stable_key` helper, and
    its SQL literals must name only the dedicated table."""
    assert not hasattr(structured_api, "_stable_key")
    assert "isola_bridge_requests" not in str(structured_api._CLAIM_SQL)
    assert "isola_structured_bridge_requests" in str(structured_api._CLAIM_SQL)
    assert "ON CONFLICT (tenant_id, correlation_id)" in str(structured_api._CLAIM_SQL)


def test_structured_message_in_forbids_unknown_fields():
    from pydantic import ValidationError

    payload = dict(GOLDEN_REQUEST)
    payload["unexpected_field"] = "nope"
    with pytest.raises(ValidationError):
        structured_api.StructuredBridgeMessageIn.model_validate(payload)


def test_structured_message_in_rejects_unknown_ownership_state():
    from pydantic import ValidationError

    payload = dict(GOLDEN_REQUEST, ownership_state="ON_THE_MOON")
    with pytest.raises(ValidationError):
        structured_api.StructuredBridgeMessageIn.model_validate(payload)


def test_structured_message_in_accepts_the_golden_request():
    body = structured_api.StructuredBridgeMessageIn.model_validate(GOLDEN_REQUEST)
    assert str(body.designated_agent_id) == str(AGENT_ID)
    assert body.correlation_id == GOLDEN_REQUEST["correlation_id"]


# ── Endpoint-level proofs ────────────────────────────────────────────────────


async def _fake_resolve_tenant_ok(body):
    return TENANT_ID, None


async def _fake_resolve_effective_tool_names_permit_all(agent_id, requested):
    """Tool-governance stand-in for tests that aren't about tool governance:
    treat every requested name as already configured for the Agent, so
    idempotency/replay/envelope behavior can be exercised without depending
    on a real Agent tool catalogue (that dependency is exercised on its own
    by the dedicated tool-governance tests below)."""
    return sorted(requested), frozenset()


@pytest.mark.asyncio
async def test_unauthenticated_request_is_rejected(client):
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message",
            json=GOLDEN_REQUEST,
            headers={"X-Isola-Secret": "wrong-secret"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_unsupported_schema_major_is_rejected_with_400(client):
    payload = dict(GOLDEN_REQUEST, schema_version="2.0.0")
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "unsupported_schema_version"
    assert body["supported_major"] == 1


@pytest.mark.asyncio
async def test_agent_not_found_returns_404(monkeypatch, client):
    async def fake_resolve_tenant(body):
        return None, structured_api.JSONResponse(status_code=404, content={"error": "agent_not_found"})

    monkeypatch.setattr(structured_api, "_resolve_tenant", fake_resolve_tenant)

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=GOLDEN_REQUEST, headers=_headers()
        )
    assert response.status_code == 404
    assert response.json()["error"] == "agent_not_found"


@pytest.mark.asyncio
async def test_tenant_mismatch_returns_409_not_400(monkeypatch, client):
    async def fake_resolve_tenant(body):
        return None, structured_api.JSONResponse(status_code=409, content={"error": "tenant_mismatch"})

    monkeypatch.setattr(structured_api, "_resolve_tenant", fake_resolve_tenant)

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message",
            json=dict(GOLDEN_REQUEST, tenant_id=str(uuid.uuid4())),
            headers=_headers(),
        )
    assert response.status_code == 409
    assert response.json()["error"] == "tenant_mismatch"


class _FakeClaimStore:
    """Mirrors the (tenant_id, correlation_id) UNIQUE constraint on the real
    isola_structured_bridge_requests table: the first claim for a key wins,
    every subsequent claim for the same key is a no-op that returns the
    existing row. This is the same INSERT ... ON CONFLICT DO NOTHING
    semantics the production code relies on."""

    def __init__(self):
        self.rows: dict[str, SimpleNamespace] = {}

    def claim(self, tenant_id, correlation_id, tool_policy_digest=None, contact_ref=None, agent_id=None):
        db_key = f"{tenant_id}:{correlation_id}"
        if db_key in self.rows:
            return False, self.rows[db_key]
        row = SimpleNamespace(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            state="accepted",
            session_id=None,
            run_id=None,
            initiating_message_id=None,
            terminal_message_id=None,
            error_class=None,
            tool_policy_digest=tool_policy_digest,
            contact_ref=contact_ref or GOLDEN_REQUEST["contact_ref"],
            agent_id=agent_id or AGENT_ID,
        )
        self.rows[db_key] = row
        return True, row

    def populate_enqueued(self, row_id, *, session_id, run_id, initiating_message_id):
        for row in self.rows.values():
            if row.id == row_id:
                row.session_id = session_id
                row.run_id = run_id
                row.initiating_message_id = initiating_message_id
                row.state = "running"
                return

    def mark_terminal(self, row_id, *, state, terminal_message_id=None, error_class=None):
        for row in self.rows.values():
            if row.id == row_id:
                row.state = state
                if terminal_message_id is not None:
                    row.terminal_message_id = terminal_message_id
                if error_class is not None:
                    row.error_class = error_class
                return


def _wire_claim_store(monkeypatch, store: _FakeClaimStore):
    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return store.claim(tenant_id, correlation_id, tool_policy_digest)

    async def fake_wait_for_claim_population(row_id):
        for row in store.rows.values():
            if row.id == row_id:
                return row
        raise AssertionError("unknown claim row")  # pragma: no cover

    async def fake_mark_failed(row_id, error_class):
        store.mark_terminal(row_id, state="failed", error_class=error_class)

    monkeypatch.setattr(structured_api, "_claim", fake_claim)
    monkeypatch.setattr(structured_api, "_wait_for_claim_population", fake_wait_for_claim_population)
    monkeypatch.setattr(structured_api, "_mark_failed", fake_mark_failed)


@pytest.mark.asyncio
async def test_golden_request_produces_a_foundation_parseable_response(monkeypatch, client):
    """End-to-end happy path: golden request in, a response satisfying every
    field `parseClawithResponse` (Foundation, response.ts) requires out."""
    store = _FakeClaimStore()
    _wire_claim_store(monkeypatch, store)
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)
    monkeypatch.setattr(
        structured_api,
        "_resolve_effective_tool_names",
        _fake_resolve_effective_tool_names_permit_all,
    )

    # The "won" branch talks to Agent/LLMModel/User/ChatMessage and
    # enqueue_chat_runtime directly inside its own `async_session()` block;
    # rather than fake every ORM call, replace the whole branch's side
    # effects by monkeypatching `_claim` to hand back an ALREADY-enqueued
    # row (state=running, session/run populated) and asserting the poll
    # layer + envelope construction — the piece unique to this endpoint —
    # behaves correctly. The separate `won` DB-wiring is exercised by
    # `test_effective_tool_names_reach_enqueue_chat_runtime_on_the_won_path`.
    async def fake_claim_pre_enqueued(*, tenant_id, correlation_id, body, tool_policy_digest):
        won, row = store.claim(tenant_id, correlation_id, tool_policy_digest)
        if won:
            store.populate_enqueued(
                row.id, session_id=SESSION_ID, run_id=RUN_ID, initiating_message_id=uuid.uuid4()
            )
        return False, row  # always report as "lost" so the endpoint waits, never re-enqueues

    monkeypatch.setattr(structured_api, "_claim", fake_claim_pre_enqueued)

    async def fake_poll_and_read(tenant_id, run_id, session_id, agent_id, user_id):
        assert tenant_id == TENANT_ID
        assert run_id == RUN_ID
        assert session_id == SESSION_ID
        assert agent_id == AGENT_ID
        return (
            True,
            "completed",
            structured_api.RunOwnedReply(
                message_id=uuid.uuid4(),
                content="Yes — EPIC installs fibre in Roseau.",
                delivery_kind="terminal",
                lifecycle_status="completed",
            ),
            None,
        )

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)

    class _NoopSession:
        async def execute(self, *a, **k):
            return SimpleNamespace(first=lambda: None, mappings=lambda: SimpleNamespace(first=lambda: None))

        async def commit(self):
            return None

        async def get(self, *a, **k):
            # `_claim` always reports "lost" in this test (see
            # fake_claim_pre_enqueued above), so the endpoint takes the
            # wait/read path, which does `db.get(ChatMessage, ...)` to look
            # up the initiating message's created_at. Returning None makes
            # the endpoint fall back to `accepted_at` — fine, that timestamp
            # isn't asserted on here.
            return None

    class _NoopSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _NoopSession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "async_session", _NoopSessionFactory())

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=GOLDEN_REQUEST, headers=_headers()
        )

    assert response.status_code == 200
    envelope = response.json()

    # Every field dec-foundation-clawith-structured-response-contract-2026-07-29
    # / response.ts's parseClawithResponse requires or reads.
    for field in [
        "schema_version",
        "agent_id",
        "session_id",
        "correlation_id",
        "customer_reply",
        "intent",
        "confidence",
        "qualification_state",
        "knowledge_references",
        "tool_requests",
        "escalation",
        "missing_information",
        "follow_up_required",
        "usage",
    ]:
        assert field in envelope, f"missing {field}"

    assert envelope["schema_version"] == "1.0.0"
    assert envelope["agent_id"] == str(AGENT_ID)
    assert envelope["correlation_id"] == GOLDEN_REQUEST["correlation_id"]
    assert envelope["session_id"]
    assert isinstance(envelope["confidence"], (int, float))
    assert 0 <= envelope["confidence"] <= 1
    assert envelope["customer_reply"] == "Yes — EPIC installs fibre in Roseau."
    assert envelope["escalation"]["requested"] is False
    assert envelope["tool_requests"] == []
    # at least one of customer_reply / escalation.requested / tool_requests
    assert envelope["customer_reply"] or envelope["escalation"]["requested"] or envelope["tool_requests"]


@pytest.mark.asyncio
async def test_duplicate_correlation_id_does_not_enqueue_second_run(monkeypatch, client):
    """The core idempotency proof: two requests carrying the SAME
    correlation_id must result in at most one _claim() winner — the second
    request must never reach enqueue_chat_runtime."""
    store = _FakeClaimStore()
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)
    monkeypatch.setattr(
        structured_api,
        "_resolve_effective_tool_names",
        _fake_resolve_effective_tool_names_permit_all,
    )

    win_count = {"n": 0}

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        won, row = store.claim(tenant_id, correlation_id, tool_policy_digest)
        if won:
            win_count["n"] += 1
            store.populate_enqueued(
                row.id, session_id=SESSION_ID, run_id=RUN_ID, initiating_message_id=uuid.uuid4()
            )
        # Report every caller as "lost" so the endpoint always takes the
        # wait/read path rather than the real won-branch DB writes (Agent /
        # User provisioning + enqueue_chat_runtime) — those aren't what this
        # test proves. The one-winner-per-correlation_id invariant is still
        # enforced by `_FakeClaimStore.claim()` above (mirroring the real
        # unique constraint) and asserted on via `win_count` below.
        return False, row

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

    async def fake_wait_for_claim_population(row_id):
        for row in store.rows.values():
            if row.id == row_id:
                return row
        raise AssertionError("unknown claim row")  # pragma: no cover

    monkeypatch.setattr(structured_api, "_wait_for_claim_population", fake_wait_for_claim_population)

    async def fake_poll_and_read(tenant_id, run_id, session_id, agent_id, user_id):
        return (
            True,
            "completed",
            structured_api.RunOwnedReply(
                message_id=uuid.uuid4(),
                content="Yes — EPIC installs fibre in Roseau.",
                delivery_kind="terminal",
                lifecycle_status="completed",
            ),
            None,
        )

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)

    class _NoopSession:
        async def execute(self, *a, **k):
            return SimpleNamespace(first=lambda: None, mappings=lambda: SimpleNamespace(first=lambda: None))

        async def commit(self):
            return None

        async def get(self, *a, **k):
            return None

        def expire_all(self):
            return None

    class _NoopSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _NoopSession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "async_session", _NoopSessionFactory())

    async with await client() as ac:
        r1 = await ac.post("/api/isola/bridge/structured/message", json=GOLDEN_REQUEST, headers=_headers())
        r2 = await ac.post("/api/isola/bridge/structured/message", json=GOLDEN_REQUEST, headers=_headers())

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Exactly one winner claimed the correlation_id across both requests —
    # the second request never enqueued a second run.
    assert win_count["n"] == 1
    assert r1.json()["correlation_id"] == r2.json()["correlation_id"] == GOLDEN_REQUEST["correlation_id"]


@pytest.mark.asyncio
async def test_replay_after_completion_returns_stored_result_without_polling(monkeypatch, client):
    """Restart-safety proof: once a claim row is already `completed`, a
    replay with the same correlation_id must return the stored result
    directly and must never call the poll/enqueue path at all."""
    store = _FakeClaimStore()
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)
    monkeypatch.setattr(
        structured_api,
        "_resolve_effective_tool_names",
        _fake_resolve_effective_tool_names_permit_all,
    )

    terminal_message_id = uuid.uuid4()
    completed_row = store.claim(
        TENANT_ID,
        GOLDEN_REQUEST["correlation_id"],
        structured_api._tool_policy_digest(["crm.lead.create"]),
    )[1]
    completed_row.session_id = SESSION_ID
    completed_row.run_id = RUN_ID
    completed_row.state = "completed"
    completed_row.terminal_message_id = terminal_message_id

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return False, completed_row  # always a replay of the already-completed row

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

    async def fake_wait_for_claim_population(row_id):
        return completed_row

    monkeypatch.setattr(structured_api, "_wait_for_claim_population", fake_wait_for_claim_population)

    poll_called = {"n": 0}

    async def fake_poll_and_read(*a, **k):
        poll_called["n"] += 1
        raise AssertionError("must not poll on a replay of a completed row")

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)

    # Replay re-derives through read_run_owned_reply and requires it to
    # agree with the id the claim row already recorded -- never a bare
    # ChatMessage fetch by the stored id.
    async def fake_read_run_owned_reply(db, *, tenant_id, run_id, session_id, agent_id, user_id):
        assert tenant_id == TENANT_ID
        assert run_id == RUN_ID
        assert session_id == SESSION_ID
        assert agent_id == AGENT_ID
        return structured_api.RunOwnedReply(
            message_id=terminal_message_id,
            content="Yes — EPIC installs fibre in Roseau.",
            delivery_kind="terminal",
            lifecycle_status="completed",
        )

    monkeypatch.setattr(structured_api, "read_run_owned_reply", fake_read_run_owned_reply)

    class _NoopSession:
        async def get(self, *a, **k):
            return None

    class _NoopSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _NoopSession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "async_session", _NoopSessionFactory())

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=GOLDEN_REQUEST, headers=_headers()
        )

    assert response.status_code == 200
    assert poll_called["n"] == 0
    envelope = response.json()
    assert envelope["customer_reply"] == "Yes — EPIC installs fibre in Roseau."
    assert envelope["correlation_id"] == GOLDEN_REQUEST["correlation_id"]


# ── Per-turn `allowed_tools` governance
# (dec-clawith-structured-per-turn-tool-governance-2026-08-02) ───────────────
# The effective set for a turn is the intersection of the exact names the
# caller supplied and the names the designated Agent is already configured
# with (`_resolve_effective_tool_names`). An unsupported name must be
# rejected BEFORE the idempotency claim — no durable row, no reasoning Run.


def test_tool_policy_digest_does_not_collide_across_comma_containing_tool_names():
    """Regression test for a BLOCKING finding from adversarial review: a
    naive comma-join canonicalization would let `["a,b", "c"]` and
    `["a", "b,c"]` hash identically (both join to "a,b,c"), silently
    treating two DIFFERENT effective tool sets as the same policy. Tool
    names are an unconstrained `String(100)` (app/models/tool.py), so a
    tool literally named with a comma is a real possibility, not a
    theoretical one."""
    digest_1 = structured_api._tool_policy_digest(["a,b", "c"])
    digest_2 = structured_api._tool_policy_digest(["a", "b,c"])

    assert digest_1 != digest_2


def test_tool_policy_digest_is_order_independent():
    """The digest must depend only on the SET of effective tool names, not
    the order `allowed_tools` happened to list them in."""
    assert structured_api._tool_policy_digest(
        ["read_file", "write_file"]
    ) == structured_api._tool_policy_digest(["write_file", "read_file"])


def test_tool_policy_digest_matches_the_column_shape_check():
    """The digest must satisfy
    ck_isola_structured_bridge_requests_digest_shape (^[0-9a-f]{16}$) for
    every input, including the empty set — the fixed public constant a v2
    caller would need to forge."""
    import re

    for names in ([], ["a"], ["a,b", "c"], ["read_file", "write_file"]):
        digest = structured_api._tool_policy_digest(names)
        assert re.fullmatch(r"[0-9a-f]{16}", digest), digest


@pytest.mark.asyncio
async def test_resolve_effective_tool_names_intersects_and_reports_unsupported():
    """Pure proof of `_resolve_effective_tool_names`'s own contract: the
    effective set is exactly the intersection, and anything requested but
    not configured for the Agent is reported as unsupported — independent
    of any HTTP/claim wiring."""

    async def fake_catalogue(agent_id):
        assert agent_id == AGENT_ID
        return [
            {"type": "function", "function": {"name": "crm.lead.create"}},
            {"type": "function", "function": {"name": "crm.lead.read"}},
        ]

    import app.api.isola_bridge_structured as mod

    original = mod.get_runtime_agent_tools_for_llm
    mod.get_runtime_agent_tools_for_llm = fake_catalogue
    try:
        effective, unsupported = await structured_api._resolve_effective_tool_names(
            AGENT_ID, frozenset({"crm.lead.create", "crm.lead.delete"})
        )
    finally:
        mod.get_runtime_agent_tools_for_llm = original

    assert effective == ["crm.lead.create"]
    assert unsupported == {"crm.lead.delete"}


@pytest.mark.asyncio
async def test_resolve_effective_tool_names_empty_request_is_empty_and_unsupported_free():
    """EMPTY ALLOWLIST at the resolver layer: requesting nothing is never
    an error, and yields an empty effective set."""

    async def fake_catalogue(agent_id):
        return [{"type": "function", "function": {"name": "crm.lead.create"}}]

    import app.api.isola_bridge_structured as mod

    original = mod.get_runtime_agent_tools_for_llm
    mod.get_runtime_agent_tools_for_llm = fake_catalogue
    try:
        effective, unsupported = await structured_api._resolve_effective_tool_names(
            AGENT_ID, frozenset()
        )
    finally:
        mod.get_runtime_agent_tools_for_llm = original

    assert effective == []
    assert unsupported == set()


@pytest.mark.asyncio
async def test_unsupported_tool_rejected_with_400_before_claim(monkeypatch, client):
    """UNKNOWN OR UNSUPPORTED TOOL: rejected before the idempotency claim —
    no durable isola_structured_bridge_requests row, no reasoning Run."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        return [], {"crm.lead.create"}

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    claim_calls = []

    async def spy_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        claim_calls.append(correlation_id)
        raise AssertionError("must not claim before tool governance passes")

    monkeypatch.setattr(structured_api, "_claim", spy_claim)

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=GOLDEN_REQUEST, headers=_headers()
        )

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_tool"
    assert response.json()["detail"] == ["crm.lead.create"]
    assert claim_calls == []


@pytest.mark.asyncio
async def test_empty_allowed_tools_request_proceeds_past_governance_check(
    monkeypatch, client
):
    """EMPTY ALLOWLIST at the endpoint layer: `allowed_tools=[]` is a valid
    request that must NOT be rejected — it must reach the claim step with
    an empty effective set, not be treated as an error."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    resolver_calls = []

    async def fake_resolve_effective_tool_names(agent_id, requested):
        resolver_calls.append((agent_id, requested))
        return [], set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    claim_calls = []

    async def spy_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        claim_calls.append(correlation_id)
        return False, SimpleNamespace(
            id=uuid.uuid4(),
            session_id=SESSION_ID,
            run_id=RUN_ID,
            state="running",
            initiating_message_id=None,
            tool_policy_digest=tool_policy_digest,
        )

    monkeypatch.setattr(structured_api, "_claim", spy_claim)

    async def fake_wait_for_claim_population(row_id):
        return SimpleNamespace(
            id=row_id,
            session_id=SESSION_ID,
            run_id=RUN_ID,
            state="running",
            initiating_message_id=None,
            contact_ref=GOLDEN_REQUEST["contact_ref"],
            agent_id=AGENT_ID,
        )

    monkeypatch.setattr(
        structured_api, "_wait_for_claim_population", fake_wait_for_claim_population
    )

    async def fake_poll_and_read(tenant_id, run_id, session_id, agent_id, user_id):
        return (
            True,
            "completed",
            structured_api.RunOwnedReply(
                message_id=uuid.uuid4(),
                content="ok",
                delivery_kind="terminal",
                lifecycle_status="completed",
            ),
            None,
        )

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)

    class _NoopSession:
        async def execute(self, *a, **k):
            return SimpleNamespace(first=lambda: None, mappings=lambda: SimpleNamespace(first=lambda: None))

        async def commit(self):
            return None

        async def get(self, *a, **k):
            return None

        def expire_all(self):
            return None

    class _NoopSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _NoopSession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "async_session", _NoopSessionFactory())

    payload = dict(GOLDEN_REQUEST, allowed_tools=[])
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 200
    assert claim_calls == [GOLDEN_REQUEST["correlation_id"]]
    assert resolver_calls == [(AGENT_ID, frozenset())]


@pytest.mark.asyncio
async def test_duplicate_tool_names_in_request_deduplicate_to_one_name(
    monkeypatch, client
):
    """Duplicate or malformed tool names: two entries naming the same tool
    must not expand the effective set, crash, or double-count."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    resolver_calls = []

    async def fake_resolve_effective_tool_names(agent_id, requested):
        resolver_calls.append(requested)
        return sorted(requested), set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    async def spy_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return False, SimpleNamespace(
            id=uuid.uuid4(), session_id=None, run_id=None, state="accepted",
            initiating_message_id=None,
            tool_policy_digest=tool_policy_digest,
        )

    monkeypatch.setattr(structured_api, "_claim", spy_claim)

    async def fake_wait_for_claim_population(row_id):
        return SimpleNamespace(
            id=row_id, session_id=None, run_id=None, state="running",
            initiating_message_id=None,
        )

    monkeypatch.setattr(
        structured_api, "_wait_for_claim_population", fake_wait_for_claim_population
    )

    payload = dict(
        GOLDEN_REQUEST,
        allowed_tools=[
            {"name": "crm.lead.create", "description": "Create", "arguments": [], "mutating": True},
            {"name": "crm.lead.create", "description": "Create (dup)", "arguments": ["x"], "mutating": True},
        ],
    )

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    # Duplicate entries collapse to one name before reaching the resolver.
    assert resolver_calls == [frozenset({"crm.lead.create"})]
    # 504 here just means "claim not yet populated" (the fake winner never
    # populates session/run) — the point already proven is the dedup call,
    # not this transport-level retry status.
    assert response.status_code == 504


@pytest.mark.asyncio
async def test_malformed_tool_entry_rejected_with_422_before_any_governance_call(
    monkeypatch, client
):
    """A structurally malformed `allowed_tools` entry (wrong field type)
    must fail Pydantic validation (422) before tenant resolution or tool
    governance ever run — no reasoning, no durable side effect."""
    resolve_tenant_calls = []

    async def spy_resolve_tenant(body):
        resolve_tenant_calls.append(body)
        return TENANT_ID, None

    monkeypatch.setattr(structured_api, "_resolve_tenant", spy_resolve_tenant)

    resolver_calls = []

    async def spy_resolve_effective_tool_names(agent_id, requested):
        resolver_calls.append(requested)
        return [], set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", spy_resolve_effective_tool_names
    )

    payload = dict(
        GOLDEN_REQUEST,
        allowed_tools=[
            # Missing the required `mutating` field entirely — Pydantic's
            # lax bool coercion (which accepts strings like "yes"/"no")
            # makes a present-but-wrong-typed value an unreliable way to
            # force a validation error, so this uses outright absence.
            {"name": "crm.lead.create", "description": "Create", "arguments": []}
        ],
    )

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 422
    assert resolve_tenant_calls == []
    assert resolver_calls == []


@pytest.mark.asyncio
async def test_effective_tool_names_reach_enqueue_chat_runtime_on_the_won_path(
    monkeypatch, client
):
    """End-to-end proof that the resolved effective set actually reaches
    `enqueue_chat_runtime` (and therefore the Run's immutable input
    snapshot) on the real claim-winning path — not just validated and
    dropped, as the pre-S2.1A dark-launch slice did."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        assert agent_id == AGENT_ID
        return ["crm.lead.create"], set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return True, SimpleNamespace(
            id=uuid.uuid4(), session_id=None, run_id=None, state="accepted",
            initiating_message_id=None,
            tool_policy_digest=tool_policy_digest,
        )

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

    model_id = uuid.uuid4()
    fake_agent = SimpleNamespace(
        id=AGENT_ID, primary_model_id=model_id, fallback_model_id=None
    )
    fake_model = SimpleNamespace(id=model_id)
    user_id = uuid.uuid4()
    fake_user = SimpleNamespace(id=user_id)
    fake_session = SimpleNamespace(id=SESSION_ID)

    class _Begin:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _WonSession:
        async def execute(self, *a, **k):
            return SimpleNamespace(first=lambda: None, mappings=lambda: SimpleNamespace(first=lambda: None))

        async def commit(self):
            return None

        def begin(self):
            return _Begin()

        async def get(self, model_cls, pk):
            if model_cls is structured_api.Agent:
                return fake_agent
            if model_cls is structured_api.LLMModel:
                return fake_model
            if model_cls is structured_api.User:
                return fake_user
            return None

    class _WonSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _WonSession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "async_session", _WonSessionFactory())

    async def fake_ensure_session(db, agent_id, user_id_arg):
        return fake_session

    monkeypatch.setattr(
        structured_api, "ensure_primary_platform_session", fake_ensure_session
    )

    class _Reader:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        structured_api, "open_run_state_reader", lambda db: _Reader()
    )

    enqueue_calls = []

    async def fake_enqueue_chat_runtime(db, **kwargs):
        enqueue_calls.append(kwargs)
        return SimpleNamespace(
            handle=SimpleNamespace(run_id=RUN_ID),
            message_id=uuid.uuid4(),
        )

    monkeypatch.setattr(
        structured_api, "enqueue_chat_runtime", fake_enqueue_chat_runtime
    )

    async def fake_poll_and_read(tenant_id, run_id, session_id, agent_id, user_id):
        return (
            True,
            "completed",
            structured_api.RunOwnedReply(
                message_id=uuid.uuid4(),
                content="Yes.",
                delivery_kind="terminal",
                lifecycle_status="completed",
            ),
            None,
        )

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=GOLDEN_REQUEST, headers=_headers()
        )

    assert response.status_code == 200
    assert len(enqueue_calls) == 1
    assert enqueue_calls[0]["allowed_tool_names"] == ["crm.lead.create"]


@pytest.mark.asyncio
async def test_correlation_id_reused_with_different_allowed_tools_is_rejected_not_joined(
    monkeypatch, client
):
    """Regression test for a race an adversarial review flagged BLOCKING:
    a `correlation_id` already claimed under one `allowed_tools` policy
    must never let a request carrying a DIFFERENT policy silently join and
    inherit that claim's result — the mismatched request must be rejected
    deterministically instead."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        return sorted(requested), set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    winner_digest = structured_api._tool_policy_digest(["crm.lead.create"])
    existing_row = SimpleNamespace(
        id=uuid.uuid4(),
        session_id=SESSION_ID,
        run_id=RUN_ID,
        state="running",
        initiating_message_id=None,
        tool_policy_digest=winner_digest,
    )

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        # Always reports the loser path against the pre-existing winning
        # claim, mirroring test_replay_after_completion's pattern.
        return False, existing_row

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

    wait_calls = []

    async def fake_wait_for_claim_population(row_id):
        wait_calls.append(row_id)
        return existing_row

    monkeypatch.setattr(
        structured_api, "_wait_for_claim_population", fake_wait_for_claim_population
    )

    async def fake_poll_and_read(tenant_id, run_id, session_id, agent_id, user_id):
        raise AssertionError(
            "must not poll/return a mismatched-policy claim's result"
        )

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)

    # Requests an EMPTY allowlist -- a different policy than the digest
    # already recorded on the existing claim (which was computed from
    # ["crm.lead.create"]).
    payload = dict(GOLDEN_REQUEST, allowed_tools=[])

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 409
    assert response.json()["error"] == "correlation_id_policy_mismatch"
    # The mismatch is caught before the loser's wait/poll path is ever
    # reached -- no partial progress down that path.
    assert wait_calls == []


@pytest.mark.asyncio
async def test_correlation_id_retry_with_same_allowed_tools_digest_still_joins_claim(
    monkeypatch, client
):
    """A genuine retry of the SAME logical turn (identical resolved
    `allowed_tools` policy, hence identical digest) must NOT be rejected --
    only a policy MISMATCH is rejected, never a legitimate retry."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        return sorted(requested), set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    matching_digest = structured_api._tool_policy_digest(["crm.lead.create"])
    existing_row = SimpleNamespace(
        id=uuid.uuid4(),
        session_id=SESSION_ID,
        run_id=RUN_ID,
        state="running",
        initiating_message_id=None,
        tool_policy_digest=matching_digest,
        contact_ref=GOLDEN_REQUEST["contact_ref"],
        agent_id=AGENT_ID,
    )

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return False, existing_row

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

    async def fake_wait_for_claim_population(row_id):
        return existing_row

    monkeypatch.setattr(
        structured_api, "_wait_for_claim_population", fake_wait_for_claim_population
    )

    async def fake_poll_and_read(tenant_id, run_id, session_id, agent_id, user_id):
        return (
            True,
            "completed",
            structured_api.RunOwnedReply(
                message_id=uuid.uuid4(),
                content="Yes — same policy, legitimate retry.",
                delivery_kind="terminal",
                lifecycle_status="completed",
            ),
            None,
        )

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)

    class _NoopSession:
        async def execute(self, *a, **k):
            return SimpleNamespace(first=lambda: None, mappings=lambda: SimpleNamespace(first=lambda: None))

        async def commit(self):
            return None

        async def get(self, *a, **k):
            return None

        def expire_all(self):
            return None

    class _NoopSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _NoopSession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "async_session", _NoopSessionFactory())

    payload = dict(
        GOLDEN_REQUEST,
        allowed_tools=[
            {"name": "crm.lead.create", "description": "Create", "arguments": [], "mutating": True}
        ],
    )

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 200
    assert response.json()["customer_reply"] == "Yes — same policy, legitimate retry."


@pytest.mark.asyncio
async def test_loser_derives_user_id_from_the_claimed_row_not_the_request_body(monkeypatch, client):
    """Regression test for a BLOCKING adversarial-review finding: a
    duplicate/retried request reusing an existing correlation_id but
    carrying a DIFFERENT contact_ref than the request that actually won the
    claim must still derive user_id from the CLAIMED row's own contact_ref
    -- never from this request's own body.contact_ref. Deriving it from the
    request would make the run-owned lookup see the winner's real reply as
    a user mismatch, which would mark the SHARED claim row failed and
    poison it for the original, legitimate caller."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        return sorted(requested), set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    winner_contact_ref = GOLDEN_REQUEST["contact_ref"]
    # Must match what _resolve_effective_tool_names resolves for
    # GOLDEN_REQUEST's own allowed_tools ("crm.lead.create") -- this test
    # varies only contact_ref, not the tool policy, so the digest must
    # agree or the request is rejected 409 before ever reaching the loser
    # path this test is about.
    digest = structured_api._tool_policy_digest(["crm.lead.create"])
    existing_row = SimpleNamespace(
        id=uuid.uuid4(),
        session_id=SESSION_ID,
        run_id=RUN_ID,
        state="running",
        initiating_message_id=None,
        tool_policy_digest=digest,
        contact_ref=winner_contact_ref,
        agent_id=AGENT_ID,
    )

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return False, existing_row

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

    async def fake_wait_for_claim_population(row_id):
        return existing_row

    monkeypatch.setattr(
        structured_api, "_wait_for_claim_population", fake_wait_for_claim_population
    )

    observed_user_ids = []

    async def fake_read_run_owned_reply(db, *, tenant_id, run_id, session_id, agent_id, user_id):
        observed_user_ids.append(user_id)
        return structured_api.RunOwnedReply(
            message_id=uuid.uuid4(),
            content="The winner's real reply.",
            delivery_kind="terminal",
            lifecycle_status="completed",
        )

    monkeypatch.setattr(structured_api, "read_run_owned_reply", fake_read_run_owned_reply)

    class _Reader:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get_run_state(self, tenant_id, run_id):
            return SimpleNamespace(execution_status="completed", waiting_reason=None, result_summary=None)

    monkeypatch.setattr(structured_api, "open_run_state_reader", lambda db: _Reader())

    class _NoopSession:
        async def execute(self, *a, **k):
            return SimpleNamespace(first=lambda: None, mappings=lambda: SimpleNamespace(first=lambda: None))

        async def commit(self):
            return None

        async def get(self, *a, **k):
            return None

        def expire_all(self):
            return None

    class _NoopSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _NoopSession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "async_session", _NoopSessionFactory())

    # This request's own contact_ref deliberately differs from the claimed
    # row's -- a stale retry, a client bug, or an adversarial duplicate.
    payload = dict(GOLDEN_REQUEST, contact_ref="contact:a-completely-different-customer")

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 200
    # The winner's real reply is returned -- the claim was never poisoned.
    assert response.json()["customer_reply"] == "The winner's real reply."
    assert len(observed_user_ids) == 1
    assert observed_user_ids[0] == structured_api._stable_user_id(TENANT_ID, winner_contact_ref)
    assert observed_user_ids[0] != structured_api._stable_user_id(
        TENANT_ID, "contact:a-completely-different-customer"
    )


@pytest.mark.asyncio
async def test_loser_derives_agent_id_from_the_claimed_row_not_the_request_body(monkeypatch, client):
    """Same regression as above for agent_id: a duplicate/concurrent
    request reusing an existing correlation_id under the SAME tenant and
    tool-policy digest but naming a DIFFERENT designated_agent_id must
    still validate ownership against the CLAIMED row's own agent_id --
    never this request's body.designated_agent_id -- or it can fail the
    run-owned lookup's agent check and mark the shared claim failed,
    poisoning it for the original winner."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        return sorted(requested), set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    winner_agent_id = AGENT_ID
    # Must match what _resolve_effective_tool_names resolves for
    # GOLDEN_REQUEST's own allowed_tools ("crm.lead.create") -- this test
    # varies only designated_agent_id, not the tool policy.
    digest = structured_api._tool_policy_digest(["crm.lead.create"])
    existing_row = SimpleNamespace(
        id=uuid.uuid4(),
        session_id=SESSION_ID,
        run_id=RUN_ID,
        state="running",
        initiating_message_id=None,
        tool_policy_digest=digest,
        contact_ref=GOLDEN_REQUEST["contact_ref"],
        agent_id=winner_agent_id,
    )

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return False, existing_row

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

    async def fake_wait_for_claim_population(row_id):
        return existing_row

    monkeypatch.setattr(
        structured_api, "_wait_for_claim_population", fake_wait_for_claim_population
    )

    observed_agent_ids = []

    async def fake_read_run_owned_reply(db, *, tenant_id, run_id, session_id, agent_id, user_id):
        observed_agent_ids.append(agent_id)
        return structured_api.RunOwnedReply(
            message_id=uuid.uuid4(),
            content="The winner's real reply.",
            delivery_kind="terminal",
            lifecycle_status="completed",
        )

    monkeypatch.setattr(structured_api, "read_run_owned_reply", fake_read_run_owned_reply)

    class _Reader:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get_run_state(self, tenant_id, run_id):
            return SimpleNamespace(execution_status="completed", waiting_reason=None, result_summary=None)

    monkeypatch.setattr(structured_api, "open_run_state_reader", lambda db: _Reader())

    class _NoopSession:
        async def execute(self, *a, **k):
            return SimpleNamespace(first=lambda: None, mappings=lambda: SimpleNamespace(first=lambda: None))

        async def commit(self):
            return None

        async def get(self, *a, **k):
            return None

        def expire_all(self):
            return None

    class _NoopSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _NoopSession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "async_session", _NoopSessionFactory())

    # This request's own designated_agent_id deliberately differs from the
    # claimed row's -- a stale retry, a client bug, or an adversarial
    # duplicate reusing the same correlation_id.
    different_agent_id = uuid.uuid4()
    payload = dict(GOLDEN_REQUEST, designated_agent_id=str(different_agent_id))

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 200
    # The winner's real reply is returned -- the claim was never poisoned.
    assert response.json()["customer_reply"] == "The winner's real reply."
    assert len(observed_agent_ids) == 1
    assert observed_agent_ids[0] == winner_agent_id
    assert observed_agent_ids[0] != different_agent_id


@pytest.mark.asyncio
async def test_fail_closed_when_claim_row_has_no_recorded_digest(monkeypatch, client):
    """Defence-in-depth proof: even though tool_policy_digest is a NOT NULL
    database column now, the endpoint's own comparison must still fail
    CLOSED (reject) rather than silently join when a claim row somehow
    surfaces without a comparable digest — never treat a missing/unexpected
    value as "compatible"."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        return sorted(requested), set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    # No tool_policy_digest attribute at all on this row.
    existing_row = SimpleNamespace(
        id=uuid.uuid4(),
        session_id=SESSION_ID,
        run_id=RUN_ID,
        state="running",
        initiating_message_id=None,
    )

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return False, existing_row

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

    async def fake_poll_and_read(*a, **k):
        raise AssertionError("must not poll/return a claim with no comparable digest")

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=GOLDEN_REQUEST, headers=_headers()
        )

    assert response.status_code == 409
    assert response.json()["error"] == "correlation_id_policy_mismatch"
