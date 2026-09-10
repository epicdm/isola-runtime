"""Server-side enforcement kernel for bounded internal agent runs (INT-001).

Everything in this module is DEFAULT-OFF. The ``active_bounds`` ContextVar is
None on every pre-existing code path, and every wrapper installed here is a
straight pass-through in that case. /internal/dispatch, the WhatsApp webhook,
the trigger daemon and interactive chat are unaffected.

Inside a bounded run the kernel enforces, in the receiver process and NOT by
prompt:

  * a DEFAULT-DENY tool allowlist, applied twice -- once when the tool
    catalogue is offered to the model, and again (authoritatively) at
    execution time, so a model that invents a tool name still cannot reach
    the handler;
  * a hard ceiling on provider calls that COUNTS RETRIES, because the wrapper
    sits on the provider client itself: every attempt, including a failover
    retry taken inside call_llm, passes through this one counter;
  * a per-call input-token ceiling, and an injected per-call output-token
    ceiling;
  * a wall-clock deadline plus an explicit cancel fence, evaluated
    immediately BEFORE every provider call, so that once the receiver has
    answered "timed out" no further provider call can be issued. This is the
    difference between cancelling a coroutine and proving the provider is not
    still being called.

WHY WRAPPERS AND NOT AN EDIT TO agent_tools.py
----------------------------------------------
app/services/llm/caller.py binds the symbols it uses with FROM-imports --
``execute_tool`` and ``get_agent_tools_for_llm`` at caller.py:26, and
``create_llm_client`` at caller.py:31. Patching the defining module alone
would therefore never be seen by the call site. install() rebinds the names on
app.services.llm.caller, the module that actually calls them. That is both the
effective change and the minimal one: it edits no existing file, and it is
undone by uninstall().

THE F1 LAW
----------
An allowlist of tool names that do not exist permits everything while looking
strict, and its tests pass forever. ``resolve_allowlist`` therefore validates
every requested name against the real AGENT_TOOLS registry and raises on any
name the registry does not contain. A typo fails the run closed instead of
silently widening it.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterable

from loguru import logger

# ---------------------------------------------------------------------------
# States. The receiver's run ledger stores exactly these five values.
# ---------------------------------------------------------------------------
STATE_ACCEPTED = "accepted"
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_TIMED_OUT = "timed_out"

ALL_STATES = (
    STATE_ACCEPTED,
    STATE_RUNNING,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_TIMED_OUT,
)

# ---------------------------------------------------------------------------
# INT-001's permitted work, expressed as tool names.
#
# INT-001 is: read the offer documents, read the customer record, write one
# work item and one comment.
#
# The write half is NOT in this list on purpose. There is no create-issue or
# post-comment tool anywhere in AGENT_TOOLS -- the issue and the comment are
# Paperclip objects, and the receiver writes the comment itself, exactly once,
# after the model has returned. That makes "one comment" a structural property
# of the receiver rather than something the model is trusted to observe.
#
# Everything not named here is denied, including execute_code, write_file,
# send_email, install_skill, import_mcp_server, create_payment_link and the
# trigger tools. send_message_to_agent and send_file_to_agent are denied too:
# that is the ZERO-DELEGATION limit, enforced here rather than left to depend
# on whether a subordinate happens to be reachable.
# ---------------------------------------------------------------------------
INT_001_ALLOWED_TOOLS: tuple[str, ...] = (
    "read_document",
    "read_file",
    "list_files",
    "search_files",
    "find_files",
    "read_skill_md",
    "get_customer_balance",
    "check_enquiry_status",
)


class BoundsExceeded(RuntimeError):
    """Raised inside a bounded run when a hard limit is hit."""


class RunFenced(RuntimeError):
    """Raised when a provider call is attempted after the fence closed."""


class AllowlistError(RuntimeError):
    """Raised when a requested tool name is not in the real registry."""


@dataclass
class RunBounds:
    """The live budget for one bounded run."""

    run_id: str
    allowed_tools: frozenset[str]
    max_provider_calls: int = 3
    max_input_tokens: int = 12_000
    max_output_tokens: int = 2_000
    duration_budget_s: float = 120.0

    started_monotonic: float = field(default_factory=time.monotonic)
    provider_calls: int = 0
    denied_tools: list[str] = field(default_factory=list)
    _fenced: bool = False
    _fence_reason: str = ""

    # -- fence ---------------------------------------------------------------
    @property
    def deadline_monotonic(self) -> float:
        return self.started_monotonic + self.duration_budget_s

    @property
    def fenced(self) -> bool:
        return self._fenced

    @property
    def fence_reason(self) -> str:
        return self._fence_reason

    def close_fence(self, reason: str) -> None:
        """Permanently forbid further provider calls for this run.

        Called by the receiver the moment it decides to answer 'timed out' or
        'failed'. Idempotent, and never reopens.
        """
        if not self._fenced:
            self._fenced = True
            self._fence_reason = reason
            logger.info(f"[int001] fence closed run={self.run_id} reason={reason}")

    def remaining_s(self) -> float:
        return self.deadline_monotonic - time.monotonic()

    # -- admission -----------------------------------------------------------
    def admit_provider_call(self, estimated_input_tokens: int) -> int:
        """Admit one provider attempt, or raise. Retries count here.

        Returns the max_tokens value the attempt must be capped to.
        """
        if self._fenced:
            raise RunFenced(
                f"provider call refused after fence closed ({self._fence_reason})"
            )
        if time.monotonic() >= self.deadline_monotonic:
            self.close_fence("duration_budget_exhausted")
            raise RunFenced("provider call refused: duration budget exhausted")
        if self.provider_calls >= self.max_provider_calls:
            raise BoundsExceeded(
                f"provider call ceiling reached "
                f"({self.provider_calls}/{self.max_provider_calls}, retries included)"
            )
        if estimated_input_tokens > self.max_input_tokens:
            raise BoundsExceeded(
                f"input token ceiling exceeded "
                f"({estimated_input_tokens} > {self.max_input_tokens})"
            )
        self.provider_calls += 1
        return self.max_output_tokens

    def tool_permitted(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools

    def record_denial(self, tool_name: str) -> None:
        self.denied_tools.append(tool_name)

    def summary(self) -> dict[str, Any]:
        """Non-secret run facts, safe to log and to return to a caller."""
        return {
            "providerCalls": self.provider_calls,
            "maxProviderCalls": self.max_provider_calls,
            "deniedTools": sorted(set(self.denied_tools)),
            "fenced": self._fenced,
            "fenceReason": self._fence_reason,
        }


active_bounds: ContextVar[RunBounds | None] = ContextVar("int001_active_bounds", default=None)


# ---------------------------------------------------------------------------
# Allowlist resolution -- the F1 law.
# ---------------------------------------------------------------------------
def registry_tool_names() -> frozenset[str]:
    """Every tool name the real registry declares."""
    from app.services.agent_tools import AGENT_TOOLS

    names: set[str] = set()
    for entry in AGENT_TOOLS:
        fn = entry.get("function") if isinstance(entry, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            names.add(fn["name"])
    return frozenset(names)


def resolve_allowlist(requested: Iterable[str]) -> frozenset[str]:
    """Validate requested tool names against the real registry.

    Raises AllowlistError naming every unknown entry. A run whose allowlist
    cannot be resolved must not start: an unresolvable name is either a typo
    that silently widens the gate, or evidence the registry moved.
    """
    requested_set = frozenset(requested)
    known = registry_tool_names()
    unknown = sorted(requested_set - known)
    if unknown:
        raise AllowlistError(
            "allowlist names absent from AGENT_TOOLS: " + ", ".join(unknown)
        )
    return requested_set


# ---------------------------------------------------------------------------
# Wrappers.
# ---------------------------------------------------------------------------
_ORIGINALS: dict[str, Any] = {}


def _estimate_input_tokens(messages: Any, tools: Any) -> int:
    """Conservative character-based estimate, matching token_tracker's ratio."""
    from app.services.token_tracker import estimate_tokens_from_chars

    chars = 0
    try:
        for m in messages or []:
            content = getattr(m, "content", None)
            if content is None and isinstance(m, dict):
                content = m.get("content")
            if isinstance(content, str):
                chars += len(content)
            elif content is not None:
                chars += len(str(content))
        if tools:
            chars += len(str(tools))
    except Exception:  # pragma: no cover - estimation must never crash a run
        return 0
    return estimate_tokens_from_chars(chars)


class BoundedClient:
    """Proxy over a real LLMClient that enforces the run's budget.

    Every provider attempt in the run -- first call, tool-loop continuation,
    failover retry -- reaches the provider through complete() or stream() on
    this object, which is why the counter here is a true ceiling on calls
    WITH retries counted inside the allowance.
    """

    def __init__(self, inner: Any, bounds: RunBounds):
        self._inner = inner
        self._bounds = bounds

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None, **kwargs):
        capped = self._bounds.admit_provider_call(_estimate_input_tokens(messages, tools))
        max_tokens = capped if max_tokens is None else min(int(max_tokens), capped)
        return await self._inner.complete(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens, **kwargs
        )

    async def stream(self, messages, tools=None, temperature=None, max_tokens=None, **kwargs):
        capped = self._bounds.admit_provider_call(_estimate_input_tokens(messages, tools))
        max_tokens = capped if max_tokens is None else min(int(max_tokens), capped)
        return await self._inner.stream(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens, **kwargs
        )

    async def close(self):
        closer = getattr(self._inner, "close", None)
        if closer is not None:
            return await closer()
        return None


def _wrap_create_llm_client(original):
    def wrapped(*args, **kwargs):
        client = original(*args, **kwargs)
        bounds = active_bounds.get()
        if bounds is None:
            return client
        return BoundedClient(client, bounds)

    wrapped.__int001_wrapped__ = True  # type: ignore[attr-defined]
    return wrapped


def _wrap_execute_tool(original):
    async def wrapped(tool_name, arguments, agent_id, user_id, session_id: str = ""):
        bounds = active_bounds.get()
        if bounds is None:
            return await original(tool_name, arguments, agent_id, user_id, session_id)
        if not bounds.tool_permitted(tool_name):
            bounds.record_denial(tool_name)
            logger.warning(
                f"[int001] tool denied run={bounds.run_id} tool={tool_name} "
                f"(default-deny; allowlist={sorted(bounds.allowed_tools)})"
            )
            return (
                f"Denied: '{tool_name}' is not permitted for this run. This run is "
                f"restricted to reading documents and the customer record; it has no "
                f"authority to execute code, write files, message anyone, delegate, "
                f"or change configuration."
            )
        return await original(tool_name, arguments, agent_id, user_id, session_id)

    wrapped.__int001_wrapped__ = True  # type: ignore[attr-defined]
    return wrapped


def _wrap_tools_for_llm(original):
    async def wrapped(agent_id):
        tools = await original(agent_id)
        bounds = active_bounds.get()
        if bounds is None:
            return tools
        filtered = []
        for entry in tools or []:
            fn = entry.get("function") if isinstance(entry, dict) else None
            name = fn.get("name") if isinstance(fn, dict) else None
            if isinstance(name, str) and name in bounds.allowed_tools:
                filtered.append(entry)
        return filtered

    wrapped.__int001_wrapped__ = True  # type: ignore[attr-defined]
    return wrapped


def install() -> None:
    """Idempotently install the wrappers on the module that calls them.

    Safe to call on every request: a second call is a no-op. Installing does
    not by itself bound anything -- the wrappers pass through until a
    RunBounds is set on the ContextVar.
    """
    from app.services.llm import caller as caller_mod

    if getattr(caller_mod.create_llm_client, "__int001_wrapped__", False):
        return

    _ORIGINALS["create_llm_client"] = caller_mod.create_llm_client
    _ORIGINALS["execute_tool"] = caller_mod.execute_tool
    _ORIGINALS["get_agent_tools_for_llm"] = caller_mod.get_agent_tools_for_llm

    caller_mod.create_llm_client = _wrap_create_llm_client(_ORIGINALS["create_llm_client"])
    caller_mod.execute_tool = _wrap_execute_tool(_ORIGINALS["execute_tool"])
    caller_mod.get_agent_tools_for_llm = _wrap_tools_for_llm(
        _ORIGINALS["get_agent_tools_for_llm"]
    )
    logger.info("[int001] bounded-run kernel installed on app.services.llm.caller")


def uninstall() -> None:
    """Restore the original bindings. Used by tests."""
    from app.services.llm import caller as caller_mod

    for name, original in _ORIGINALS.items():
        setattr(caller_mod, name, original)
    _ORIGINALS.clear()
