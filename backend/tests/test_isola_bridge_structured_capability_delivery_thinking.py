"""The `chat_messages.thinking` sink for capability-bearing runs
(`dec-pr42-capability-turns-no-model-derived-progress-events-2026-08-06`).

The independent exact-head review of `7ce5e412` proved a second, non-event
model-derived sink in `checkpoint_side_effects.py`. The previous head
suppressed `AgentRunEvent` payloads correctly but left
`delivery_from_checkpoint` copying the model's terminal `reasoning_content`
into `DeliveryRequest.thinking`, which `delivery.py` persists verbatim into
the `chat_messages.thinking` COLUMN for a `direct` session — which is what
every structured-bridge run is — and which `api/chat_sessions.py` returns to
ordinary tenant clients.

The settle-time redaction in the bridge rewrites `content` only, and only
when the reply body actually changed, so a capability echoed into terminal
reasoning but NOT into the reply body was persisted raw and tenant-readable
with no redaction attempted at all.

Every assertion below is "the model's text is ABSENT", never "the exact
token was replaced" — the leak shapes are parametrized precisely to show the
control does not depend on recognizing the value.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.agent_runtime import checkpoint_side_effects as cse

AGENT_ID = uuid.UUID("81b38cd6-9fba-4cc8-8f87-1bce1a4aa162")
TENANT_ID = uuid.UUID("43b006e4-33e0-42a8-bec7-4422ba290d79")
RUN_ID = uuid.uuid4()

READ_TOOL = "isola_revenue_customer_context_get"
RAW_CAPABILITY = "cap_9f8e7d6c5b4a39281726354453627180_thinking_probe"
RAW_CAPABILITY_2 = "cap_00112233445566778899aabbccddeeff_thinking_second"

CLEAN_REPLY = "I have pulled up that account for you."

# Ways a model could reproduce the credential inside its terminal reasoning
# that an exact-string scrubber would miss. Structural omission covers all of
# them by construction.
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


def _run_record(system_role: str = "assistant"):
    return SimpleNamespace(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        agent_id=str(AGENT_ID),
        system_role=system_role,
    )


def _terminal_checkpoint(
    *,
    reasoning: str | None,
    suppress: bool | None,
    final_answer: str = CLEAN_REPLY,
    status: str = "completed",
    with_snapshots: bool = True,
):
    """A terminal checkpoint whose `finish` message carries model reasoning."""
    initial_input: dict = {"message_id": str(uuid.uuid4())}
    if suppress is not None:
        initial_input["suppress_model_progress"] = suppress
    state: dict = {
        "lifecycle": {"status": status, "final_answer": final_answer},
        "messages": [
            {
                "role": "assistant",
                "id": "msg-final",
                "runtime_run_id": str(RUN_ID),
                "runtime_intent": "finish",
                "reasoning_content": reasoning,
                "content": final_answer,
            }
        ],
    }
    if with_snapshots:
        state["snapshots"] = SimpleNamespace(initial_input=initial_input)
    return SimpleNamespace(checkpoint_id="ckpt-final", state=state)


# ── 1. capability run: no model text in EITHER projection ───────────────────


@pytest.mark.parametrize("reasoning", LEAK_SHAPES)
def test_capability_run_delivery_thinking_is_none(reasoning):
    request = cse.delivery_from_checkpoint(
        _run_record(), _terminal_checkpoint(reasoning=reasoning, suppress=True)
    )
    assert request is not None
    assert request.thinking is None


@pytest.mark.parametrize("reasoning", LEAK_SHAPES)
def test_capability_run_event_projection_still_carries_no_model_text(reasoning):
    """The previously accepted control must not regress."""
    events, _calls = cse._runtime_observation_events(
        _run_record(), _terminal_checkpoint(reasoning=reasoning, suppress=True)
    )
    payloads = [payload for _t, _s, payload, _k, _e in events]
    assert not any(
        reasoning in v for p in payloads for v in p.values() if isinstance(v, str)
    )
    assert RAW_CAPABILITY not in json.dumps(payloads)


# ── 2. capability ONLY in reasoning: the content-redaction no-op path ───────


def test_capability_only_in_reasoning_is_not_saved_by_content_redaction():
    """The exact gap the review proved.

    When the reply body is clean, the bridge's settle-time UPDATE does not
    run at all (`if settled_reply != reply.content`), so nothing would ever
    rewrite the persisted row. Suppression is the only thing standing here.
    """
    ckpt = _terminal_checkpoint(reasoning=RAW_CAPABILITY, suppress=True)
    request = cse.delivery_from_checkpoint(_run_record(), ckpt)
    assert request.content == CLEAN_REPLY
    assert RAW_CAPABILITY not in request.content  # redaction would be a no-op
    assert request.thinking is None  # so this must already be safe


def test_settle_time_sql_covers_content_only_so_thinking_must_be_suppressed():
    """Documents WHY suppression, not redaction, is the control here."""
    from app.api import isola_bridge_structured as structured_api

    sql = str(structured_api._REDACT_STORED_REPLY_SQL).lower()
    assert "set content" in sql
    assert "thinking" not in sql


# ── 3. capability in BOTH reasoning and content ─────────────────────────────


def test_capability_in_reasoning_and_content_thinking_none_content_redactable():
    from app.api import isola_bridge_structured as structured_api

    dirty_reply = f"Here you go: {RAW_CAPABILITY}"
    ckpt = _terminal_checkpoint(
        reasoning=RAW_CAPABILITY, suppress=True, final_answer=dirty_reply
    )
    request = cse.delivery_from_checkpoint(_run_record(), ckpt)
    assert request.thinking is None
    # The content path remains the settle-time redaction's job, unchanged.
    assert RAW_CAPABILITY in request.content
    cap = SimpleNamespace(capability=RAW_CAPABILITY)
    redacted = structured_api._redact_capabilities(request.content, [cap])
    assert RAW_CAPABILITY not in redacted
    assert structured_api._CAPABILITY_REDACTION_MARKER in redacted


# ── 4. ordinary run is untouched ────────────────────────────────────────────


@pytest.mark.parametrize("reasoning", LEAK_SHAPES)
@pytest.mark.parametrize("suppress", [None, False])
def test_ordinary_run_delivery_thinking_unchanged(reasoning, suppress):
    """No global suppression: absent OR explicitly-false keeps prior behaviour."""
    request = cse.delivery_from_checkpoint(
        _run_record(), _terminal_checkpoint(reasoning=reasoning, suppress=suppress)
    )
    assert request.thinking == reasoning


def test_ordinary_run_matches_unsuppressed_terminal_thinking_helper():
    """Head behaviour for an ordinary run is byte-identical to `_terminal_thinking`."""
    run, ckpt = _run_record(), _terminal_checkpoint(reasoning="ordinary", suppress=None)
    assert cse.delivery_from_checkpoint(run, ckpt).thinking == cse._terminal_thinking(run, ckpt)


def test_ordinary_run_with_no_reasoning_is_still_none():
    request = cse.delivery_from_checkpoint(
        _run_record(), _terminal_checkpoint(reasoning=None, suppress=None)
    )
    assert request.thinking is None


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_suppression_applies_across_every_terminal_status(status):
    ckpt = _terminal_checkpoint(reasoning=RAW_CAPABILITY, suppress=True, status=status)
    assert cse.delivery_from_checkpoint(_run_record(), ckpt).thinking is None


# ── 4b. no sibling-field leak on the delivery object ────────────────────────


@pytest.mark.parametrize("reasoning", LEAK_SHAPES)
def test_no_delivery_request_field_carries_the_model_reasoning(reasoning):
    """Suppression must not be defeated by relocating the text.

    Asserts across the WHOLE DeliveryRequest, not just `thinking`, so moving
    the model's terminal reasoning into `failure_message`, `content` or any
    future sibling field is caught rather than silently accepted.
    """
    request = cse.delivery_from_checkpoint(
        _run_record(), _terminal_checkpoint(reasoning=reasoning, suppress=True)
    )
    for field in dataclasses.fields(request):
        value = getattr(request, field.name)
        if isinstance(value, str):
            assert reasoning not in value, f"model reasoning leaked into {field.name!r}"


def test_failed_status_suppressed_run_keeps_failure_metadata_clean():
    """The failure fields come from lifecycle error metadata, never the model."""
    ckpt = _terminal_checkpoint(reasoning=RAW_CAPABILITY, suppress=True, status="failed")
    ckpt.state["lifecycle"]["error"] = {"code": "tool_failed", "message": "upstream 502"}
    request = cse.delivery_from_checkpoint(_run_record(), ckpt)
    assert request.thinking is None
    assert request.failure_code == "tool_failed"
    assert request.failure_message == "upstream 502"
    strings = {
        f.name: getattr(request, f.name)
        for f in dataclasses.fields(request)
        if isinstance(getattr(request, f.name), str)
    }
    assert RAW_CAPABILITY not in json.dumps(strings)


# ── 5. persistence + tenant serialization hops ──────────────────────────────


def test_delivery_persists_thinking_solely_from_the_delivery_request():
    """`chat_messages.thinking` has exactly one non-default writer."""
    from app.services.agent_runtime import delivery

    src = inspect.getsource(delivery)
    assert 'thinking=request.thinking if session.session_type == "direct" else None' in src
    # No other ChatMessage construction in the module supplies `thinking`.
    assert src.count("thinking=") == 1


def test_chat_session_serializer_omits_a_null_thinking_column():
    """A NULL column cannot produce a `thinking` key for a tenant client."""
    from app.api import chat_sessions

    src = inspect.getsource(chat_sessions)
    assert 'if getattr(message, "thinking", None):' in src
    assert 'entry["thinking"] = message.thinking' in src
    # The base entry never carries thinking on its own.
    message = SimpleNamespace(
        id=uuid.uuid4(),
        role="assistant",
        content=CLEAN_REPLY,
        created_at=datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC),
        thinking=None,
    )
    assert "thinking" not in chat_sessions._base_message_entry(message)


def test_capability_run_end_to_end_column_value_is_null():
    """Delivery request -> column value, for a direct session."""
    request = cse.delivery_from_checkpoint(
        _run_record(), _terminal_checkpoint(reasoning=RAW_CAPABILITY, suppress=True)
    )
    for session_type in ("direct", "group"):
        column_value = request.thinking if session_type == "direct" else None
        assert column_value is None


# ── 6. waiting delivery ─────────────────────────────────────────────────────


@pytest.mark.parametrize("suppress", [True, False, None])
def test_waiting_delivery_never_introduces_thinking(suppress):
    initial_input: dict = {}
    if suppress is not None:
        initial_input["suppress_model_progress"] = suppress
    ckpt = SimpleNamespace(
        checkpoint_id="ckpt-wait",
        state={
            "lifecycle": {
                "status": "waiting_user",
                "waiting_request": {
                    "correlation_id": "corr-1",
                    "question": "Which invoice did you mean?",
                },
            },
            "messages": [],
            "snapshots": SimpleNamespace(initial_input=initial_input),
        },
    )
    request = cse.delivery_from_checkpoint(_run_record(), ckpt)
    assert request.kind != "terminal"
    assert request.thinking is None


# ── 7. group acknowledgement ────────────────────────────────────────────────


def test_group_acknowledgement_delivery_carries_no_thinking():
    from app.services.agent_runtime import group_acknowledgement

    src = inspect.getsource(group_acknowledgement)
    assert "DeliveryRequest(" in src
    assert "thinking" not in src


def test_delivery_request_thinking_defaults_to_none():
    from app.services.agent_runtime.delivery import DeliveryRequest

    assert DeliveryRequest.__dataclass_fields__["thinking"].default is None


# ── 8. malformed / missing protected suppression state ──────────────────────


@pytest.mark.parametrize("value", ["yes", 1, 0.0, [], {}, None, "false"])
def test_malformed_suppression_state_fails_closed_for_thinking(value):
    """Any PRESENT value that is not exactly `False` suppresses."""
    ckpt = _terminal_checkpoint(reasoning=RAW_CAPABILITY, suppress=None)
    ckpt.state["snapshots"].initial_input["suppress_model_progress"] = value
    assert cse.delivery_from_checkpoint(_run_record(), ckpt).thinking is None


def test_missing_snapshots_is_an_ordinary_run_not_a_crash():
    """A partially constructed run object must not raise here."""
    ckpt = _terminal_checkpoint(reasoning="ordinary", suppress=None, with_snapshots=False)
    assert cse.delivery_from_checkpoint(_run_record(), ckpt).thinking == "ordinary"


def test_group_planning_completed_run_still_returns_none():
    """Unrelated early-return path is untouched by the correction."""
    ckpt = _terminal_checkpoint(reasoning=RAW_CAPABILITY, suppress=True)
    assert cse.delivery_from_checkpoint(_run_record("group_planning"), ckpt) is None
