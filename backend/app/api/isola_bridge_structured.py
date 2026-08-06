# ISOLA GLUE — additive structured bridge endpoint (not upstream)
"""Structured Foundation → Clawith bridge endpoint.

Implements `dec-clawith-structured-bridge-versioned-endpoint-2026-08-02` /
`dec-structured-bridge-dark-launch-and-split-execution-2026-08-02` slice S2:
an ADDITIVE versioned route, `POST /api/isola/bridge/structured/message`,
carrying the ratified request/response envelope from
`dec-foundation-clawith-structured-response-contract-2026-07-29`.

This file changes NOTHING about the two existing bridge routes
(`isola_bridge.py`, `isola_bridge_v2.py`). It is mounted by exactly one
``include_router`` line (plus one validation-handler line, mirroring the
pattern already used for the v2 router) in ``main.py``.

DESIGN
------
* Translation, not a second brain: this endpoint drives the SAME internal
  runtime primitives ``isola_bridge.py`` already uses —
  ``ensure_primary_platform_session`` + ``enqueue_chat_runtime`` +
  ``open_run_state_reader`` — via the SAME synchronous poll-then-read pattern.
  It does not duplicate agent reasoning or create a second runtime.
* Per-turn tool governance (`dec-clawith-structured-per-turn-tool-governance
  -2026-08-02`): ``allowed_tools`` is enforced, not merely accepted. The
  effective set for this turn is the intersection of the exact names the
  caller supplied and the names the designated Agent is already
  persistently configured with (`_resolve_effective_tool_names`) — any
  requested name outside that intersection is rejected with 400 BEFORE the
  idempotency claim, the Agent's own tool configuration is never mutated,
  and the effective set is threaded through `enqueue_chat_runtime` into the
  one Run's immutable input snapshot only, where both the model-offering
  step and the tool-execution step re-check it independently (see
  `model_step_service._allowed_tool_names_override` /
  `tool_step_service._allowed_tool_names_override`). Clawith still gains no
  NEW authority from this envelope — the effective set can only narrow what
  the Agent already had, never widen it.
* Tenant is DERIVED, never trusted: ``designated_agent_id`` is resolved
  against Clawith's own ``agents`` table and ITS ``tenant_id`` is
  authoritative. The request's own ``tenant_id`` field is compared against
  that derived value and a mismatch is rejected — it is never used as an
  authorization assertion on its own.
* Durable correlation-keyed idempotency uses a DEDICATED table,
  ``isola_structured_bridge_requests`` (added by
  ``202608021900_add_structured_bridge_requests.py``, ratified by
  ``dec-clawith-structured-claim-isolation-dedicated-table-2026-08-02``), with
  its own ``uq_isola_structured_bridge_requests_tenant_correlation`` unique
  constraint on ``(tenant_id, correlation_id)``. This endpoint no longer
  shares any table, unique constraint or claim namespace with
  ``isola_bridge_v2.py``: there is no ``stable_request_id``, no
  ``idempotency_key`` and no ``"structured:"`` prefix anywhere in this file.
  A v2 caller has no request field that can select this relation, so
  cross-route preclaim and forged-digest join are unrepresentable by
  construction rather than merely rejected — see
  `defect-clawith-structured-bridge-v2-request-table-collision-2026-08-02`
  and `evidence-clawith-cross-route-claim-isolation-design-audit-2026-08-02`
  for the audited attacks this removes.
* ``tool_policy_digest`` is a first-class, ``NOT NULL``, shape-``CHECK``ed
  column on the claim row (not a key inside the general-purpose
  ``metadata_labels`` JSONB, which a shared table's other caller could
  forge). The digest comparison below is fail-closed defence in depth on
  top of that database-enforced invariant, not the only line of defence.
* Claim BEFORE enqueue, not after: unlike ``isola_bridge_v2.submit_request``
  (which checks-then-enqueues-then-inserts inside one transaction — a window
  where two concurrent identical submissions could each pass the check and
  each enqueue a run before either commits), this endpoint performs the
  ``INSERT ... ON CONFLICT DO NOTHING`` claim FIRST, in its own short
  transaction, and only the request that wins the claim proceeds to
  ``enqueue_chat_runtime``. A request that loses the claim never enqueues —
  it waits on the winner's row (durable, restart-safe, concurrent-safe,
  process-independent) and returns/waits for that run's result instead.
* Timeout is HTTP 504 (transport-level, retryable per
  `dec-clawith-structured-bridge-versioned-endpoint-2026-08-02`'s retry
  classification), not a structured envelope. The claimed row stays in
  `running` state, so a retried request with the identical `correlation_id`
  finds the SAME claim and waits for the SAME run rather than starting a
  second one.

TOOL CAPABILITY CARRIER (`dec-revenue-mcp-turn-capability-no-clawith-core
-fork-2026-08-06`)
--------------------------------------------------------------------------
An optional `tool_capabilities` list carries Foundation-minted, opaque,
single-use, per-turn operation capabilities (NOT identity). Each entry is
bound 1:1 to a tool name that is already a member of THIS turn's
`effective_tool_names` — the same intersection `allowed_tools` above
already computes and the same set `tool_step_service` already enforces
unconditionally at execution time. A capability can therefore only ever
NARROW what this turn's model is told to attach where; it cannot expand
the effective tool set, select a tenant/agent/actor, or move to a
different tool. The shared `X-Isola-Secret` bearer above remains
transport/service authentication only and is never treated as Atlas (or
any other) identity — capabilities are the only per-operation authority
this file forwards.

Delivered through the SAME `runtime_instruction` channel the identity
directive above already uses (no new seam, no core-runtime file touched):
the model is told, per capability, to attach the exact opaque value as a
named argument on that exact tool call and never disclose it elsewhere.
Because that is a prompt instruction and not, by itself, a technical
control, two independent non-prompt controls also apply: (1) a capability
whose tool name is not already in `effective_tool_names` is rejected
before the idempotency claim, so it can never reach the model regardless
of what the model does; (2) any raw capability value that reappears
verbatim in the customer-facing reply is redacted IN THE STORED MESSAGE
before this route settles the claim, independent of model cooperation.

WHERE THE RAW VALUE ACTUALLY GOES (corrected 2026-08-06 after the
independent exact-head review of the first head, `dec-pr42-capability
-sanitizer-and-replay-binding-2026-08-06`). The earlier claim that this
file "never persists it outside the existing `runtime_instruction`
snapshot" described only what THIS file writes. It was not an accurate
description of the delivered mechanism, because the whole point of the
directive is that the model then attaches the value as a tool argument.
The accurate contract is:

* the raw value MAY be present in protected runtime/model/tool-call
  context — the `runtime_instruction` snapshot on the run's initial
  input, the model request, and the model's own tool call. That is the
  tolerated surface named in the governing decision, and it is protected
  by short expiry plus Foundation's one-use atomic claim;
* the SHARED tool-argument sanitizer removes it from every ordinary
  projection: `AgentToolExecution.sanitized_arguments`, every
  `AgentRunEvent.payload["args"]` insertion, and therefore the web-chat
  run-event stream. That is why `operationcapability` is a member of
  `_SENSITIVE_KEYS` in `app/services/agent_runtime/tool_execution.py` —
  the single, separately authorized shared-runtime change;
* a settled customer reply is redacted BEFORE it is persisted and before
  the claim is marked completed, so the stored reply is already safe at
  rest and an idempotent replay never has to resupply the capability
  list in order to be safe;
* the policy/idempotency digest stores only a one-way, domain-separated
  SHA-256 fingerprint of the raw value — never the value itself;
* Foundation remains the sole authority that validates, claims and
  consumes the opaque value. This file never inspects, decodes or
  interprets it, and adds no table, column or migration.

KNOWN RESIDUAL, deliberately NOT fixed here because it is outside the
narrow shared-runtime authorization: if the model disobeys the directive
and echoes a raw capability into its assistant text, that text also
reaches `assistant_progress` run-event payloads written by
`checkpoint_side_effects.py` and `tool_step_service.py`. Redacting those
would require editing further core runtime files. Recorded rather than
silently widened — see the Port defect record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.isola_bridge import _authorize, _stable_user_id
from app.database import async_session
from app.models.agent import Agent, AgentUserOnboarding
from app.models.llm import LLMModel
from app.models.user import User
from app.services.agent_runtime.chat_intake import (
    ChatRuntimeIntakeError,
    enqueue_chat_runtime,
)
from app.services.agent_runtime.run_owned_reply import (
    RunOwnedReply,
    RunOwnedReplyError,
    read_run_owned_reply,
)
from app.services.agent_runtime.run_state_reader import (
    RunStateReadError,
    open_run_state_reader,
)
from app.services.agent_tools import get_runtime_agent_tools_for_llm
from app.services.chat_session_service import ensure_primary_platform_session

router = APIRouter(prefix="/isola/bridge/structured", tags=["isola-bridge-structured"])

# ── Wire contract ────────────────────────────────────────────────────────────
# Mirrors epicdm/Isola-Foundation artifacts/isola/lib/clawith/contract.ts
# (dec-foundation-clawith-structured-response-contract-2026-07-29). Field
# names, bounds and the ownership/escalation/qualification vocabularies are
# copied from that file, not re-derived, so this endpoint accepts exactly
# what Foundation's request builder emits.

SCHEMA_VERSION = "1.0.0"
SUPPORTED_SCHEMA_MAJOR = 1

_OWNERSHIP_STATES = frozenset(
    {"AI_OWNED", "HUMAN_REQUESTED", "HUMAN_OWNED", "HANDING_BACK", "AI_RESUMED"}
)

_MAX_HISTORY_TURNS = 20
_MAX_KNOWLEDGE_SCOPE_IDS = 16
_MAX_ALLOWED_TOOLS = 24
_MAX_RESPONSE_DEADLINE_MS = 90_000

# Tool capability carrier (dec-revenue-mcp-turn-capability-no-clawith-core
# -fork-2026-08-06). CAPABILITY_ARGUMENT_NAME is the Foundation-side MCP
# tool-schema argument name this file instructs the model to attach an
# opaque capability value to — Foundation's provider-side tool schemas for
# `isola_revenue_customer_context_get` / `isola_revenue_followup_set` MUST
# accept an argument with this exact name for the carrier to reach it.
_MAX_TOOL_CAPABILITIES = 8
CAPABILITY_CONTRACT_VERSION = "1.0.0"
CAPABILITY_ARGUMENT_NAME = "operation_capability"
# Fixed domain separator for the one-way capability fingerprint that binds
# a capability set into the idempotency/policy digest without ever storing
# the raw value (dec-pr42-capability-sanitizer-and-replay-binding
# -2026-08-06). Kept as bytes with an explicit NUL terminator so the
# separator can never run into attacker-influenced input.
_CAPABILITY_FINGERPRINT_DOMAIN = b"isola-operation-capability-v1\x00"
# Marker written over a raw capability that the model echoed into the
# customer-facing reply. Must match `_redact_capabilities`.
_CAPABILITY_REDACTION_MARKER = "[redacted-capability]"
# Same poll shape as isola_bridge.py's synchronous /message route, so both
# routes give Foundation the same latency profile for the same underlying
# work. Both stay comfortably under Foundation's DEFAULT_RESPONSE_DEADLINE_MS
# (45_000ms).
_POLL_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.75
_MESSAGE_GRACE_S = 5.0
# How long a request that LOST the idempotency claim will wait for the
# winner to finish enqueuing (populate session_id/run_id) before giving up
# with a retryable 504. The claim row itself is untouched by this wait, so a
# retry after this window still finds the same claim.
_CLAIM_POPULATE_TIMEOUT_S = 8.0
_CLAIM_POPULATE_INTERVAL_S = 0.5

# How long a structured claim row is considered live before it is eligible
# for some future cleanup mechanism. Recorded at claim time as
# `accepted_at + this interval`. NO cleanup or deletion job exists in this
# slice (matching the legacy table, which also has none) — expires_at is a
# durable timestamp only; rows do not disappear on their own. Introducing
# automatic expiry/deletion is a separate, explicitly out-of-scope decision.
_STRUCTURED_RETENTION_H = int(os.environ.get("ISOLA_BRIDGE_STRUCTURED_RETENTION_HOURS", "24"))

_SETTLED_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "waiting_user", "waiting_external", "waiting_agent"}
)
_TERMINAL_CLAIM_STATES = frozenset({"completed", "failed", "cancelled", "expired", "rejected"})


class ClawithHistoryTurnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str


class ClawithToolDefinitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str
    arguments: list[str] = Field(default_factory=list)
    required: list[str] | None = None
    mutating: bool


class ToolCapabilityIn(BaseModel):
    """One Foundation-minted, opaque, single-use, tool-bound turn
    capability (dec-revenue-mcp-turn-capability-no-clawith-core-fork
    -2026-08-06). Opaque to Clawith: this repo never inspects, decodes,
    stores or interprets `capability`'s contents — it only binds the value
    1:1 to an already-effective tool name and forwards it. `extra="forbid"`
    means a caller-supplied `agentRef`, `actorType`, tenant id or any other
    field is a 422 validation error, not a silently-ignored extra key —
    this model has no field through which caller-selected identity or
    authority can enter."""

    model_config = ConfigDict(extra="forbid")
    tool_name: str = Field(min_length=1, max_length=200)
    capability: str = Field(min_length=1, max_length=4_096)
    contract_version: str = Field(min_length=1, max_length=40)
    # Display/validation only, never authoritative — Foundation alone
    # enforces real expiry at claim time. A blatantly pre-expired value is
    # rejected here purely to avoid wasting a Run on a request that can
    # never be honored.
    expires_at: datetime | None = None

    @field_validator("tool_name", "capability", "contract_version")
    @classmethod
    def _strip_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class StructuredBridgeMessageIn(BaseModel):
    """The full ratified structured envelope. Unknown fields are refused —
    an undeclared field is an unbounded authority channel, same posture as
    isola_bridge_v2.BridgeRequestIn."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    # NOT an authorization assertion — see _resolve_tenant below.
    tenant_id: str = Field(min_length=1)
    business_id: str = Field(min_length=1)
    chatwoot_account_id: str = Field(min_length=1)
    inbox_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    inbound_message_id: str = Field(min_length=1)
    contact_ref: str = Field(min_length=1)
    normalized_customer_message: str = Field(min_length=1, max_length=8_000)
    bounded_conversation_history: list[ClawithHistoryTurnIn] = Field(default_factory=list)
    designated_agent_id: uuid.UUID
    knowledge_scope_ids: list[str] = Field(default_factory=list)
    allowed_tools: list[ClawithToolDefinitionIn] = Field(default_factory=list)
    # Optional. Each entry must name a tool already present in this turn's
    # effective_tool_names (validated in _validate_tool_capabilities, AFTER
    # _resolve_effective_tool_names, BEFORE the idempotency claim) — a
    # capability can therefore never expand what this turn may call.
    tool_capabilities: list[ToolCapabilityIn] = Field(default_factory=list)
    ownership_state: str
    # The per-turn operation boundary. Stored directly in
    # isola_structured_bridge_requests.correlation_id, the unique-constraint
    # column (paired with tenant_id) on this route's own dedicated table —
    # no prefix or namespacing needed. Bounds mirror the column's own
    # ck_isola_structured_bridge_requests_correlation_len CHECK
    # (1..180 chars).
    correlation_id: str = Field(min_length=1, max_length=180)
    locale: str = Field(min_length=1)
    timezone: str = Field(min_length=1)
    response_deadline_ms: int = Field(gt=0, le=_MAX_RESPONSE_DEADLINE_MS)

    @field_validator("correlation_id", "tenant_id")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("ownership_state")
    @classmethod
    def _known_ownership_state(cls, v: str) -> str:
        if v not in _OWNERSHIP_STATES:
            raise ValueError(f"unknown ownership_state {v!r}")
        return v

    @field_validator("bounded_conversation_history")
    @classmethod
    def _bound_history(cls, v: list[ClawithHistoryTurnIn]) -> list[ClawithHistoryTurnIn]:
        if len(v) > _MAX_HISTORY_TURNS:
            raise ValueError(f"bounded_conversation_history exceeds {_MAX_HISTORY_TURNS} turns")
        return v

    @field_validator("knowledge_scope_ids")
    @classmethod
    def _bound_knowledge_scope(cls, v: list[str]) -> list[str]:
        if len(v) > _MAX_KNOWLEDGE_SCOPE_IDS:
            raise ValueError(f"knowledge_scope_ids exceeds {_MAX_KNOWLEDGE_SCOPE_IDS} ids")
        return v

    @field_validator("allowed_tools")
    @classmethod
    def _bound_allowed_tools(cls, v: list[ClawithToolDefinitionIn]) -> list[ClawithToolDefinitionIn]:
        if len(v) > _MAX_ALLOWED_TOOLS:
            raise ValueError(f"allowed_tools exceeds {_MAX_ALLOWED_TOOLS} tools")
        return v

    @field_validator("tool_capabilities")
    @classmethod
    def _bound_tool_capabilities(cls, v: list[ToolCapabilityIn]) -> list[ToolCapabilityIn]:
        if len(v) > _MAX_TOOL_CAPABILITIES:
            raise ValueError(f"tool_capabilities exceeds {_MAX_TOOL_CAPABILITIES} entries")
        return v


def _schema_major(raw: object) -> int | None:
    if not isinstance(raw, str) or not raw:
        return None
    head = raw.split(".", 1)[0]
    return int(head) if head.isdigit() else None


def _sanitized_validation_body(exc: ValidationError) -> dict:
    """Same posture as isola_bridge_v2's scoped 422 sanitizer, reimplemented
    locally (not shared) so that file's own exception-handler registration —
    and therefore its exact existing behaviour — is never touched."""
    detail = [
        {
            "code": "validation_error",
            "location": [str(part) for part in err.get("loc", ())],
            "type": err.get("type"),
        }
        for err in exc.errors()
    ]
    return {"error": "validation_error", "detail": detail}


# ── Durable correlation-keyed idempotency ───────────────────────────────────
# Dedicated table (202608021900_add_structured_bridge_requests.py), owned
# exclusively by this route. isola_bridge_v2.py cannot name this relation —
# there is no shared model, service or SQL between the two bridges.

_CLAIM_SQL = text(
    """
    INSERT INTO isola_structured_bridge_requests (
        id, tenant_id, correlation_id, agent_id, tool_policy_digest, schema_version,
        state, accepted_at, expires_at, external_conversation_id, contact_ref
    ) VALUES (
        :id, :tenant_id, :correlation_id, :agent_id, :tool_policy_digest, :schema_version,
        :state, :accepted_at, :expires_at, :external_conversation_id, :contact_ref
    )
    ON CONFLICT (tenant_id, correlation_id) DO NOTHING
    RETURNING id
    """
)

_SELECT_BY_CORRELATION_SQL = text(
    "SELECT * FROM isola_structured_bridge_requests WHERE tenant_id = :tenant_id "
    "AND correlation_id = :correlation_id"
)

_SELECT_BY_ID_SQL = text("SELECT * FROM isola_structured_bridge_requests WHERE id = :id")

_UPDATE_ENQUEUED_SQL = text(
    """
    UPDATE isola_structured_bridge_requests
       SET session_id = :session_id,
           run_id = :run_id,
           initiating_message_id = :initiating_message_id,
           started_at = :started_at,
           state = 'running'
     WHERE id = :id
    """
)

# Settle-time redaction of the stored customer-facing reply
# (dec-pr42-capability-sanitizer-and-replay-binding-2026-08-06). Narrowly
# scoped to the ONE assistant message this run is durably on record as
# having delivered — the id comes from `read_run_owned_reply`, which has
# already proved run/session/agent/user ownership for it. Run in the same
# transaction as, and BEFORE, the terminal claim update, so the claim can
# never be marked `completed` while the raw value is still at rest.
_REDACT_STORED_REPLY_SQL = text(
    """
    UPDATE chat_messages
       SET content = :content
     WHERE id = :id
       AND role = 'assistant'
       AND content = :expected_content
    """
)

_UPDATE_TERMINAL_SQL = text(
    """
    UPDATE isola_structured_bridge_requests
       SET state = :state,
           terminal_message_id = COALESCE(CAST(:terminal_message_id AS UUID), terminal_message_id),
           completed_at = COALESCE(:completed_at, completed_at),
           error_class = COALESCE(:error_class, error_class),
           last_checked_at = :now
     WHERE id = :id
    """
)


def _now() -> datetime:
    return datetime.now(UTC)


class _Row:
    def __init__(self, mapping):
        self._m = mapping

    def __getattr__(self, name):
        try:
            return self._m[name]
        except KeyError as exc:  # pragma: no cover - programming error
            raise AttributeError(name) from exc


async def _resolve_tenant(body: StructuredBridgeMessageIn):
    """Look up ONLY the scalar tenant_id column for the designated agent —
    deliberately not the full Agent ORM object, so nothing here can be
    accidentally used across a session boundary later. Returns
    (tenant_id, error_response)."""
    async with async_session() as db:
        tenant_id = (
            await db.execute(select(Agent.tenant_id).where(Agent.id == body.designated_agent_id))
        ).scalar_one_or_none()
    if tenant_id is None:
        return None, JSONResponse(status_code=404, content={"error": "agent_not_found"})
    if str(tenant_id).strip().lower() != body.tenant_id.strip().lower():
        return (
            None,
            JSONResponse(
                status_code=409,
                content={
                    "error": "tenant_mismatch",
                    "detail": "request tenant_id does not match the designated agent's tenant",
                },
            ),
        )
    return tenant_id, None


async def _resolve_effective_tool_names(
    agent_id: uuid.UUID, requested: frozenset[str]
) -> tuple[list[str], frozenset[str]]:
    """Per-turn `allowed_tools` governance
    (`dec-clawith-structured-per-turn-tool-governance-2026-08-02`): the
    effective set for this turn is the intersection of the exact names the
    caller supplied with the names the designated Agent is ALREADY
    persistently configured with — never a superset, and never touching
    that persistent configuration. Uses the SAME catalogue function the
    Runtime itself resolves tools from (`get_runtime_agent_tools_for_llm`),
    so "supported for this agent" here means exactly what it will mean when
    the Run actually executes. Returns (effective_names, unsupported_names);
    a non-empty `unsupported_names` means the caller asked for at least one
    tool this Agent/tenant does not have — the caller must reject before
    any durable side effect."""
    configured = await get_runtime_agent_tools_for_llm(agent_id)
    configured_names = {
        name
        for name in (
            tool.get("function", {}).get("name") if isinstance(tool, dict) else None
            for tool in configured
        )
        if isinstance(name, str) and name
    }
    unsupported = requested - configured_names
    effective = sorted(requested & configured_names)
    return effective, unsupported


def _capability_fingerprint(raw_capability: str) -> str:
    """One-way, domain-separated SHA-256 fingerprint of a raw capability
    (dec-pr42-capability-sanitizer-and-replay-binding-2026-08-06).

    The digest identity below has to be able to say "the same capability"
    without the raw value ever being stored, logged or persisted. The
    fixed domain separator keeps this hash space disjoint from any other
    SHA-256 use in the codebase, so a value fingerprinted here can never
    be mistaken for — or replayed against — a hash produced elsewhere.

    This supplies REPLAY IDENTITY ONLY. It is not validation: Foundation
    remains authoritative for the token's authenticity, expiry, tenant,
    agent, tool and object scope and its atomic one-use claim.
    """
    return hashlib.sha256(
        _CAPABILITY_FINGERPRINT_DOMAIN + raw_capability.encode("utf-8")
    ).hexdigest()


def _capability_binding(capabilities: list[ToolCapabilityIn]) -> list[list[str]]:
    """Canonical, raw-free description of the turn's capability set, in a
    stable order, for inclusion in the policy/idempotency digest.

    Per capability: exact tool name, contract version, the normalized
    expiry metadata this bridge actually acted on (empty string when
    absent, so "no expiry supplied" and "expiry supplied" are
    distinguishable), and the one-way fingerprint. Sorted by exact tool
    name — duplicates are already rejected upstream by
    `_validate_tool_capabilities`, so tool name is a total order here.
    """
    return sorted(
        [
            cap.tool_name,
            cap.contract_version,
            cap.expires_at.astimezone(UTC).isoformat() if cap.expires_at is not None else "",
            _capability_fingerprint(cap.capability),
        ]
        for cap in capabilities
    )


def _tool_policy_digest(
    effective_tool_names: list[str],
    capabilities: list[ToolCapabilityIn] | None = None,
) -> str:
    """A short, order-independent fingerprint of one turn's effective tool
    set AND its capability binding, stored on the idempotency claim row's
    first-class, NOT NULL, shape-CHECKed `tool_policy_digest` column. Lets
    a request that loses the claim detect whether it is a genuine retry of
    the SAME turn-defining policy, or a `correlation_id` reused with a
    DIFFERENT `allowed_tools` or a DIFFERENT capability binding — which
    must never silently inherit another policy's already-produced result.

    Canonicalized via JSON array serialization, NOT a naive comma-join:
    `Tool.name` is an unconstrained `String(100)` (app/models/tool.py),
    so a tool literally named e.g. "a,b" makes a naive `",".join(...)` of
    `["a,b", "c"]` collide with `["a", "b,c"]`. JSON's per-element quoting
    disambiguates the boundary in every such case.

    An omitted capability list is a DIFFERENT policy from a supplied one
    and must conflict rather than silently replay
    (dec-pr42-capability-sanitizer-and-replay-binding-2026-08-06) — that
    is the whole point of the second element. Only raw-free fingerprints
    enter the hash; the raw value never does.
    """
    canonical = json.dumps(
        [sorted(effective_tool_names), _capability_binding(capabilities or [])],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _validate_tool_capabilities(
    effective_tool_names: frozenset[str],
    capabilities: list[ToolCapabilityIn],
) -> JSONResponse | None:
    """Fail-closed, schema-level binding between a per-turn capability and
    the already-enforced effective tool set
    (dec-revenue-mcp-turn-capability-no-clawith-core-fork-2026-08-06).

    Runs strictly AFTER `_resolve_effective_tool_names` and strictly
    BEFORE the idempotency claim — same posture as the `unsupported_tool`
    check above: a malformed capability request is a request-shape
    defect, not a Run outcome, and must create no durable row, no Run, no
    tool execution.

    A capability can only ever NARROW what this turn's model is told to
    attach where — it can never widen `effective_tool_names`, select an
    agent/tenant/actor, or substitute for one tool at another. Foundation
    remains the sole authority that actually validates and consumes the
    opaque value; this only proves the REQUEST is well-formed and
    internally consistent before any capability reaches the model.

    Error `detail` lists carry tool names only, never a `capability`
    value — see the module redaction discipline.
    """
    seen_tool_names: set[str] = set()
    duplicates: set[str] = set()
    unavailable: set[str] = set()
    bad_versions: set[str] = set()
    for cap in capabilities:
        if cap.tool_name in seen_tool_names:
            duplicates.add(cap.tool_name)
            continue
        seen_tool_names.add(cap.tool_name)
        if cap.tool_name not in effective_tool_names:
            unavailable.add(cap.tool_name)
            continue
        if cap.contract_version != CAPABILITY_CONTRACT_VERSION:
            bad_versions.add(cap.tool_name)
        if cap.expires_at is not None and cap.expires_at <= _now():
            return JSONResponse(
                status_code=400,
                content={"error": "expired_tool_capability", "detail": [cap.tool_name]},
            )
    if duplicates:
        return JSONResponse(
            status_code=400,
            content={"error": "duplicate_tool_capability", "detail": sorted(duplicates)},
        )
    if unavailable:
        return JSONResponse(
            status_code=400,
            content={"error": "capability_for_unavailable_tool", "detail": sorted(unavailable)},
        )
    if bad_versions:
        return JSONResponse(
            status_code=400,
            content={
                "error": "unsupported_capability_contract_version",
                "detail": sorted(bad_versions),
                "supported_contract_version": CAPABILITY_CONTRACT_VERSION,
            },
        )
    return None


def _capability_directive(capabilities: list[ToolCapabilityIn]) -> str:
    """Presents each capability to the model as an opaque per-tool
    operation token it must attach verbatim and never disclose. This is a
    prompt instruction, NOT the only control: a capability can only ever
    be built for a tool name already inside `effective_tool_names`
    (enforced in `_validate_tool_capabilities` above, and independently,
    unconditionally, re-enforced downstream by `tool_step_service`'s own
    allow-list intersection — unchanged by this file). This directive can
    influence only WHAT ARGUMENT VALUE the model attaches to a call the
    runtime was already going to allow; it cannot itself authorize a
    call, widen the tool set, or select an identity.
    """
    if not capabilities:
        return ""
    intro = (
        "The following tools have been pre-authorized for this exact turn "
        "with a single-use operation capability. When, and only when, you "
        "call one of these tools, you MUST include an argument named "
        f'"{CAPABILITY_ARGUMENT_NAME}" set to EXACTLY the value shown '
        "below for that tool, with no modification. Never repeat, quote, "
        "paraphrase, translate, log, or otherwise disclose any of these "
        "values anywhere else, including in your reply to the customer or "
        "in any other tool call."
    )
    lines = [intro]
    for cap in capabilities:
        lines.append(f'- tool `{cap.tool_name}`: {CAPABILITY_ARGUMENT_NAME}="{cap.capability}"')
    return "\n".join(lines)


def _redact_capabilities(reply_text: str | None, capabilities: list[ToolCapabilityIn]) -> str | None:
    """Defence in depth against a model that disobeys the directive above
    and echoes a raw capability value into its customer-facing reply. The
    directive is the primary control; this is a second, independent,
    non-LLM control that requires no model cooperation. Only ever narrows
    (redacts) text already produced — never used to authorize or alter
    behaviour.

    Applied AT SETTLE TIME, to the stored `chat_messages` row, before the
    claim is marked completed — not only on the way out of this route
    (dec-pr42-capability-sanitizer-and-replay-binding-2026-08-06). The
    first head redacted only the response, which left the raw value at
    rest and let a replay that omitted `tool_capabilities` return it
    verbatim. Both halves of that gap are closed: the stored text is safe,
    and a replay with a different capability binding is refused by the
    digest check before it can read anything."""
    if reply_text is None or not capabilities:
        return reply_text
    redacted = reply_text
    for cap in capabilities:
        if cap.capability and cap.capability in redacted:
            redacted = redacted.replace(cap.capability, _CAPABILITY_REDACTION_MARKER)
    return redacted


async def _claim(
    *,
    tenant_id,
    correlation_id: str,
    body: StructuredBridgeMessageIn,
    tool_policy_digest: str,
):
    """Atomic claim attempt against the dedicated
    isola_structured_bridge_requests table, arbitrated entirely by its own
    `uq_isola_structured_bridge_requests_tenant_correlation` unique
    constraint on (tenant_id, correlation_id) — no other table, route or
    caller can contend for this key. Returns (won: bool, row: _Row)."""
    claim_id = uuid.uuid4()
    accepted_at = _now()
    expires_at = accepted_at + timedelta(hours=_STRUCTURED_RETENTION_H)
    async with async_session() as db:
        async with db.begin():
            inserted = (
                await db.execute(
                    _CLAIM_SQL,
                    {
                        "id": str(claim_id),
                        "tenant_id": str(tenant_id),
                        "correlation_id": correlation_id,
                        "agent_id": str(body.designated_agent_id),
                        "tool_policy_digest": tool_policy_digest,
                        "schema_version": body.schema_version,
                        "state": "accepted",
                        "accepted_at": accepted_at,
                        "expires_at": expires_at,
                        "external_conversation_id": body.conversation_id,
                        "contact_ref": body.contact_ref,
                    },
                )
            ).first()
        row = (
            await db.execute(
                _SELECT_BY_CORRELATION_SQL,
                {"tenant_id": str(tenant_id), "correlation_id": correlation_id},
            )
        ).mappings().first()
    won = inserted is not None
    return won, (_Row(row) if row is not None else None)


async def _wait_for_claim_population(row_id):
    """Loser path: poll the claimed row until the winner has populated
    session_id/run_id, or until it reaches a terminal state, or until the
    grace window elapses. Never enqueues anything."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _CLAIM_POPULATE_TIMEOUT_S
    while True:
        async with async_session() as db:
            row = (await db.execute(_SELECT_BY_ID_SQL, {"id": str(row_id)})).mappings().first()
        r = _Row(row)
        if r.state in _TERMINAL_CLAIM_STATES:
            return r
        if r.session_id is not None and r.run_id is not None:
            return r
        if loop.time() >= deadline:
            return r
        await asyncio.sleep(_CLAIM_POPULATE_INTERVAL_S)


async def _poll_and_read(tenant_id, run_id, session_id, agent_id, user_id):
    """Same settle-then-read shape as isola_bridge.bridge_message. Returns
    (settled: bool, final_status: str | None, reply: RunOwnedReply | None,
    error_class: str | None). ``error_class`` is set only when a delivery
    receipt existed but failed run-ownership validation -- fail closed, never
    fall back to scanning the session for a different assistant message."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _POLL_TIMEOUT_S
    final_status: str | None = None

    async with async_session() as poll_db:
        async with open_run_state_reader(poll_db) as reader:
            settled = False
            while True:
                poll_db.expire_all()
                try:
                    view = await reader.get_run_state(tenant_id, run_id)
                    final_status = view.execution_status
                except RunStateReadError:
                    final_status = None
                if final_status in _SETTLED_STATUSES:
                    settled = True
                    break
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(_POLL_INTERVAL_S)

            if not settled:
                return False, final_status, None, None

            reply: RunOwnedReply | None = None
            error_class: str | None = None
            grace_deadline = loop.time() + _MESSAGE_GRACE_S
            while True:
                poll_db.expire_all()
                try:
                    reply = await read_run_owned_reply(
                        poll_db,
                        tenant_id=tenant_id,
                        run_id=run_id,
                        session_id=session_id,
                        agent_id=agent_id,
                        user_id=user_id,
                    )
                except RunOwnedReplyError:
                    error_class = "run_owned_reply_scope_mismatch"
                    break
                if reply is not None or loop.time() >= grace_deadline:
                    break
                await asyncio.sleep(_POLL_INTERVAL_S)

    return True, final_status, reply, error_class


def _envelope(
    *,
    agent_id,
    session_id,
    correlation_id: str,
    tenant_id,
    customer_reply: str | None,
    confidence: float,
    escalation_requested: bool = False,
    escalation_reason_code: str | None = None,
    escalation_explanation: str | None = None,
    latency_ms: int | None = None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "correlation_id": correlation_id,
        "tenant_id": str(tenant_id),
        "customer_reply": customer_reply,
        "intent": None,
        # Dark-launch approximation, not a qualification judgement: 1.0 when
        # a settled reply was produced, 0.0 when the turn had to escalate
        # instead. Real confidence scoring is out of scope for this slice.
        "confidence": confidence,
        "qualification_state": "unknown",
        "knowledge_references": [],
        # No tool-request translation in this dark-launch slice — Clawith's
        # own internal tool calls stay internal; nothing here claims a tool
        # succeeded, and nothing here asks Foundation to authorise one.
        "tool_requests": [],
        "escalation": {
            "requested": escalation_requested,
            "reason_code": escalation_reason_code,
            "explanation": escalation_explanation,
            "urgency": "normal" if escalation_requested else None,
            "required_team": None,
            "customer_handoff_message": None,
        },
        "missing_information": [],
        "follow_up_required": False,
        "usage": {"latency_ms": latency_ms} if latency_ms is not None else None,
    }


async def _mark_failed(request_id, error_class: str) -> None:
    async with async_session() as db:
        await db.execute(
            _UPDATE_TERMINAL_SQL,
            {
                "id": str(request_id),
                "state": "failed",
                "terminal_message_id": None,
                "completed_at": _now(),
                "error_class": error_class,
                "now": _now(),
            },
        )
        await db.commit()


@router.post("/message")
async def bridge_structured_message(
    request: Request,
    x_isola_secret: str | None = Header(default=None, alias="X-Isola-Secret"),
) -> JSONResponse:
    denied = _authorize(x_isola_secret)
    if denied is not None:
        return denied

    try:
        raw = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "malformed_json"})

    if not isinstance(raw, dict):
        return JSONResponse(status_code=400, content={"error": "malformed_json"})

    major = _schema_major(raw.get("schema_version"))
    if major != SUPPORTED_SCHEMA_MAJOR:
        return JSONResponse(
            status_code=400,
            content={
                "error": "unsupported_schema_version",
                "schema_version": raw.get("schema_version"),
                "supported_major": SUPPORTED_SCHEMA_MAJOR,
            },
        )

    # Manual validation (not a FastAPI body-param dependency): this keeps
    # RequestValidationError out of the picture entirely, so it can never
    # collide with, or silently replace, isola_bridge_v2's own scoped 422
    # exception-handler registration for the SAME exception class.
    try:
        body = StructuredBridgeMessageIn.model_validate(raw)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content=_sanitized_validation_body(exc))

    agent_id = body.designated_agent_id
    correlation_id = body.correlation_id
    accepted_at = _now()

    tenant_id, err = await _resolve_tenant(body)
    if err is not None:
        return err

    requested_tool_names = frozenset(tool.name for tool in body.allowed_tools)
    effective_tool_names, unsupported_tool_names = await _resolve_effective_tool_names(
        agent_id, requested_tool_names
    )
    if unsupported_tool_names:
        # Reject BEFORE the idempotency claim: an unsupported tool name is a
        # request-shape defect, not a Run outcome, so it must create no
        # durable isola_structured_bridge_requests row, no reasoning Run,
        # and no tool execution — see
        # dec-clawith-structured-per-turn-tool-governance-2026-08-02.
        return JSONResponse(
            status_code=400,
            content={
                "error": "unsupported_tool",
                "detail": sorted(unsupported_tool_names),
            },
        )

    cap_error = _validate_tool_capabilities(frozenset(effective_tool_names), body.tool_capabilities)
    if cap_error is not None:
        # Same posture as the unsupported_tool rejection above: a
        # request-shape defect, rejected before any durable row is
        # created.
        return cap_error

    # Capability-aware: a retry that omits, substitutes, re-targets or
    # re-versions a capability computes a DIFFERENT digest and is refused
    # as `correlation_id_policy_mismatch` below, rather than inheriting
    # the original turn's already-produced result. Only raw-free
    # fingerprints enter the digest
    # (dec-pr42-capability-sanitizer-and-replay-binding-2026-08-06).
    tool_policy_digest = _tool_policy_digest(effective_tool_names, body.tool_capabilities)

    won, claim_row = await _claim(
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        body=body,
        tool_policy_digest=tool_policy_digest,
    )
    if claim_row is None:  # pragma: no cover - defensive; claim always leaves a row
        return JSONResponse(status_code=500, content={"error": "claim_failed"})

    # Fail-closed claim-row policy check, kept as defence in depth on top of
    # the database invariant: tool_policy_digest is a first-class NOT NULL,
    # shape-CHECKed column on isola_structured_bridge_requests (never a key
    # inside caller-reachable metadata), so every row this SELECT can return
    # already carries a valid digest. A missing/unexpected value is still
    # treated as a MISMATCH, never as "compatible" — the winner's own
    # just-inserted row always carries the digest it just wrote, so this is
    # a no-op on the winning path and only ever rejects on the losing path.
    #
    # This table is dedicated to this route (ratified by
    # dec-clawith-structured-claim-isolation-dedicated-table-2026-08-02,
    # implementing dec-clawith-structured-v2-claim-isolation-gate-2026-08-02
    # and closing defect-clawith-structured-bridge-v2-request-table-collision
    # -2026-08-02): isola_bridge_v2.py cannot write, read or contend for a
    # row here — there is no shared table, unique constraint or claim
    # namespace between the two bridges.
    # Correlation-identity binding (`dec-clawith-r1-1-run-state-fallback-and-
    # correlation-identity-fix-2026-08-03`, closing `defect-clawith-
    # structured-correlation-replay-request-identity-not-bound-2026-08-03`):
    # `correlation_id` is an idempotency key bound to the ORIGINAL claimed
    # request's identity, not merely to its tool policy. A same-tenant
    # request reusing this `correlation_id` but naming a different contact,
    # designated agent, or external conversation must never join, wait on,
    # or replay the existing claim -- doing so would let a mismatched
    # duplicate receive another recipient's real reply. `contact_ref`,
    # `agent_id` and `external_conversation_id` are already durably stored
    # on every row this SELECT can return (all three are non-optional on
    # the code path that inserts a row -- see `_claim` above), so this is a
    # database-backed comparison, not a new migration. On the WINNING path
    # `claim_row` is the row this same request just inserted with its own
    # values, so the comparison is always a no-op there; it only ever
    # rejects on the LOSING path. Checked before any join, wait, replay,
    # terminal-result or winner-reply below -- no claim mutation, no new
    # run/session/user/message/tool/audit row on mismatch.
    stored_contact_ref = getattr(claim_row, "contact_ref", None)
    stored_agent_id = getattr(claim_row, "agent_id", None)
    stored_external_conversation_id = getattr(claim_row, "external_conversation_id", None)
    if (
        stored_contact_ref != body.contact_ref
        or str(stored_agent_id) != str(body.designated_agent_id)
        or stored_external_conversation_id != body.conversation_id
    ):
        return JSONResponse(
            status_code=409,
            content={
                "error": "correlation_id_request_mismatch",
                "detail": (
                    "this correlation_id is already claimed by a request "
                    "naming a different contact_ref, designated_agent_id, "
                    "or external_conversation_id"
                ),
            },
        )

    stored_digest = getattr(claim_row, "tool_policy_digest", None)
    if stored_digest != tool_policy_digest:
        # This correlation_id is already claimed under a DIFFERENT (or
        # unrecorded) allowed_tools policy than this request just resolved.
        # Whatever Run that claim is attached to may have been authorized
        # under a different effective tool set — this request must never
        # silently inherit its result, so it is rejected rather than
        # joining the existing claim. A genuine retry of the SAME logical
        # turn always recomputes the SAME digest and is unaffected.
        return JSONResponse(
            status_code=409,
            content={
                "error": "correlation_id_policy_mismatch",
                "detail": (
                    "this correlation_id is already claimed under a "
                    "different or unrecorded allowed_tools policy"
                ),
            },
        )

    if won:
        try:
            async with async_session() as db:
                async with db.begin():
                    agent = await db.get(Agent, agent_id)
                    if agent is None:  # pragma: no cover - resolved moments ago
                        await _mark_failed(claim_row.id, "agent_not_found")
                        return JSONResponse(status_code=404, content={"error": "agent_not_found"})

                    model = None
                    if agent.primary_model_id is not None:
                        model = await db.get(LLMModel, agent.primary_model_id)
                    if model is None and agent.fallback_model_id is not None:
                        model = await db.get(LLMModel, agent.fallback_model_id)
                    if model is None:
                        await _mark_failed(claim_row.id, "agent_has_no_model")
                        return JSONResponse(status_code=409, content={"error": "agent_has_no_model"})

                    # contact_ref is the structured contract's opaque customer
                    # contact identity (dec-foundation-clawith-structured-response
                    # -contract-2026-07-29) — the same role `phone` plays for the
                    # legacy bridges. Reused here as the phone-shaped anchor
                    # _stable_user_id already expects, so the same phone/ref
                    # always maps to the same tenant-scoped User row.
                    phone = body.contact_ref.strip()
                    user_id = _stable_user_id(tenant_id, phone)
                    await db.execute(
                        pg_insert(User)
                        .values(
                            id=user_id,
                            tenant_id=tenant_id,
                            display_name=phone,
                            role="member",
                            is_active=True,
                            registration_source="isola_bridge_structured",
                        )
                        .on_conflict_do_nothing(index_elements=["id"])
                    )
                    user = await db.get(User, user_id)
                    if user is None:
                        await _mark_failed(claim_row.id, "user_provision_failed")
                        return JSONResponse(status_code=500, content={"error": "user_provision_failed"})

                    await db.execute(
                        pg_insert(AgentUserOnboarding)
                        .values(agent_id=agent.id, user_id=user.id, phase="completed")
                        .on_conflict_do_update(
                            index_elements=["agent_id", "user_id"], set_={"phase": "completed"}
                        )
                    )

                    session = await ensure_primary_platform_session(db, agent.id, user.id)

                    caller_directive = (
                        "The customer you are assisting is contacting via the Isola "
                        f"structured bridge. Their verified contact reference is {phone}. "
                        "Treat this as their confirmed identity."
                    )
                    capability_directive = _capability_directive(body.tool_capabilities)
                    if capability_directive:
                        caller_directive = f"{caller_directive}\n\n{capability_directive}"

                    async with open_run_state_reader(db) as reader:
                        intake = await enqueue_chat_runtime(
                            db,
                            agent=agent,
                            user=user,
                            session=session,
                            model=model,
                            content=body.normalized_customer_message,
                            source_channel="web",
                            runtime_instruction=caller_directive,
                            allowed_tool_names=effective_tool_names,
                            run_state_reader=reader,
                        )
                    if intake is None:
                        await _mark_failed(claim_row.id, "runtime_v2_disabled_for_agent")
                        return JSONResponse(
                            status_code=409, content={"error": "runtime_v2_disabled_for_agent"}
                        )

                    run_id = intake.handle.run_id
                    session_id = session.id
                    # Authoritative for run-ownership validation below: this
                    # request WON the claim, so its own designated_agent_id
                    # is exactly the agent the new run was enqueued under.
                    run_owner_agent_id = agent_id

                    await db.execute(
                        _UPDATE_ENQUEUED_SQL,
                        {
                            "id": str(claim_row.id),
                            "session_id": str(session_id),
                            "run_id": str(run_id),
                            "initiating_message_id": str(intake.message_id),
                            "started_at": accepted_at,
                        },
                    )
        except ChatRuntimeIntakeError as exc:
            await _mark_failed(claim_row.id, exc.code)
            return JSONResponse(status_code=409, content={"error": exc.code, "detail": str(exc)})
    else:
        # Idempotent replay or genuine concurrent duplicate: never enqueue a
        # second run. Wait for the winner (or a prior completion) instead.
        populated = await _wait_for_claim_population(claim_row.id)
        if populated.state == "completed" and populated.terminal_message_id is not None:
            # Replay of an already-completed claim: re-derive the reply
            # through the SAME run-owned receipt lookup used on the winner
            # path (never a bare ChatMessage fetch by the stored id), and
            # require it to agree with the id this claim already recorded.
            # Disagreement is a fail-safe signal, not a "use what we have"
            # fallback -- see RunOwnedReplyError's fail-closed contract.
            #
            # Derived from the CLAIMED row's own contact_ref/agent_id, never
            # this request's body.contact_ref/designated_agent_id: a
            # duplicate/retried request carrying a different (stale,
            # mistaken, or adversarial) contact_ref or agent must not be
            # able to fail run-ownership validation against the winner's
            # real run and then mark the SHARED claim row failed --
            # poisoning it for the original, legitimate caller.
            replay_user_id = _stable_user_id(tenant_id, populated.contact_ref.strip())
            replay_agent_id = uuid.UUID(str(populated.agent_id))
            replay_reply: RunOwnedReply | None = None
            async with async_session() as db:
                try:
                    replay_reply = await read_run_owned_reply(
                        db,
                        tenant_id=tenant_id,
                        run_id=uuid.UUID(str(populated.run_id)),
                        session_id=uuid.UUID(str(populated.session_id)),
                        agent_id=replay_agent_id,
                        user_id=replay_user_id,
                    )
                except RunOwnedReplyError:
                    replay_reply = None
            if replay_reply is None or str(replay_reply.message_id) != str(
                populated.terminal_message_id
            ):
                return JSONResponse(
                    status_code=200,
                    content=_envelope(
                        agent_id=agent_id,
                        session_id=populated.session_id,
                        correlation_id=correlation_id,
                        tenant_id=tenant_id,
                        customer_reply=None,
                        confidence=0.0,
                        escalation_requested=True,
                        escalation_reason_code="tool_failure",
                        escalation_explanation="stored replay result failed run-owned re-validation",
                    ),
                )
            # The stored reply was already redacted at settle time, so this
            # is a no-op on every well-formed replay. Kept as belt-and-
            # braces: it costs one substring scan and it is the only thing
            # standing between a customer and a raw capability if the
            # settle-time UPDATE were ever skipped. It is NO LONGER the
            # primary control, and it can no longer be disabled by a
            # retry that omits `tool_capabilities` -- such a retry now
            # fails the capability-aware digest check above and never
            # reaches here.
            return JSONResponse(
                status_code=200,
                content=_envelope(
                    agent_id=agent_id,
                    session_id=populated.session_id,
                    correlation_id=correlation_id,
                    tenant_id=tenant_id,
                    customer_reply=_redact_capabilities(replay_reply.content, body.tool_capabilities),
                    confidence=1.0,
                ),
            )
        if populated.state in ("failed", "rejected", "cancelled", "expired"):
            return JSONResponse(
                status_code=200,
                content=_envelope(
                    agent_id=agent_id,
                    session_id=populated.session_id or uuid.uuid4(),
                    correlation_id=correlation_id,
                    tenant_id=tenant_id,
                    customer_reply=None,
                    confidence=0.0,
                    escalation_requested=True,
                    escalation_reason_code="tool_failure",
                    escalation_explanation=f"prior attempt for this correlation_id ended in {populated.error_class}",
                ),
            )
        if populated.session_id is None or populated.run_id is None:
            # Winner has not finished enqueuing yet. Retryable: the claim
            # already exists, so a retry will not create a second run.
            return JSONResponse(
                status_code=504,
                content={"error": "claim_not_yet_populated", "retry_after_seconds": 3},
            )
        run_id = uuid.UUID(str(populated.run_id))
        session_id = uuid.UUID(str(populated.session_id))
        # Derived from the CLAIMED row's own contact_ref/agent_id, not this
        # request's body.contact_ref/designated_agent_id -- see the replay
        # branch above for why: a duplicate request with a mismatched
        # contact_ref or agent must not be able to fail run-ownership
        # validation against the winner's real run and mark the shared
        # claim failed, poisoning it for the original caller.
        user_id = _stable_user_id(tenant_id, populated.contact_ref.strip())
        run_owner_agent_id = uuid.UUID(str(populated.agent_id))

    settled, final_status, reply, poll_error_class = await _poll_and_read(
        tenant_id, run_id, session_id, run_owner_agent_id, user_id
    )

    if not settled:
        # Row stays 'running'. A retry with the same correlation_id lands in
        # the loser branch above and waits on the SAME run — no second
        # enqueue.
        return JSONResponse(
            status_code=504,
            content={
                "error": "runtime_timeout",
                "run_id": str(run_id),
                "session_id": str(session_id),
                "correlation_id": correlation_id,
            },
        )

    latency_ms = int((_now() - accepted_at).total_seconds() * 1000)

    if reply is not None:
        # terminal_message_id is read from the SAME run-owned reply object
        # whose text is returned below -- never a second, independent query.
        settled_reply = _redact_capabilities(reply.content, body.tool_capabilities)
        async with async_session() as db:
            if settled_reply != reply.content:
                # The model disobeyed the directive and echoed a raw
                # capability. Overwrite it AT REST first, in the same
                # transaction and strictly before the claim is marked
                # completed, so no replay -- and no other reader of
                # chat_messages -- can ever see the raw value. Guarded on
                # the exact expected content so a concurrent duplicate
                # that already redacted this row is a no-op rather than a
                # lost update. Every request that reaches this point
                # carries the SAME capability binding (the digest check
                # above rejects any other), so both racers compute the
                # identical replacement.
                await db.execute(
                    _REDACT_STORED_REPLY_SQL,
                    {
                        "id": str(reply.message_id),
                        "content": settled_reply,
                        "expected_content": reply.content,
                    },
                )
            await db.execute(
                _UPDATE_TERMINAL_SQL,
                {
                    "id": str(claim_row.id),
                    "state": "completed",
                    "terminal_message_id": str(reply.message_id),
                    "completed_at": _now(),
                    "error_class": None,
                    "now": _now(),
                },
            )
            await db.commit()
        return JSONResponse(
            status_code=200,
            content=_envelope(
                agent_id=agent_id,
                session_id=session_id,
                correlation_id=correlation_id,
                tenant_id=tenant_id,
                customer_reply=settled_reply,
                confidence=1.0,
                latency_ms=latency_ms,
            ),
        )

    # Settled but no assistant reply was produced -- either the run ended
    # with nothing to deliver (failed/cancelled), or a delivery receipt
    # existed but failed run-ownership validation (poll_error_class set).
    # Either way this is an escalation, never another turn's text.
    error_class = poll_error_class or f"run_{final_status}_no_reply"
    escalation_explanation = (
        "run-owned delivery receipt failed ownership/identity validation"
        if poll_error_class is not None
        else f"agent run ended in status {final_status!r} with no reply"
    )
    async with async_session() as db:
        await db.execute(
            _UPDATE_TERMINAL_SQL,
            {
                "id": str(claim_row.id),
                "state": "failed",
                "terminal_message_id": None,
                "completed_at": _now(),
                "error_class": error_class,
                "now": _now(),
            },
        )
        await db.commit()
    return JSONResponse(
        status_code=200,
        content=_envelope(
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            customer_reply=None,
            confidence=0.0,
            escalation_requested=True,
            escalation_reason_code="tool_failure",
            escalation_explanation=escalation_explanation,
            latency_ms=latency_ms,
        ),
    )


__all__ = ["router"]
