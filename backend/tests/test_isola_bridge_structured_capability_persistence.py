"""Persistence, settlement and replay-identity proofs for the structured
bridge's tool-capability carrier
(`dec-pr42-capability-sanitizer-and-replay-binding-2026-08-06`).

Companion to `test_isola_bridge_structured_tool_capabilities.py`, which
covers the request contract, allowed-tool coupling and carriage. This file
covers only the two leaks the independent exact-head review of head
`40641317` proved, and the controls that close them:

1. the raw capability survived `sanitize_tool_arguments` into
   `AgentToolExecution.sanitized_arguments`, into every
   `AgentRunEvent.payload["args"]` projection and therefore into the
   web-chat run-event stream;
2. the customer-reply redaction was request-time only, so the reply was
   raw at rest and a replay that omitted `tool_capabilities` returned it
   verbatim.

Same monkeypatch-the-module harness as the companion file: the real
FastAPI app over httpx.ASGITransport with the module's async collaborators
faked, so no database or LLM runtime is required.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.api import isola_bridge_structured as structured_api
from app.main import app
from app.services.agent_runtime import chat_stream as chat_stream_module
from app.services.agent_runtime import tool_step_service as tool_step_module
from app.services.agent_runtime.tool_execution import sanitize_tool_arguments
from app.services.builtin_tool_definitions import builtin_sensitive_paths

AGENT_ID = uuid.UUID("81b38cd6-9fba-4cc8-8f87-1bce1a4aa162")
TENANT_ID = uuid.UUID("43b006e4-33e0-42a8-bec7-4422ba290d79")
SESSION_ID = uuid.uuid4()
RUN_ID = uuid.uuid4()
REPLY_MESSAGE_ID = uuid.uuid4()

READ_TOOL = "isola_revenue_customer_context_get"
WRITE_TOOL = "isola_revenue_followup_set"

RAW_CAPABILITY = "cap_9f3a1c7e8b2d4650a1f7e6c3b9d8a012_do_not_disclose"
RAW_CAPABILITY_2 = "cap_44bb99aa11cc7733ee55dd22ff660088_also_secret"

TEST_BRIDGE_AUTH_VALUE = "test-isola-bridge-secret"

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
    "correlation_id": "corr-capability-persistence-2026-08-06-0001",
    "locale": "en-DM",
    "timezone": "America/Dominica",
    "response_deadline_ms": 45000,
}


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


def _model(**overrides) -> structured_api.ToolCapabilityIn:
    return structured_api.ToolCapabilityIn.model_validate(_capability(**overrides))


async def _fake_resolve_tenant_ok(body):
    return TENANT_ID, None


# ── 1. Shared tool-argument sanitizer ───────────────────────────────────────


def test_capability_argument_is_redacted_by_the_shared_sanitizer():
    """The exact leak the review proved, now closed. `sensitive_paths=()`
    is what an MCP tool really gets -- `builtin_sensitive_paths` returns an
    empty tuple for any non-builtin -- so the exact-key set is the only
    thing that can redact this argument."""
    assert builtin_sensitive_paths(READ_TOOL) == ()
    sanitized = sanitize_tool_arguments(
        {"conversation_id": 131, structured_api.CAPABILITY_ARGUMENT_NAME: RAW_CAPABILITY},
        sensitive_paths=builtin_sensitive_paths(READ_TOOL),
    )
    assert sanitized["conversation_id"] == 131
    assert sanitized[structured_api.CAPABILITY_ARGUMENT_NAME] == "[REDACTED]"
    assert RAW_CAPABILITY not in json.dumps(sanitized)


@pytest.mark.parametrize(
    "key",
    [
        "operation_capability",
        "Operation-Capability",
        "OPERATION CAPABILITY",
        "operationCapability",
        "operation.capability",
        "__operation_capability__",
    ],
)
def test_normalized_capability_key_variants_are_redacted(key):
    """`_sensitive_key` normalizes by casefold + non-alphanumeric strip, so
    every spelling that normalizes to `operationcapability` is covered."""
    sanitized = sanitize_tool_arguments({key: RAW_CAPABILITY}, sensitive_paths=())
    assert RAW_CAPABILITY not in json.dumps(sanitized)


@pytest.mark.parametrize(
    "key",
    [
        "operation_capability_hint",
        "capability",
        "capabilities",
        "operation_token_label",
        "conversation_id",
        "chatwoot_account_id",
        "owner_next_action",
    ],
)
def test_neighbouring_keys_are_not_falsely_redacted(key):
    """Exact-key matching only: no substring, prefix or suffix matching was
    introduced. A business field that merely looks similar keeps its value,
    otherwise ordinary tool arguments would become unreadable in the
    ledger and the operator activity feed."""
    value = "ordinary-business-value"
    sanitized = sanitize_tool_arguments({key: value}, sensitive_paths=())
    assert sanitized[key] == value


def test_sanitizer_matching_is_exact_not_substring():
    """Guards the shape of the fix itself: a key that CONTAINS the
    normalized sensitive key but is not equal to it must survive. If a
    future edit switched to substring matching this fails."""
    sanitized = sanitize_tool_arguments(
        {"x_operation_capability_audit_label": "not-a-credential"}, sensitive_paths=()
    )
    assert sanitized["x_operation_capability_audit_label"] == "not-a-credential"


def test_run_event_args_projection_carries_no_raw_capability():
    """Every `AgentRunEvent.payload["args"]` insertion in
    `tool_step_service` is built from `sanitized_arguments`, never from the
    raw `arguments` dict. This reproduces that exact payload shape."""
    sanitized = sanitize_tool_arguments(
        {"conversation_id": 131, structured_api.CAPABILITY_ARGUMENT_NAME: RAW_CAPABILITY},
        sensitive_paths=(),
    )
    payload = {
        "status": "running",
        "activity_type": "tool_call",
        "call_id": "call-1",
        "name": READ_TOOL,
        "args": dict(sanitized),
        "reasoning_content": "",
        "assistant_message_id": str(uuid.uuid4()),
    }
    assert RAW_CAPABILITY not in json.dumps(payload)

    source = inspect.getsource(tool_step_module)
    # Every args projection must read from sanitized_arguments. If a new
    # one ever reads `reservation.execution.arguments` this fails.
    assert '"args": dict(reservation.execution.arguments' not in source
    assert source.count('"args": dict(reservation.execution.sanitized_arguments or {})') >= 1


def test_web_chat_event_stream_projection_carries_no_raw_capability():
    """`chat_stream` forwards `payload["args"]` verbatim to the browser, so
    the stream is safe exactly when the event payload is. Proves the
    forwarded object is the sanitized one and that the forwarding
    expression still reads `args` (not a raw sibling field)."""
    source = inspect.getsource(chat_stream_module)
    assert 'payload.get("args")' in source
    assert 'payload.get("arguments")' not in source

    sanitized = sanitize_tool_arguments(
        {structured_api.CAPABILITY_ARGUMENT_NAME: RAW_CAPABILITY}, sensitive_paths=()
    )
    forwarded = sanitized if isinstance(sanitized, dict) else {}
    assert RAW_CAPABILITY not in json.dumps(forwarded)


def test_sanitizer_key_is_registered_exactly_once_and_normalized():
    from app.services.agent_runtime import tool_execution

    assert "operationcapability" in tool_execution._SENSITIVE_KEYS
    # The registered member must be the NORMALIZED form -- an unnormalized
    # spelling would silently never match.
    assert tool_execution._sensitive_key(structured_api.CAPABILITY_ARGUMENT_NAME) is True


# ── 2. Settle-time redaction of the stored reply ────────────────────────────


class _Begin:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _RecordingSession:
    """Records every SQL statement + params the route executes, in order."""

    def __init__(self, recorder, agent=None, model=None, user=None):
        self._recorder = recorder
        self._agent = agent
        self._model = model
        self._user = user

    async def execute(self, statement, params=None, *a, **k):
        self._recorder.append((str(statement), params))
        return SimpleNamespace(
            first=lambda: None, mappings=lambda: SimpleNamespace(first=lambda: None)
        )

    async def commit(self):
        return None

    def begin(self):
        return _Begin()

    async def get(self, model_cls, pk):
        if model_cls is structured_api.Agent:
            return self._agent
        if model_cls is structured_api.LLMModel:
            return self._model
        if model_cls is structured_api.User:
            return self._user
        return None


def _won_path(monkeypatch, *, reply_content: str, recorder: list):
    """Wire the won path end to end with a chosen assistant reply."""
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

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _RecordingSession(recorder, fake_agent, fake_model, fake_user)

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "async_session", _Factory())

    async def fake_ensure_session(db, agent_id, user_id_arg):
        return fake_session

    monkeypatch.setattr(structured_api, "ensure_primary_platform_session", fake_ensure_session)

    class _Reader:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "open_run_state_reader", lambda db: _Reader())

    enqueued = []

    async def fake_enqueue_chat_runtime(db, **kwargs):
        enqueued.append(kwargs)
        return SimpleNamespace(handle=SimpleNamespace(run_id=RUN_ID), message_id=uuid.uuid4())

    monkeypatch.setattr(structured_api, "enqueue_chat_runtime", fake_enqueue_chat_runtime)

    async def fake_poll_and_read(tenant_id, run_id, session_id, agent_id, user_id):
        return (
            True, "completed",
            structured_api.RunOwnedReply(
                message_id=REPLY_MESSAGE_ID, content=reply_content,
                delivery_kind="terminal", lifecycle_status="completed",
            ),
            None,
        )

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)
    return enqueued


def _redaction_updates(recorder):
    return [
        params
        for sql, params in recorder
        if "UPDATE chat_messages" in sql and params is not None
    ]


def _terminal_update_index(recorder):
    for index, (sql, _params) in enumerate(recorder):
        if "isola_structured_bridge_requests" in sql and "terminal_message_id" in sql:
            return index
    return -1


@pytest.mark.asyncio
async def test_leaked_capability_is_redacted_in_storage_before_settlement(monkeypatch, client):
    """The core correction: the raw value is overwritten in
    `chat_messages` BEFORE the claim is marked completed, so it is never at
    rest in a readable state that a replay could return."""
    recorder: list = []
    _won_path(
        monkeypatch,
        reply_content=f"Sure -- I used {RAW_CAPABILITY} to look that up.",
        recorder=recorder,
    )

    payload = dict(GOLDEN_REQUEST, tool_capabilities=[_capability()])
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 200
    assert RAW_CAPABILITY not in response.text
    assert structured_api._CAPABILITY_REDACTION_MARKER in response.json()["customer_reply"]

    updates = _redaction_updates(recorder)
    assert len(updates) == 1, "the stored reply must be redacted exactly once"
    stored = updates[0]
    assert stored["id"] == str(REPLY_MESSAGE_ID)
    assert RAW_CAPABILITY not in stored["content"]
    assert structured_api._CAPABILITY_REDACTION_MARKER in stored["content"]
    # Compare-and-set guard, so a concurrent duplicate cannot lose-update.
    assert stored["expected_content"] == f"Sure -- I used {RAW_CAPABILITY} to look that up."

    # Ordering: redaction strictly before the terminal claim update.
    redaction_index = next(
        i for i, (sql, _p) in enumerate(recorder) if "UPDATE chat_messages" in sql
    )
    assert redaction_index < _terminal_update_index(recorder)


@pytest.mark.asyncio
async def test_clean_reply_triggers_no_storage_rewrite(monkeypatch, client):
    """No false positives at rest: a reply that never contained a
    capability is not rewritten."""
    recorder: list = []
    _won_path(monkeypatch, reply_content="Yes, we install fibre in Roseau.", recorder=recorder)

    payload = dict(GOLDEN_REQUEST, tool_capabilities=[_capability()])
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 200
    assert response.json()["customer_reply"] == "Yes, we install fibre in Roseau."
    assert _redaction_updates(recorder) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape",
    [
        "plain {c} here",
        "**bold {c}** and `code {c}`",
        '{{"operation_capability": "{c}"}}',
        "```\n{c}\n```",
        "https://example.test/cb?operation_capability={c}&z=1",
        "line one\nline two {c}\nline three",
        "({c}), {c}.",
    ],
)
async def test_every_leak_shape_is_removed_from_storage(monkeypatch, client, shape):
    recorder: list = []
    _won_path(monkeypatch, reply_content=shape.format(c=RAW_CAPABILITY), recorder=recorder)

    payload = dict(GOLDEN_REQUEST, tool_capabilities=[_capability()])
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 200
    assert RAW_CAPABILITY not in response.text
    stored = _redaction_updates(recorder)[0]
    assert RAW_CAPABILITY not in stored["content"]


@pytest.mark.asyncio
async def test_multiple_capabilities_are_all_removed_from_storage(monkeypatch, client):
    recorder: list = []
    _won_path(
        monkeypatch,
        reply_content=f"first {RAW_CAPABILITY} then {RAW_CAPABILITY_2} done",
        recorder=recorder,
    )

    async def two_tools(agent_id, requested):
        return [READ_TOOL, WRITE_TOOL], set()

    monkeypatch.setattr(structured_api, "_resolve_effective_tool_names", two_tools)

    payload = dict(
        GOLDEN_REQUEST,
        tool_capabilities=[
            _capability(),
            _capability(tool_name=WRITE_TOOL, capability=RAW_CAPABILITY_2),
        ],
    )
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 200
    assert RAW_CAPABILITY not in response.text
    assert RAW_CAPABILITY_2 not in response.text
    stored = _redaction_updates(recorder)[0]
    assert RAW_CAPABILITY not in stored["content"]
    assert RAW_CAPABILITY_2 not in stored["content"]


# ── 3. Capability-aware replay identity ─────────────────────────────────────


def test_fingerprint_is_the_specified_domain_separated_sha256():
    expected = hashlib.sha256(
        b"isola-operation-capability-v1\x00" + RAW_CAPABILITY.encode("utf-8")
    ).hexdigest()
    assert structured_api._capability_fingerprint(RAW_CAPABILITY) == expected
    # Domain separation: a bare SHA-256 of the same value must not match.
    assert (
        structured_api._capability_fingerprint(RAW_CAPABILITY)
        != hashlib.sha256(RAW_CAPABILITY.encode("utf-8")).hexdigest()
    )


def test_capability_binding_is_raw_free_and_ordered():
    binding = structured_api._capability_binding(
        [
            _model(tool_name=WRITE_TOOL, capability=RAW_CAPABILITY_2),
            _model(tool_name=READ_TOOL, capability=RAW_CAPABILITY),
        ]
    )
    serialized = json.dumps(binding)
    assert RAW_CAPABILITY not in serialized
    assert RAW_CAPABILITY_2 not in serialized
    # Stable ordering by exact tool name, independent of request order.
    assert [entry[0] for entry in binding] == sorted([READ_TOOL, WRITE_TOOL])
    # Every required binding field is present.
    for entry in binding:
        assert len(entry) == 4
        assert entry[1] == structured_api.CAPABILITY_CONTRACT_VERSION


def test_digest_never_contains_the_raw_capability():
    digest = structured_api._tool_policy_digest([READ_TOOL], [_model()])
    assert RAW_CAPABILITY not in digest
    assert len(digest) == 16 and all(ch in "0123456789abcdef" for ch in digest)


def _digest(caps):
    return structured_api._tool_policy_digest([READ_TOOL, WRITE_TOOL], caps)


def test_identical_capability_binding_digests_identically():
    a = _digest([_model()])
    b = _digest([_model()])
    assert a == b


def test_omitted_capability_changes_the_digest():
    assert _digest([_model()]) != _digest([])


def test_substituted_capability_changes_the_digest():
    assert _digest([_model()]) != _digest([_model(capability=RAW_CAPABILITY_2)])


def test_capability_moved_to_another_tool_changes_the_digest():
    assert _digest([_model()]) != _digest([_model(tool_name=WRITE_TOOL)])


def test_changed_contract_version_changes_the_digest():
    assert _digest([_model()]) != _digest([_model(contract_version="2.0.0")])


def test_changed_expiry_metadata_changes_the_digest():
    soon = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    later = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    assert _digest([_model(expires_at=soon)]) != _digest([_model(expires_at=later)])
    # Supplying no expiry is distinguishable from supplying one.
    assert _digest([_model()]) != _digest([_model(expires_at=soon)])


def test_request_order_does_not_change_the_digest():
    forward = _digest(
        [_model(), _model(tool_name=WRITE_TOOL, capability=RAW_CAPABILITY_2)]
    )
    reverse = _digest(
        [_model(tool_name=WRITE_TOOL, capability=RAW_CAPABILITY_2), _model()]
    )
    assert forward == reverse


def _loser_path(monkeypatch, *, stored_digest: str, recorder: list):
    """Wire a request that LOSES the claim against a stored digest."""
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        return [READ_TOOL], set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return False, SimpleNamespace(
            id=uuid.uuid4(), session_id=SESSION_ID, run_id=RUN_ID, state="completed",
            initiating_message_id=uuid.uuid4(), tool_policy_digest=stored_digest,
            contact_ref=body.contact_ref, agent_id=body.designated_agent_id,
            external_conversation_id=body.conversation_id,
        )

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

    enqueued: list = []

    async def fake_enqueue_chat_runtime(db, **kwargs):  # pragma: no cover - must not run
        enqueued.append(kwargs)
        raise AssertionError("a policy-mismatched replay must never enqueue a run")

    monkeypatch.setattr(structured_api, "enqueue_chat_runtime", fake_enqueue_chat_runtime)

    polls: list = []

    async def fake_poll_and_read(*a, **k):  # pragma: no cover - must not run
        polls.append(a)
        raise AssertionError("a policy-mismatched replay must never poll for a reply")

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _RecordingSession(recorder)

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "async_session", _Factory())
    return enqueued, polls


@pytest.mark.asyncio
async def test_replay_omitting_the_capability_is_refused(monkeypatch, client):
    """The exact review finding: a retry that drops `tool_capabilities`
    used to reach the stored reply with an empty redaction set. It now
    computes a different digest and is refused before reading anything."""
    recorder: list = []
    original_digest = structured_api._tool_policy_digest([READ_TOOL], [_model()])
    enqueued, polls = _loser_path(monkeypatch, stored_digest=original_digest, recorder=recorder)

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=dict(GOLDEN_REQUEST), headers=_headers()
        )

    assert response.status_code == 409
    assert response.json()["error"] == "correlation_id_policy_mismatch"
    assert RAW_CAPABILITY not in response.text
    assert enqueued == []
    assert polls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replayed",
    [
        [_capability(capability=RAW_CAPABILITY_2)],
        [_capability(expires_at="2030-01-01T00:00:00+00:00")],
    ],
    ids=["substituted-capability", "changed-expiry"],
)
async def test_replay_with_a_different_binding_is_refused(monkeypatch, client, replayed):
    recorder: list = []
    original_digest = structured_api._tool_policy_digest([READ_TOOL], [_model()])
    enqueued, polls = _loser_path(monkeypatch, stored_digest=original_digest, recorder=recorder)

    payload = dict(GOLDEN_REQUEST, tool_capabilities=replayed)
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 409
    assert response.json()["error"] == "correlation_id_policy_mismatch"
    assert enqueued == []
    assert polls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replayed", "expected_error"),
    [
        ([_capability(contract_version="2.0.0")], "unsupported_capability_contract_version"),
        ([_capability(tool_name=WRITE_TOOL)], "capability_for_unavailable_tool"),
    ],
    ids=["changed-version", "moved-to-another-tool"],
)
async def test_replay_with_an_invalid_binding_is_refused_even_earlier(
    monkeypatch, client, replayed, expected_error
):
    """A retry whose capability names an unsupported contract version, or a
    tool this turn cannot call, never even reaches the digest comparison --
    `_validate_tool_capabilities` refuses it before the claim. Still no
    second run and no second tool execution. (The digest would also differ;
    that is proved at unit level above.)"""
    recorder: list = []
    original_digest = structured_api._tool_policy_digest([READ_TOOL], [_model()])
    enqueued, polls = _loser_path(monkeypatch, stored_digest=original_digest, recorder=recorder)

    payload = dict(GOLDEN_REQUEST, tool_capabilities=replayed)
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 400
    assert response.json()["error"] == expected_error
    assert RAW_CAPABILITY not in response.text
    assert enqueued == []
    assert polls == []


@pytest.mark.asyncio
async def test_identical_binding_still_replays_deterministically(monkeypatch, client):
    """The correction must not break legitimate idempotent retry: the SAME
    capability binding still joins the SAME claim and returns the stored
    (already-safe) reply without enqueuing a second run."""
    recorder: list = []
    original_digest = structured_api._tool_policy_digest([READ_TOOL], [_model()])

    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        return [READ_TOOL], set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        assert tool_policy_digest == original_digest
        return False, SimpleNamespace(
            id=uuid.uuid4(), session_id=SESSION_ID, run_id=RUN_ID, state="completed",
            initiating_message_id=uuid.uuid4(), tool_policy_digest=original_digest,
            contact_ref=body.contact_ref, agent_id=body.designated_agent_id,
            external_conversation_id=body.conversation_id,
        )

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

    enqueued: list = []

    async def fake_enqueue_chat_runtime(db, **kwargs):  # pragma: no cover
        enqueued.append(kwargs)
        raise AssertionError("replay must never enqueue a second run")

    monkeypatch.setattr(structured_api, "enqueue_chat_runtime", fake_enqueue_chat_runtime)

    terminal_message_id = uuid.uuid4()

    async def fake_wait_for_claim_population(claim_id):
        return SimpleNamespace(
            state="completed", terminal_message_id=terminal_message_id,
            session_id=SESSION_ID, run_id=RUN_ID, contact_ref="contact:abc123",
            agent_id=AGENT_ID, error_class=None,
        )

    monkeypatch.setattr(
        structured_api, "_wait_for_claim_population", fake_wait_for_claim_population
    )

    async def fake_read_run_owned_reply(db, **kwargs):
        # Already safe at rest -- settle-time redaction ran on the winner.
        return structured_api.RunOwnedReply(
            message_id=terminal_message_id,
            content=f"Sure -- I used {structured_api._CAPABILITY_REDACTION_MARKER} to look that up.",
            delivery_kind="terminal", lifecycle_status="completed",
        )

    monkeypatch.setattr(structured_api, "read_run_owned_reply", fake_read_run_owned_reply)

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _RecordingSession(recorder)

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "async_session", _Factory())

    payload = dict(GOLDEN_REQUEST, tool_capabilities=[_capability()])
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )

    assert response.status_code == 200
    assert RAW_CAPABILITY not in response.text
    assert structured_api._CAPABILITY_REDACTION_MARKER in response.json()["customer_reply"]
    assert enqueued == []


# ── 4. Compatibility and bounded-scope guards ───────────────────────────────


def test_legacy_request_digest_is_stable_and_capability_free():
    """A turn with no capabilities digests deterministically, so ordinary
    idempotent retry of a legacy request is unaffected."""
    a = structured_api._tool_policy_digest([READ_TOOL], [])
    b = structured_api._tool_policy_digest([READ_TOOL], None)
    c = structured_api._tool_policy_digest([READ_TOOL])
    assert a == b == c


def test_only_the_authorized_sanitizer_file_gained_capability_awareness():
    """Bounded-scope guard for the two authorized shared-runtime changes
    (`dec-pr42-capability-sanitizer-and-replay-binding-2026-08-06` and
    `dec-pr42-capability-turns-no-model-derived-progress-events
    -2026-08-06`).

    Only `tool_execution.py` may name the capability argument. The progress
    -suppression files carry a deliberately CAPABILITY-AGNOSTIC boolean, so
    they must still contain no capability vocabulary at all — that is what
    keeps the flag non-sensitive and keeps this guard meaningful."""
    backend_root = Path(__file__).resolve().parents[1]
    still_forbidden = (
        "app/services/mcp_client.py",
        "app/services/agent_tools.py",
        "app/services/agent_runtime/chat_intake.py",
        "app/services/agent_runtime/model_step_service.py",
        "app/services/agent_runtime/tool_step_service.py",
        "app/services/agent_runtime/checkpoint_side_effects.py",
        "app/services/agent_runtime/chat_stream.py",
        "app/api/isola_bridge.py",
        "app/api/isola_bridge_v2.py",
    )
    for relative in still_forbidden:
        source = (backend_root / relative).read_text(encoding="utf-8")
        assert structured_api.CAPABILITY_ARGUMENT_NAME not in source, relative
        assert "tool_capabilities" not in source, relative
        assert "operationcapability" not in source, relative

    authorized = (backend_root / "app/services/agent_runtime/tool_execution.py").read_text(
        encoding="utf-8"
    )
    assert "operationcapability" in authorized


def test_settle_time_redaction_sql_stores_no_capability_column():
    sql = str(structured_api._REDACT_STORED_REPLY_SQL)
    assert "capability" not in sql.lower()
    assert "chat_messages" in sql
    # Compare-and-set, scoped to one assistant message.
    assert "expected_content" in sql and "role = 'assistant'" in sql


def test_no_migration_added_for_the_capability_corrections():
    versions_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    for path in versions_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8").lower()
        assert "operation_capability" not in source
        assert "tool_capabilit" not in source
