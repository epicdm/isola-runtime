"""Tests for the additive tool-capability carrier on the structured bridge
(`dec-revenue-mcp-turn-capability-no-clawith-core-fork-2026-08-06`).

Follows the same monkeypatch-the-module pattern as
`test_isola_bridge_structured.py`: the real FastAPI app is exercised via
httpx.ASGITransport, and the module's own async collaborators are replaced
with fakes so no real database or LLM runtime is required. This file is
scoped ONLY to the `tool_capabilities` carrier — golden-path structured
bridge behavior (idempotency, claim isolation, run-owned reply) is already
covered by `test_isola_bridge_structured.py` and is not re-proved here.

Kept as a dedicated file (not appended to the 1671-line existing suite) to
match this repo's established pattern of one file per concern
(`test_isola_structured_bridge_claim_isolation.py`,
`test_isola_structured_bridge_concurrency.py`, etc).
"""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.api import isola_bridge_structured as structured_api
from app.main import app

AGENT_ID = uuid.UUID("81b38cd6-9fba-4cc8-8f87-1bce1a4aa162")
TENANT_ID = uuid.UUID("43b006e4-33e0-42a8-bec7-4422ba290d79")
SESSION_ID = uuid.uuid4()
RUN_ID = uuid.uuid4()

READ_TOOL = "isola_revenue_customer_context_get"
WRITE_TOOL = "isola_revenue_followup_set"

RAW_CAPABILITY = "cap_9f3a1c7e8b2d4650a1f7e6c3b9d8a012_do_not_disclose"

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
        {
            "name": READ_TOOL,
            "description": "Read governed Revenue context",
            "arguments": ["chatwoot_account_id", "inbox_id", "conversation_id"],
            "mutating": False,
        }
    ],
    "ownership_state": "AI_OWNED",
    "correlation_id": "corr-capability-2026-08-06-0001",
    "locale": "en-DM",
    "timezone": "America/Dominica",
    "response_deadline_ms": 45000,
}

TEST_BRIDGE_AUTH_VALUE = "test-isola-bridge-secret"


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch):
    monkeypatch.setenv("ISOLA_BRIDGE_SECRET", TEST_BRIDGE_AUTH_VALUE)


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)

    async def _build():
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _build


def _headers():
    return {"X-Isola-Secret": TEST_BRIDGE_AUTH_VALUE}


def _capability(
    tool_name: str = READ_TOOL,
    capability: str = RAW_CAPABILITY,
    contract_version: str = structured_api.CAPABILITY_CONTRACT_VERSION,
    expires_at=None,
) -> dict:
    entry = {"tool_name": tool_name, "capability": capability, "contract_version": contract_version}
    if expires_at is not None:
        entry["expires_at"] = expires_at
    return entry


async def _fake_resolve_tenant_ok(body):
    return TENANT_ID, None


# ── Pure model / contract proofs (no HTTP) ──────────────────────────────────


def test_tool_capability_in_forbids_unknown_fields():
    """Identity non-authority: a caller-supplied agentRef/actorType/tenant
    has no field to enter through — extra="forbid" makes it a 422, not a
    silently-ignored key."""
    from pydantic import ValidationError

    for extra_field, value in (
        ("agentRef", "some-agent"),
        ("actorType", "owner"),
        ("tenant_id", str(TENANT_ID)),
        ("foundation_agent_id", str(uuid.uuid4())),
        ("clawith_tenant_id", "6572bd90"),
    ):
        payload = _capability()
        payload[extra_field] = value
        with pytest.raises(ValidationError):
            structured_api.ToolCapabilityIn.model_validate(payload)


def test_tool_capability_in_rejects_blank_capability():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        structured_api.ToolCapabilityIn.model_validate(_capability(capability=""))
    with pytest.raises(ValidationError):
        structured_api.ToolCapabilityIn.model_validate(_capability(capability="   "))


def test_tool_capability_in_rejects_malformed_capability_type():
    from pydantic import ValidationError

    payload = _capability()
    payload["capability"] = 12345
    with pytest.raises(ValidationError):
        structured_api.ToolCapabilityIn.model_validate(payload)


def test_tool_capability_in_accepts_a_well_formed_entry():
    body = structured_api.ToolCapabilityIn.model_validate(_capability())
    assert body.tool_name == READ_TOOL
    assert body.capability == RAW_CAPABILITY
    assert body.contract_version == structured_api.CAPABILITY_CONTRACT_VERSION


def test_structured_message_in_defaults_tool_capabilities_to_empty_list():
    """No capability survives as a default/global value: the field defaults
    to an empty list, never None, never inherited from anywhere else."""
    body = structured_api.StructuredBridgeMessageIn.model_validate(GOLDEN_REQUEST)
    assert body.tool_capabilities == []


def test_structured_message_in_bounds_tool_capabilities_count():
    from pydantic import ValidationError

    too_many = [_capability(tool_name=f"tool_{i}") for i in range(structured_api._MAX_TOOL_CAPABILITIES + 1)]
    payload = dict(GOLDEN_REQUEST, tool_capabilities=too_many)
    with pytest.raises(ValidationError):
        structured_api.StructuredBridgeMessageIn.model_validate(payload)


# ── _validate_tool_capabilities: pure proofs ────────────────────────────────


def test_validate_tool_capabilities_accepts_capability_for_effective_tool():
    caps = [structured_api.ToolCapabilityIn.model_validate(_capability())]
    result = structured_api._validate_tool_capabilities(frozenset({READ_TOOL}), caps)
    assert result is None


def test_validate_tool_capabilities_rejects_unavailable_tool():
    """Capability for a tool not in effective_tool_names — either never
    requested, or requested but not configured for the Agent (i.e. denied).
    Both collapse to the same effective-set membership check."""
    caps = [structured_api.ToolCapabilityIn.model_validate(_capability(tool_name=WRITE_TOOL))]
    result = structured_api._validate_tool_capabilities(frozenset({READ_TOOL}), caps)
    assert result is not None
    assert result.status_code == 400
    import json

    body = json.loads(result.body)
    assert body["error"] == "capability_for_unavailable_tool"
    assert body["detail"] == [WRITE_TOOL]
    # The raw capability value must never appear in a safe error body.
    assert RAW_CAPABILITY not in result.body.decode("utf-8")


def test_validate_tool_capabilities_read_capability_cannot_cover_write_tool():
    """Write tool remains disabled by default: even with a well-formed
    capability naming the write tool, if the write tool never made it into
    effective_tool_names (because it isn't assigned to the Agent), it is
    rejected exactly like any other unavailable tool -- a read capability
    can never substitute for write authority."""
    caps = [
        structured_api.ToolCapabilityIn.model_validate(_capability(tool_name=READ_TOOL)),
        structured_api.ToolCapabilityIn.model_validate(_capability(tool_name=WRITE_TOOL, capability="cap-write-x")),
    ]
    result = structured_api._validate_tool_capabilities(frozenset({READ_TOOL}), caps)
    assert result is not None
    import json

    assert json.loads(result.body)["detail"] == [WRITE_TOOL]


def test_validate_tool_capabilities_rejects_duplicate_tool_name():
    caps = [
        structured_api.ToolCapabilityIn.model_validate(_capability(capability="cap-1")),
        structured_api.ToolCapabilityIn.model_validate(_capability(capability="cap-2")),
    ]
    result = structured_api._validate_tool_capabilities(frozenset({READ_TOOL}), caps)
    assert result is not None
    import json

    body = json.loads(result.body)
    assert body["error"] == "duplicate_tool_capability"
    assert body["detail"] == [READ_TOOL]
    assert "cap-1" not in result.body.decode("utf-8")
    assert "cap-2" not in result.body.decode("utf-8")


def test_validate_tool_capabilities_rejects_contract_version_mismatch():
    caps = [structured_api.ToolCapabilityIn.model_validate(_capability(contract_version="0.9.0"))]
    result = structured_api._validate_tool_capabilities(frozenset({READ_TOOL}), caps)
    assert result is not None
    import json

    body = json.loads(result.body)
    assert body["error"] == "unsupported_capability_contract_version"
    assert body["supported_contract_version"] == structured_api.CAPABILITY_CONTRACT_VERSION


def test_validate_tool_capabilities_rejects_already_expired_capability():
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    caps = [structured_api.ToolCapabilityIn.model_validate(_capability(expires_at=past))]
    result = structured_api._validate_tool_capabilities(frozenset({READ_TOOL}), caps)
    assert result is not None
    import json

    assert json.loads(result.body)["error"] == "expired_tool_capability"


def test_validate_tool_capabilities_empty_list_is_a_noop():
    assert structured_api._validate_tool_capabilities(frozenset({READ_TOOL}), []) is None
    assert structured_api._validate_tool_capabilities(frozenset(), []) is None


def test_validate_tool_capabilities_takes_no_identity_parameters():
    """Structural proof of capability-as-operation-authority, not identity:
    the validator's signature has no tenant/agent/actor parameter through
    which caller-supplied identity could be consulted or injected."""
    params = set(inspect.signature(structured_api._validate_tool_capabilities).parameters)
    assert params == {"effective_tool_names", "capabilities"}


# ── _capability_directive: model-visibility + per-tool binding proofs ──────


def test_capability_directive_empty_for_no_capabilities():
    assert structured_api._capability_directive([]) == ""


def test_capability_directive_binds_each_capability_to_its_own_tool_only():
    """Capability for one tool cannot be attached to another: each line
    names exactly one tool and carries exactly its own value."""
    cap_a = structured_api.ToolCapabilityIn.model_validate(_capability(tool_name="tool_a", capability="cap-a-value"))
    cap_b = structured_api.ToolCapabilityIn.model_validate(_capability(tool_name="tool_b", capability="cap-b-value"))
    directive = structured_api._capability_directive([cap_a, cap_b])
    assert "cap-a-value" in directive
    assert "cap-b-value" in directive
    line_a = next(line for line in directive.splitlines() if "cap-a-value" in line)
    line_b = next(line for line in directive.splitlines() if "cap-b-value" in line)
    assert "tool_a" in line_a and "tool_b" not in line_a
    assert "tool_b" in line_b and "tool_a" not in line_b
    assert structured_api.CAPABILITY_ARGUMENT_NAME in directive


def test_capability_directive_instructs_non_disclosure():
    cap = structured_api.ToolCapabilityIn.model_validate(_capability())
    directive = structured_api._capability_directive([cap])
    lowered = directive.lower()
    assert "never repeat" in lowered or "never disclose" in lowered


# ── _redact_capabilities: non-LLM output control ────────────────────────────


def test_redact_capabilities_strips_raw_value_from_reply_text():
    cap = structured_api.ToolCapabilityIn.model_validate(_capability())
    leaked = f"Sure! Here is the value: {RAW_CAPABILITY} — hope that helps."
    redacted = structured_api._redact_capabilities(leaked, [cap])
    assert RAW_CAPABILITY not in redacted
    assert "[redacted-capability]" in redacted


def test_redact_capabilities_is_a_noop_without_capabilities():
    assert structured_api._redact_capabilities("hello", []) == "hello"


def test_redact_capabilities_passes_through_none():
    cap = structured_api.ToolCapabilityIn.model_validate(_capability())
    assert structured_api._redact_capabilities(None, [cap]) is None


def test_redact_capabilities_leaves_unrelated_text_untouched():
    cap = structured_api.ToolCapabilityIn.model_validate(_capability())
    assert structured_api._redact_capabilities("Yes, we install fibre in Roseau.", [cap]) == (
        "Yes, we install fibre in Roseau."
    )


# ── Endpoint-level proofs ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capability_for_unavailable_tool_rejected_before_claim(monkeypatch, client):
    """Contract validation, endpoint-level: rejected before the idempotency
    claim -- no durable row, no Run."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        return [], set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    claim_calls = []

    async def spy_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        claim_calls.append(correlation_id)
        raise AssertionError("must not claim when a capability names an unavailable tool")

    monkeypatch.setattr(structured_api, "_claim", spy_claim)

    payload = dict(GOLDEN_REQUEST, allowed_tools=[], tool_capabilities=[_capability()])
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "capability_for_unavailable_tool"
    assert RAW_CAPABILITY not in response.text
    assert claim_calls == []


@pytest.mark.asyncio
async def test_golden_request_without_tool_capabilities_is_unaffected(monkeypatch, client):
    """Compatibility: a request with no tool_capabilities field behaves
    exactly as before this carrier existed -- no directive text is added,
    no new rejection path is reachable."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        return sorted(requested), set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    fake_agent = SimpleNamespace(id=AGENT_ID, primary_model_id=uuid.uuid4(), fallback_model_id=None)
    fake_model = SimpleNamespace(id=fake_agent.primary_model_id)
    fake_user = SimpleNamespace(id=uuid.uuid4())
    fake_session = SimpleNamespace(id=SESSION_ID)

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return True, SimpleNamespace(
            id=uuid.uuid4(), session_id=None, run_id=None, state="accepted",
            initiating_message_id=None, tool_policy_digest=tool_policy_digest,
            contact_ref=body.contact_ref, agent_id=body.designated_agent_id,
            external_conversation_id=body.conversation_id,
        )

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

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

    monkeypatch.setattr(structured_api, "ensure_primary_platform_session", fake_ensure_session)

    class _Reader:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "open_run_state_reader", lambda db: _Reader())

    enqueue_calls = []

    async def fake_enqueue_chat_runtime(db, **kwargs):
        enqueue_calls.append(kwargs)
        return SimpleNamespace(handle=SimpleNamespace(run_id=RUN_ID), message_id=uuid.uuid4())

    monkeypatch.setattr(structured_api, "enqueue_chat_runtime", fake_enqueue_chat_runtime)

    async def fake_poll_and_read(tenant_id, run_id, session_id, agent_id, user_id):
        return (
            True, "completed",
            structured_api.RunOwnedReply(
                message_id=uuid.uuid4(), content="Yes, we install fibre in Roseau.",
                delivery_kind="terminal", lifecycle_status="completed",
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
    # No capability directive text leaked into the identity directive when
    # tool_capabilities is empty.
    assert "operation capability" not in enqueue_calls[0]["runtime_instruction"]
    assert response.json()["customer_reply"] == "Yes, we install fibre in Roseau."


@pytest.mark.asyncio
async def test_capability_reaches_runtime_instruction_on_the_won_path(monkeypatch, client):
    """End-to-end proof of carriage: the exact capability value reaches
    `enqueue_chat_runtime`'s `runtime_instruction` kwarg (the same accepted
    persisted-run-state channel the identity directive already uses) bound
    to its own tool name, and `allowed_tool_names` is unaffected by its
    presence -- a capability can never widen the effective tool set."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        assert agent_id == AGENT_ID
        return [READ_TOOL], set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    fake_agent = SimpleNamespace(id=AGENT_ID, primary_model_id=uuid.uuid4(), fallback_model_id=None)
    fake_model = SimpleNamespace(id=fake_agent.primary_model_id)
    fake_user = SimpleNamespace(id=uuid.uuid4())
    fake_session = SimpleNamespace(id=SESSION_ID)

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return True, SimpleNamespace(
            id=uuid.uuid4(), session_id=None, run_id=None, state="accepted",
            initiating_message_id=None, tool_policy_digest=tool_policy_digest,
            contact_ref=body.contact_ref, agent_id=body.designated_agent_id,
            external_conversation_id=body.conversation_id,
        )

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

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

    monkeypatch.setattr(structured_api, "ensure_primary_platform_session", fake_ensure_session)

    class _Reader:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "open_run_state_reader", lambda db: _Reader())

    enqueue_calls = []

    async def fake_enqueue_chat_runtime(db, **kwargs):
        enqueue_calls.append(kwargs)
        return SimpleNamespace(handle=SimpleNamespace(run_id=RUN_ID), message_id=uuid.uuid4())

    monkeypatch.setattr(structured_api, "enqueue_chat_runtime", fake_enqueue_chat_runtime)

    async def fake_poll_and_read(tenant_id, run_id, session_id, agent_id, user_id):
        return (
            True, "completed",
            structured_api.RunOwnedReply(
                message_id=uuid.uuid4(), content="Yes.",
                delivery_kind="terminal", lifecycle_status="completed",
            ),
            None,
        )

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)

    payload = dict(GOLDEN_REQUEST, tool_capabilities=[_capability()])
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 200
    assert len(enqueue_calls) == 1
    instruction = enqueue_calls[0]["runtime_instruction"]
    assert RAW_CAPABILITY in instruction
    assert READ_TOOL in instruction
    # Capability presence never widens the effective/allowed tool set.
    assert enqueue_calls[0]["allowed_tool_names"] == [READ_TOOL]
    # The raw value must not leak into the response envelope anywhere.
    assert RAW_CAPABILITY not in response.text


@pytest.mark.asyncio
async def test_raw_capability_redacted_from_settled_customer_reply(monkeypatch, client):
    """A model that disobeys the directive and echoes the raw capability
    into its reply is still caught: the non-LLM redaction control strips
    it before the envelope is returned."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        return [READ_TOOL], set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    fake_agent = SimpleNamespace(id=AGENT_ID, primary_model_id=uuid.uuid4(), fallback_model_id=None)
    fake_model = SimpleNamespace(id=fake_agent.primary_model_id)
    fake_user = SimpleNamespace(id=uuid.uuid4())
    fake_session = SimpleNamespace(id=SESSION_ID)

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return True, SimpleNamespace(
            id=uuid.uuid4(), session_id=None, run_id=None, state="accepted",
            initiating_message_id=None, tool_policy_digest=tool_policy_digest,
            contact_ref=body.contact_ref, agent_id=body.designated_agent_id,
            external_conversation_id=body.conversation_id,
        )

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

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

    monkeypatch.setattr(structured_api, "ensure_primary_platform_session", fake_ensure_session)

    class _Reader:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "open_run_state_reader", lambda db: _Reader())

    async def fake_enqueue_chat_runtime(db, **kwargs):
        return SimpleNamespace(handle=SimpleNamespace(run_id=RUN_ID), message_id=uuid.uuid4())

    monkeypatch.setattr(structured_api, "enqueue_chat_runtime", fake_enqueue_chat_runtime)

    async def fake_poll_and_read(tenant_id, run_id, session_id, agent_id, user_id):
        return (
            True, "completed",
            structured_api.RunOwnedReply(
                message_id=uuid.uuid4(),
                content=f"Here is your code: {RAW_CAPABILITY}",
                delivery_kind="terminal", lifecycle_status="completed",
            ),
            None,
        )

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)

    payload = dict(GOLDEN_REQUEST, tool_capabilities=[_capability()])
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 200
    assert RAW_CAPABILITY not in response.text
    assert "[redacted-capability]" in response.json()["customer_reply"]


@pytest.mark.asyncio
async def test_turn_a_capability_absent_from_turn_b(monkeypatch, client):
    """Turn isolation: two distinct correlation_ids (two distinct turns)
    each carrying their own capability never see each other's value, and
    the second call's instruction never contains the first's."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        return [READ_TOOL], set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    fake_agent = SimpleNamespace(id=AGENT_ID, primary_model_id=uuid.uuid4(), fallback_model_id=None)
    fake_model = SimpleNamespace(id=fake_agent.primary_model_id)
    fake_user = SimpleNamespace(id=uuid.uuid4())
    fake_session = SimpleNamespace(id=SESSION_ID)

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return True, SimpleNamespace(
            id=uuid.uuid4(), session_id=None, run_id=None, state="accepted",
            initiating_message_id=None, tool_policy_digest=tool_policy_digest,
            contact_ref=body.contact_ref, agent_id=body.designated_agent_id,
            external_conversation_id=body.conversation_id,
        )

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

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

    monkeypatch.setattr(structured_api, "ensure_primary_platform_session", fake_ensure_session)

    class _Reader:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "open_run_state_reader", lambda db: _Reader())

    enqueue_calls = []

    async def fake_enqueue_chat_runtime(db, **kwargs):
        enqueue_calls.append(kwargs)
        return SimpleNamespace(handle=SimpleNamespace(run_id=RUN_ID), message_id=uuid.uuid4())

    monkeypatch.setattr(structured_api, "enqueue_chat_runtime", fake_enqueue_chat_runtime)

    async def fake_poll_and_read(tenant_id, run_id, session_id, agent_id, user_id):
        return (
            True, "completed",
            structured_api.RunOwnedReply(
                message_id=uuid.uuid4(), content="ok",
                delivery_kind="terminal", lifecycle_status="completed",
            ),
            None,
        )

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)

    payload_a = dict(
        GOLDEN_REQUEST, correlation_id="corr-turn-a", tool_capabilities=[_capability(capability="cap-turn-a")]
    )
    payload_b = dict(
        GOLDEN_REQUEST, correlation_id="corr-turn-b", tool_capabilities=[_capability(capability="cap-turn-b")]
    )

    async with await client() as ac:
        await ac.post("/api/isola/bridge/structured/message", json=payload_a, headers=_headers())
        await ac.post("/api/isola/bridge/structured/message", json=payload_b, headers=_headers())

    assert len(enqueue_calls) == 2
    instruction_a = enqueue_calls[0]["runtime_instruction"]
    instruction_b = enqueue_calls[1]["runtime_instruction"]
    assert "cap-turn-a" in instruction_a and "cap-turn-b" not in instruction_a
    assert "cap-turn-b" in instruction_b and "cap-turn-a" not in instruction_b


# ── Compatibility / no-core-fork static proofs ──────────────────────────────

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_legacy_and_v2_bridges_have_no_tool_capability_extension():
    """Compatibility: the capability carrier is scoped entirely to the
    structured bridge -- the legacy and v2 bridges are untouched."""
    for relative in ("app/api/isola_bridge.py", "app/api/isola_bridge_v2.py"):
        source = (_BACKEND_ROOT / relative).read_text(encoding="utf-8")
        assert "tool_capabilities" not in source
        assert "ToolCapabilityIn" not in source
        assert structured_api.CAPABILITY_ARGUMENT_NAME not in source


def test_no_core_runtime_or_mcp_files_reference_the_capability_carrier():
    """Zero-core-fork proof: none of the forbidden core files gained any
    reference to this carrier's new names."""
    forbidden_paths = (
        "app/services/mcp_client.py",
        "app/services/agent_tools.py",
        "app/services/agent_runtime/chat_intake.py",
        "app/services/agent_runtime/model_step_service.py",
        "app/services/agent_runtime/tool_step_service.py",
    )
    for relative in forbidden_paths:
        source = (_BACKEND_ROOT / relative).read_text(encoding="utf-8")
        assert "tool_capabilities" not in source
        assert "ToolCapabilityIn" not in source
        assert "_validate_tool_capabilities" not in source
        assert structured_api.CAPABILITY_ARGUMENT_NAME not in source


def test_no_alembic_migration_added_for_the_capability_carrier():
    """No migration: the carrier persists nothing beyond the existing
    runtime_instruction snapshot channel -- no new table or column."""
    versions_dir = _BACKEND_ROOT / "alembic" / "versions"
    for path in versions_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "capability" not in source.lower() or "tool_capabilit" not in source.lower()


def test_claim_sql_never_references_capability_columns():
    """No raw capability in audit/event payload: the durable claim-table
    SQL this file already owns gained no capability-shaped column."""
    for stmt in (
        structured_api._CLAIM_SQL,
        structured_api._SELECT_BY_CORRELATION_SQL,
        structured_api._UPDATE_ENQUEUED_SQL,
        structured_api._UPDATE_TERMINAL_SQL,
    ):
        text = str(stmt)
        assert "capability" not in text.lower()


def test_module_source_has_no_logging_of_the_capability_directive():
    """No raw capability in normal logs: this module has no logger at all,
    so there is no call site that could log a directive or capability
    value. If a logger is ever added, this test forces the author to also
    prove it never logs `caller_directive` / `capability_directive` /
    `tool_capabilities`."""
    source = inspect.getsource(structured_api)
    assert "import logging" not in source
    assert "logger" not in source.lower()
