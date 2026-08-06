"""Model-derived progress-event suppression for capability-bearing runs
(`dec-pr42-capability-turns-no-model-derived-progress-events-2026-08-06`).

The raw operation capability IS model-visible under this carrier, so a
model may copy it into arbitrary assistant prose. Exact-value replacement
in free text is not a security boundary — a model can split, quote,
reformat or encode a credential. The control under test is therefore
structural: for a run that carries validated capabilities, model-derived
text is never copied into an event payload at all.

Three model-derived text fields exist across the two writers, and all
three are covered here:

* `thinking.content`            (checkpoint_side_effects, tool_step_service)
* `assistant_progress.content`  (checkpoint_side_effects, tool_step_service)
* `tool_call.reasoning_content` (checkpoint_side_effects, tool_step_service)

Every assertion below is "the leaked text is ABSENT", never "the exact
token was replaced" — the shapes are parametrized precisely to show that
the control does not depend on recognizing the value.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.api import isola_bridge_structured as structured_api
from app.main import app
from app.services.agent_runtime import checkpoint_side_effects as cse
from app.services.agent_runtime import tool_step_service as tss

AGENT_ID = uuid.UUID("81b38cd6-9fba-4cc8-8f87-1bce1a4aa162")
TENANT_ID = uuid.UUID("43b006e4-33e0-42a8-bec7-4422ba290d79")
SESSION_ID = uuid.uuid4()
RUN_ID = uuid.uuid4()

READ_TOOL = "isola_revenue_customer_context_get"
RAW_CAPABILITY = "cap_1a2b3c4d5e6f70819aabbccddeeff001_progress_probe"
RAW_CAPABILITY_2 = "cap_ff00ee11dd22cc33bb44aa5566778899_second_probe"

TEST_BRIDGE_AUTH_VALUE = "test-isola-bridge-secret"

# Every one of these is a way a model could reproduce the credential that a
# naive exact-string scrubber would miss. The suppression control does not
# look at the text at all, so all of them are covered by construction.
LEAK_SHAPES = [
    pytest.param(RAW_CAPABILITY, id="exact"),
    pytest.param(f"{RAW_CAPABILITY} at the start", id="leading"),
    pytest.param(f"ends with {RAW_CAPABILITY}", id="trailing"),
    pytest.param(f"{RAW_CAPABILITY} and again {RAW_CAPABILITY}", id="repeated"),
    pytest.param(f"{RAW_CAPABILITY} then {RAW_CAPABILITY_2}", id="two-capabilities"),
    pytest.param(f"**bold {RAW_CAPABILITY}** and `code {RAW_CAPABILITY}`", id="markdown"),
    pytest.param(f"```\n{RAW_CAPABILITY}\n```", id="code-block"),
    pytest.param(json.dumps({"operation_capability": RAW_CAPABILITY}), id="json-like"),
    pytest.param(f"https://x.test/cb?operation_capability={RAW_CAPABILITY}", id="url-query"),
    pytest.param("-".join(RAW_CAPABILITY), id="punctuation-between-characters"),
    pytest.param(" ".join(RAW_CAPABILITY), id="whitespace-between-characters"),
    pytest.param(f"{RAW_CAPABILITY[:20]}\n{RAW_CAPABILITY[20:]}", id="split-across-chunks"),
    pytest.param(f'"{RAW_CAPABILITY}"', id="quoted"),
    pytest.param(RAW_CAPABILITY.encode().hex(), id="hex-encoded"),
    pytest.param("Let me look up that customer's account now.", id="ordinary-prose"),
]


def _leaked(payloads: list[dict], probe: str) -> bool:
    """Did any emitted payload field carry the model's text?

    Compares against the raw field values, not a JSON dump, so text
    containing quotes or newlines is matched literally rather than against
    its JSON-escaped form.
    """
    return any(probe in value for payload in payloads for value in payload.values() if isinstance(value, str))


# ── checkpoint_side_effects writer ──────────────────────────────────────────


def _run_record():
    return SimpleNamespace(run_id=RUN_ID, tenant_id=TENANT_ID, agent_id=str(AGENT_ID))


def _checkpoint(*, model_text: str, suppress: bool | None):
    initial_input: dict = {"message_id": str(uuid.uuid4())}
    if suppress is not None:
        initial_input["suppress_model_progress"] = suppress
    state = {
        "messages": [
            {
                "role": "assistant",
                "id": "msg-1",
                "runtime_run_id": str(RUN_ID),
                "reasoning_content": model_text,
                "content": model_text,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": READ_TOOL,
                            "arguments": json.dumps(
                                {
                                    "conversation_id": 131,
                                    "operation_capability": RAW_CAPABILITY,
                                }
                            ),
                        },
                    }
                ],
            }
        ],
        "snapshots": SimpleNamespace(initial_input=initial_input),
    }
    return SimpleNamespace(checkpoint_id="ckpt-1", state=state)


def _checkpoint_payloads(*, model_text: str, suppress: bool | None) -> list[dict]:
    events, _calls = cse._runtime_observation_events(
        _run_record(), _checkpoint(model_text=model_text, suppress=suppress)
    )
    return [payload for _type, _summary, payload, _key, _extra in events]


@pytest.mark.parametrize("model_text", LEAK_SHAPES)
def test_checkpoint_capability_run_emits_no_model_derived_text(model_text):
    payloads = _checkpoint_payloads(model_text=model_text, suppress=True)
    assert not _leaked(payloads, model_text)
    kinds = {p.get("activity_type") for p in payloads}
    assert "thinking" not in kinds
    assert "assistant_progress" not in kinds
    for payload in payloads:
        assert payload.get("reasoning_content", "") == ""
    # The raw capability never appears anywhere, including through the
    # separately-sanitized tool arguments.
    assert RAW_CAPABILITY not in json.dumps(payloads)


@pytest.mark.parametrize("model_text", LEAK_SHAPES)
def test_checkpoint_ordinary_run_still_emits_progress(model_text):
    """No global disable: a run WITHOUT capabilities keeps its existing
    progress behaviour, payload shape and content."""
    payloads = _checkpoint_payloads(model_text=model_text, suppress=None)
    kinds = [p.get("activity_type") for p in payloads]
    assert "thinking" in kinds
    assert "assistant_progress" in kinds
    assert _leaked(payloads, model_text)


def test_checkpoint_tool_activity_survives_suppression():
    """Suppression removes model prose, not tool visibility."""
    payloads = _checkpoint_payloads(model_text="anything", suppress=True)
    tool_events = [p for p in payloads if p.get("activity_type") == "tool_call"]
    assert tool_events, "tool activity must remain visible"
    for payload in tool_events:
        assert payload["name"] == READ_TOOL
        assert payload["args"]["conversation_id"] == 131
        # Argument sanitization from the previous correction still holds.
        assert payload["args"]["operation_capability"] == "[REDACTED]"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), ("yes", True), (1, True), (None, True)],
)
def test_checkpoint_flag_reader_fails_closed(value, expected):
    state = {"snapshots": SimpleNamespace(initial_input={"suppress_model_progress": value})}
    assert cse._suppress_model_progress(state) is expected


def test_checkpoint_flag_absent_means_ordinary_run():
    state = {"snapshots": SimpleNamespace(initial_input={})}
    assert cse._suppress_model_progress(state) is False


# ── tool_step_service writer ────────────────────────────────────────────────


class _Begin:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def begin(self):
        return _Begin()

    async def execute(self, *a, **k):
        return SimpleNamespace(first=lambda: None)

    async def commit(self):
        return None


class _Factory:
    def __call__(self):
        return self

    async def __aenter__(self):
        return _Session()

    async def __aexit__(self, *a):
        return False


async def _tool_step_payloads(monkeypatch, *, model_text: str, suppress: bool) -> list[dict]:
    recorded: list[dict] = []

    async def fake_insert(db, *, tenant_id, run_id, key, summary, payload):
        recorded.append(payload)

    monkeypatch.setattr(tss, "_insert_runtime_activity", fake_insert)

    async def fake_reserve_tool_execution(db, **kwargs):
        return SimpleNamespace(
            execution=SimpleNamespace(
                sanitized_arguments=kwargs["sanitized_arguments"],
                run_id=run_id_holder["run_id"],
                tool_call_id="call-1",
                tool_name=READ_TOOL,
            ),
            reusable_result=None,
        )

    run_id_holder = {"run_id": RUN_ID}
    monkeypatch.setattr(tss, "reserve_tool_execution", fake_reserve_tool_execution)

    service = object.__new__(tss.RuntimeToolStepService)
    service._session_factory = _Factory()
    service._lease_ttl_seconds = 60

    await service._reserve(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        call_id="call-1",
        tool_name=READ_TOOL,
        assistant_message_id="msg-1",
        arguments={"conversation_id": 131, "operation_capability": RAW_CAPABILITY},
        policy=SimpleNamespace(side_effect_classification="read", retry_policy="safe"),
        lease_owner="owner-1",
        reasoning_content=model_text,
        assistant_content=model_text,
        suppress_model_progress=suppress,
    )
    return recorded


@pytest.mark.asyncio
@pytest.mark.parametrize("model_text", LEAK_SHAPES)
async def test_tool_step_capability_run_emits_no_model_derived_text(monkeypatch, model_text):
    payloads = await _tool_step_payloads(monkeypatch, model_text=model_text, suppress=True)
    assert not _leaked(payloads, model_text)
    kinds = {p.get("activity_type") for p in payloads}
    assert "thinking" not in kinds
    assert "assistant_progress" not in kinds
    for payload in payloads:
        assert payload.get("reasoning_content", "") == ""
    assert RAW_CAPABILITY not in json.dumps(payloads)


@pytest.mark.asyncio
async def test_tool_step_ordinary_run_still_emits_progress(monkeypatch):
    payloads = await _tool_step_payloads(
        monkeypatch, model_text="I will check that account.", suppress=False
    )
    kinds = [p.get("activity_type") for p in payloads]
    assert "thinking" in kinds
    assert "assistant_progress" in kinds
    assert _leaked(payloads, "I will check that account.")


@pytest.mark.asyncio
async def test_tool_step_tool_activity_survives_suppression(monkeypatch):
    payloads = await _tool_step_payloads(monkeypatch, model_text="anything", suppress=True)
    tool_events = [p for p in payloads if p.get("activity_type") == "tool_call"]
    assert tool_events
    for payload in tool_events:
        assert payload["args"]["conversation_id"] == 131
        assert payload["args"]["operation_capability"] == "[REDACTED]"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), ("yes", True), (1, True), (None, True)],
)
def test_tool_step_flag_reader_fails_closed(value, expected):
    state = {"snapshots": SimpleNamespace(initial_input={"suppress_model_progress": value})}
    assert tss._suppress_model_progress(state) is expected


def test_tool_step_flag_absent_means_ordinary_run():
    state = {"snapshots": SimpleNamespace(initial_input={"allowed_tool_names": [READ_TOOL]})}
    assert tss._suppress_model_progress(state) is False


# ── web-chat stream projection ──────────────────────────────────────────────


def test_web_chat_stream_has_no_model_text_to_forward_for_a_capability_run():
    """`chat_stream` forwards `thinking`/`assistant_progress` content and
    `tool_call.reasoning_content` verbatim, so the stream is safe exactly
    when those payloads are. For a suppressed run the first two events do
    not exist and the third field is empty."""
    payloads = _checkpoint_payloads(model_text=RAW_CAPABILITY, suppress=True)
    for payload in payloads:
        assert payload.get("activity_type") not in {"thinking", "assistant_progress"}
        assert str(payload.get("reasoning_content", "")) == ""
        assert RAW_CAPABILITY not in json.dumps(payload)


# ── bridge flag boundary (HTTP) ─────────────────────────────────────────────

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
    "correlation_id": "corr-capability-progress-2026-08-06-0001",
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


def _capability(tool_name: str = READ_TOOL, capability: str = RAW_CAPABILITY) -> dict:
    return {
        "tool_name": tool_name,
        "capability": capability,
        "contract_version": structured_api.CAPABILITY_CONTRACT_VERSION,
    }


async def _fake_resolve_tenant_ok(body):
    return TENANT_ID, None


def _won_path(monkeypatch, effective=(READ_TOOL,)):
    monkeypatch.setattr(structured_api, "_resolve_tenant", _fake_resolve_tenant_ok)

    async def fake_resolve_effective_tool_names(agent_id, requested):
        return list(effective), set()

    monkeypatch.setattr(
        structured_api, "_resolve_effective_tool_names", fake_resolve_effective_tool_names
    )

    fake_agent = SimpleNamespace(id=AGENT_ID, primary_model_id=uuid.uuid4(), fallback_model_id=None)
    fake_model = SimpleNamespace(id=fake_agent.primary_model_id)
    fake_user = SimpleNamespace(id=uuid.uuid4())

    async def fake_claim(*, tenant_id, correlation_id, body, tool_policy_digest):
        return True, SimpleNamespace(
            id=uuid.uuid4(), session_id=None, run_id=None, state="accepted",
            initiating_message_id=None, tool_policy_digest=tool_policy_digest,
            contact_ref=body.contact_ref, agent_id=body.designated_agent_id,
            external_conversation_id=body.conversation_id,
        )

    monkeypatch.setattr(structured_api, "_claim", fake_claim)

    class _WonSession:
        async def execute(self, *a, **k):
            return SimpleNamespace(
                first=lambda: None, mappings=lambda: SimpleNamespace(first=lambda: None)
            )

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

    class _WonFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _WonSession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "async_session", _WonFactory())

    async def fake_ensure_session(db, agent_id, user_id_arg):
        return SimpleNamespace(id=SESSION_ID)

    monkeypatch.setattr(structured_api, "ensure_primary_platform_session", fake_ensure_session)

    class _Reader:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(structured_api, "open_run_state_reader", lambda db: _Reader())

    enqueued: list[dict] = []

    async def fake_enqueue_chat_runtime(db, **kwargs):
        enqueued.append(kwargs)
        return SimpleNamespace(handle=SimpleNamespace(run_id=RUN_ID), message_id=uuid.uuid4())

    monkeypatch.setattr(structured_api, "enqueue_chat_runtime", fake_enqueue_chat_runtime)

    async def fake_poll_and_read(*a, **k):
        return (
            True, "completed",
            structured_api.RunOwnedReply(
                message_id=uuid.uuid4(), content="Yes.",
                delivery_kind="terminal", lifecycle_status="completed",
            ),
            None,
        )

    monkeypatch.setattr(structured_api, "_poll_and_read", fake_poll_and_read)
    return enqueued


@pytest.mark.asyncio
async def test_bridge_sets_the_flag_only_for_a_capability_bearing_turn(monkeypatch, client):
    enqueued = _won_path(monkeypatch)
    payload = dict(GOLDEN_REQUEST, tool_capabilities=[_capability()])
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )
    assert response.status_code == 200
    assert enqueued[0]["suppress_model_progress"] is True


@pytest.mark.asyncio
async def test_bridge_does_not_set_the_flag_for_an_ordinary_turn(monkeypatch, client):
    enqueued = _won_path(monkeypatch)
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=dict(GOLDEN_REQUEST), headers=_headers()
        )
    assert response.status_code == 200
    assert enqueued[0]["suppress_model_progress"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["suppress_model_progress", "contains_operation_capability"]
)
async def test_caller_cannot_submit_the_internal_flag(monkeypatch, client, field):
    """The flag is server-derived. `extra=\"forbid\"` makes a caller-supplied
    one a 422, not a silently honoured override."""
    enqueued = _won_path(monkeypatch)
    payload = dict(GOLDEN_REQUEST)
    payload[field] = True
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert enqueued == []


@pytest.mark.asyncio
async def test_malformed_capability_never_starts_a_run_or_sets_the_flag(monkeypatch, client):
    enqueued = _won_path(monkeypatch)
    payload = dict(
        GOLDEN_REQUEST,
        tool_capabilities=[_capability(tool_name="a_tool_this_turn_cannot_call")],
    )
    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/structured/message", json=payload, headers=_headers()
        )
    assert response.status_code == 400
    assert response.json()["error"] == "capability_for_unavailable_tool"
    assert enqueued == []


@pytest.mark.asyncio
async def test_one_capability_run_does_not_suppress_another_ordinary_run(monkeypatch, client):
    """Request-local: the flag is derived per request from that request's own
    validated capabilities, never from module or process state."""
    enqueued = _won_path(monkeypatch)
    async with await client() as ac:
        await ac.post(
            "/api/isola/bridge/structured/message",
            json=dict(GOLDEN_REQUEST, tool_capabilities=[_capability()]),
            headers=_headers(),
        )
        await ac.post(
            "/api/isola/bridge/structured/message",
            json=dict(GOLDEN_REQUEST, correlation_id="corr-progress-ordinary-0002"),
            headers=_headers(),
        )
    assert [call["suppress_model_progress"] for call in enqueued] == [True, False]


def test_flag_is_not_part_of_the_public_request_schema():
    fields = set(structured_api.StructuredBridgeMessageIn.model_fields)
    assert "suppress_model_progress" not in fields
    assert "contains_operation_capability" not in fields


def test_bridge_passes_a_plain_bool_not_derived_from_model_or_prompt_text():
    """The flag is a bare boolean derived from the validated capability
    list — never parsed out of the prompt, the runtime instruction or the
    model's output, and never carrying a capability, fingerprint, tenant,
    agent, approval or business scope."""
    import inspect

    source = inspect.getsource(structured_api)
    assert "suppress_model_progress=bool(body.tool_capabilities)" in source
    # Never inferred by parsing the prompt or the model's output.
    assert "re.search" not in source
    assert "runtime_instruction.find" not in source
    # And the only value ever written into the run payload is `True`.
    intake_source = inspect.getsource(
        __import__(
            "app.services.agent_runtime.chat_intake", fromlist=["chat_intake"]
        )
    )
    assert '{"suppress_model_progress": True} if suppress_model_progress else {}' in intake_source
