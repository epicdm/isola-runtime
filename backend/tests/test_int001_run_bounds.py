"""Acceptance tests for the INT-001 bounded-run kernel.

No database and no network. These prove the enforcement properties that live
in app/services/run_bounds.py:

  * the allowlist is validated against the REAL AGENT_TOOLS registry, so a
    name that does not exist fails the run closed instead of silently
    widening the gate (the F1 law);
  * every registry tool is classified -- allowed or denied, none unclassified;
  * default-deny holds at execution time even when the model names a tool that
    was never offered;
  * delegation is denied by the gate, not by the accident of an unreachable
    subordinate;
  * the provider-call ceiling counts retries, because it sits on the provider
    client;
  * the input ceiling refuses and the output ceiling is injected;
  * once the fence closes, no further provider call is admitted -- which is
    the timeout proof.
"""

from __future__ import annotations

import time

import pytest

from app.services.run_bounds import (
    INT_001_ALLOWED_TOOLS,
    AllowlistError,
    BoundedClient,
    BoundsExceeded,
    RunBounds,
    RunFenced,
    _wrap_execute_tool,
    _wrap_tools_for_llm,
    active_bounds,
    registry_tool_names,
    resolve_allowlist,
)

# Tools that must never be reachable from a bounded internal run.
MUST_BE_DENIED = {
    "execute_code",
    "execute_code_e2b",
    "write_file",
    "edit_file",
    "delete_file",
    "send_email",
    "reply_email",
    "read_emails",
    "install_skill",
    "import_mcp_server",
    "create_payment_link",
    "publish_page",
    "set_trigger",
    "update_trigger",
    "cancel_trigger",
    "send_message_to_agent",
    "send_file_to_agent",
    "send_channel_file",
    "send_web_message",
    "upload_image",
}


def _bounds(**kw) -> RunBounds:
    defaults = dict(
        run_id="run-test",
        allowed_tools=frozenset(INT_001_ALLOWED_TOOLS),
        max_provider_calls=3,
        max_input_tokens=12_000,
        max_output_tokens=2_000,
        duration_budget_s=120.0,
    )
    defaults.update(kw)
    return RunBounds(**defaults)


# ---------------------------------------------------------------------------
# The F1 law
# ---------------------------------------------------------------------------
def test_allowlist_resolves_against_the_real_registry():
    """Every INT-001 tool name exists in AGENT_TOOLS. This test fails if the
    registry is renamed underneath us -- which is the point."""
    resolved = resolve_allowlist(INT_001_ALLOWED_TOOLS)
    assert resolved == frozenset(INT_001_ALLOWED_TOOLS)


def test_allowlist_rejects_a_name_that_does_not_exist():
    with pytest.raises(AllowlistError) as exc:
        resolve_allowlist(["read_document", "post_comment"])
    assert "post_comment" in str(exc.value)


def test_every_registry_tool_is_classified():
    """No tool may be unclassified. allowed + denied == the whole registry."""
    registry = registry_tool_names()
    allowed = frozenset(INT_001_ALLOWED_TOOLS)
    assert allowed <= registry
    denied = registry - allowed
    assert allowed | denied == registry
    assert not (allowed & denied)


def test_the_dangerous_tools_are_denied():
    allowed = frozenset(INT_001_ALLOWED_TOOLS)
    registry = registry_tool_names()
    for name in MUST_BE_DENIED:
        assert name in registry, f"{name} vanished from AGENT_TOOLS -- update this test"
        assert name not in allowed, f"{name} must not be reachable from a bounded run"


# ---------------------------------------------------------------------------
# Default-deny at execution time
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_execution_gate_denies_an_unoffered_tool():
    calls: list[str] = []

    async def original(tool_name, arguments, agent_id, user_id, session_id=""):
        calls.append(tool_name)
        return "REAL HANDLER RAN"

    guarded = _wrap_execute_tool(original)
    token = active_bounds.set(_bounds())
    try:
        result = await guarded("execute_code", {"code": "1"}, "a", "u", "s")
    finally:
        active_bounds.reset(token)

    assert calls == []  # the real handler was never reached
    assert "Denied" in result


@pytest.mark.asyncio
async def test_execution_gate_denies_delegation():
    async def original(tool_name, arguments, agent_id, user_id, session_id=""):
        return "REAL HANDLER RAN"

    guarded = _wrap_execute_tool(original)
    token = active_bounds.set(_bounds())
    try:
        for name in ("send_message_to_agent", "send_file_to_agent"):
            assert "Denied" in await guarded(name, {}, "a", "u", "s")
    finally:
        active_bounds.reset(token)


@pytest.mark.asyncio
async def test_execution_gate_allows_permitted_tools():
    async def original(tool_name, arguments, agent_id, user_id, session_id=""):
        return "REAL HANDLER RAN"

    guarded = _wrap_execute_tool(original)
    token = active_bounds.set(_bounds())
    try:
        assert await guarded("read_document", {}, "a", "u", "s") == "REAL HANDLER RAN"
    finally:
        active_bounds.reset(token)


@pytest.mark.asyncio
async def test_gate_is_a_passthrough_outside_a_bounded_run():
    """No existing code path changes behaviour."""

    async def original(tool_name, arguments, agent_id, user_id, session_id=""):
        return "REAL HANDLER RAN"

    guarded = _wrap_execute_tool(original)
    assert active_bounds.get() is None
    assert await guarded("execute_code", {}, "a", "u", "s") == "REAL HANDLER RAN"


@pytest.mark.asyncio
async def test_catalogue_offered_to_the_model_is_filtered():
    async def original(agent_id):
        return [
            {"type": "function", "function": {"name": "read_document"}},
            {"type": "function", "function": {"name": "execute_code"}},
            {"type": "function", "function": {"name": "send_message_to_agent"}},
        ]

    guarded = _wrap_tools_for_llm(original)
    token = active_bounds.set(_bounds())
    try:
        offered = await guarded("agent")
    finally:
        active_bounds.reset(token)
    assert [t["function"]["name"] for t in offered] == ["read_document"]


# ---------------------------------------------------------------------------
# Provider ceilings
# ---------------------------------------------------------------------------
class _FakeClient:
    def __init__(self):
        self.calls: list[int | None] = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None, **kw):
        self.calls.append(max_tokens)
        return "ok"

    async def stream(self, messages, tools=None, temperature=None, max_tokens=None, **kw):
        self.calls.append(max_tokens)
        return "ok"


@pytest.mark.asyncio
async def test_provider_ceiling_counts_retries():
    """A retry is a provider attempt. It goes through the same counter."""
    inner = _FakeClient()
    bounds = _bounds(max_provider_calls=3)
    client = BoundedClient(inner, bounds)

    msgs = [{"role": "user", "content": "hi"}]
    await client.complete(msgs)            # call 1
    await client.complete(msgs)            # call 2 (tool-loop continuation)
    await client.complete(msgs)            # call 3 (failover retry)
    with pytest.raises(BoundsExceeded):
        await client.complete(msgs)        # 4th refused

    assert len(inner.calls) == 3
    assert bounds.provider_calls == 3


@pytest.mark.asyncio
async def test_output_ceiling_is_injected_and_never_raised():
    inner = _FakeClient()
    client = BoundedClient(inner, _bounds(max_output_tokens=2_000))
    msgs = [{"role": "user", "content": "hi"}]
    await client.complete(msgs)                      # caller passed nothing
    await client.complete(msgs, max_tokens=99_999)   # caller asked for more
    await client.complete(msgs, max_tokens=100)      # caller asked for less
    assert inner.calls == [2_000, 2_000, 100]


@pytest.mark.asyncio
async def test_input_ceiling_refuses_before_the_provider_is_reached():
    inner = _FakeClient()
    bounds = _bounds(max_input_tokens=100)
    client = BoundedClient(inner, bounds)
    huge = [{"role": "user", "content": "x" * 100_000}]
    with pytest.raises(BoundsExceeded):
        await client.complete(huge)
    assert inner.calls == []           # provider never reached
    assert bounds.provider_calls == 0  # a refused attempt does not consume budget


# ---------------------------------------------------------------------------
# The timeout fence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_provider_call_is_admitted_after_the_fence_closes():
    """The timeout proof: not 'the coroutine was cancelled' but 'the provider
    cannot be called again'."""
    inner = _FakeClient()
    bounds = _bounds()
    client = BoundedClient(inner, bounds)
    msgs = [{"role": "user", "content": "hi"}]

    await client.complete(msgs)
    assert len(inner.calls) == 1

    bounds.close_fence("receiver_timeout")          # receiver answers 504 here
    before = len(inner.calls)

    for _ in range(5):
        with pytest.raises(RunFenced):
            await client.complete(msgs)
        with pytest.raises(RunFenced):
            await client.stream(msgs)

    assert len(inner.calls) == before  # provider count did not move
    assert bounds.fenced and bounds.fence_reason == "receiver_timeout"


@pytest.mark.asyncio
async def test_expired_duration_budget_closes_the_fence_itself():
    inner = _FakeClient()
    bounds = _bounds(duration_budget_s=0.01)
    client = BoundedClient(inner, bounds)
    time.sleep(0.05)
    with pytest.raises(RunFenced):
        await client.complete([{"role": "user", "content": "hi"}])
    assert bounds.fenced
    assert inner.calls == []


def test_summary_carries_no_secret_material():
    bounds = _bounds()
    bounds.record_denial("execute_code")
    summary = bounds.summary()
    assert set(summary) == {
        "providerCalls",
        "maxProviderCalls",
        "deniedTools",
        "fenced",
        "fenceReason",
    }
