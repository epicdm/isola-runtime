# ISOLA GLUE — additive asynchronous bridge endpoint (not upstream)
"""Asynchronous bridge surface for the external Isola spine (Lane 2).

WHY THIS EXISTS
---------------
``isola_bridge.py`` answers synchronously: it enqueues a chat run, polls for
30s, waits 5s more for the message row, then gives up with 504. A legitimate
EMA turn that takes 51s therefore returns a *transport* failure while the run
completes normally moments later. The caller had no supported way to collect
that result — the only route was an operator SQL query against Clawith's own
tables, which is not a contract.

This module adds a submit/retrieve pair so a long-running turn is a normal
outcome rather than an error:

    POST /api/isola/bridge/v2/requests            -> accept, return a handle
    GET  /api/isola/bridge/v2/requests/{id}       -> pending | completed | ...

The legacy synchronous route is untouched. Unknown consumers keep their
current semantics exactly.

DESIGN NOTES
------------
* All net-new logic lives in this single additive file plus one migration and
  one ``include_router`` line. No upstream file is modified.
* The bridge owns its own mapping table (``isola_bridge_requests``). It does
  not depend on ``agent_runs.correlation_id`` or ``chat_sessions.external_conv_id``,
  both of which are empty today.
* Terminal-result selection reuses the exact semantics already proven by
  ``_read_last_assistant``: correct session, ``assistant`` role, created at or
  after the initiating user turn, newest first, non-empty content. ``tool_call``
  is a distinct role value, so tool calls are excluded by the role filter.
* Customer-agent selection is a fail-closed allowlist. An unset allowlist
  rejects everything, so an INTERNAL agent (Atlas, Scout, Ledger) can never be
  addressed as a customer agent by accident.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.isola_bridge import _authorize, _stable_user_id
from app.database import async_session
from app.models.agent import Agent, AgentUserOnboarding
from app.models.audit import ChatMessage
from app.models.llm import LLMModel
from app.models.user import User
from app.services.agent_runtime.chat_intake import (
    ChatRuntimeIntakeError,
    enqueue_chat_runtime,
)
from app.services.agent_runtime.run_state_reader import (
    RunStateReadError,
    open_run_state_reader,
)
from app.services.chat_session_service import ensure_primary_platform_session

router = APIRouter(prefix="/isola/bridge/v2", tags=["isola-bridge-v2"])

_SETTLED_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "waiting_user", "waiting_external", "waiting_agent"}
)
_FAILED_STATUSES = frozenset({"failed", "cancelled"})

# Submission stays fast: enqueue, take one short look, hand back a handle.
_ACCEPT_PEEK_S = 1.5
_MESSAGE_GRACE_S = 5.0
_DEFAULT_RETENTION_H = int(os.environ.get("ISOLA_BRIDGE_V2_RETENTION_HOURS", "24"))
_RETRY_AFTER_S = 3

_ALLOWLIST_ENV = "ISOLA_BRIDGE_V2_CUSTOMER_AGENTS"

class BridgeRequestState(str, Enum):
    """What is PERSISTED in isola_bridge_requests.state. Guarded by a DB check
    constraint; every value here must exist in that constraint."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


class BridgeResultStatus(str, Enum):
    """What a RETRIEVAL returns. Deliberately a different vocabulary: `pending`
    is derived, and `not_found` cannot be persisted because there is no row."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    NOT_FOUND = "not_found"


_STATE_TO_STATUS: dict[BridgeRequestState, BridgeResultStatus] = {
    BridgeRequestState.ACCEPTED: BridgeResultStatus.PENDING,
    BridgeRequestState.RUNNING: BridgeResultStatus.PENDING,
    BridgeRequestState.COMPLETED: BridgeResultStatus.COMPLETED,
    BridgeRequestState.FAILED: BridgeResultStatus.FAILED,
    BridgeRequestState.CANCELLED: BridgeResultStatus.CANCELLED,
    BridgeRequestState.EXPIRED: BridgeResultStatus.EXPIRED,
    BridgeRequestState.REJECTED: BridgeResultStatus.FAILED,
}


def result_status_for(state: str | None, has_terminal_result: bool) -> BridgeResultStatus:
    """The ONE place a persisted state becomes a retrieval status.

    Fails closed: an unknown persisted state, or `completed` with no terminal
    message, is reported as failed rather than as a usable result.
    """
    if state is None:
        return BridgeResultStatus.NOT_FOUND
    try:
        persisted = BridgeRequestState(state)
    except ValueError:
        return BridgeResultStatus.FAILED
    mapped = _STATE_TO_STATUS[persisted]
    if persisted is BridgeRequestState.COMPLETED and not has_terminal_result:
        return BridgeResultStatus.FAILED
    if mapped is BridgeResultStatus.PENDING and has_terminal_result:
        return BridgeResultStatus.COMPLETED
    return mapped


# Aliases kept so persistence sites read as persistence, not as API status.
STATE_RUNNING = BridgeRequestState.RUNNING.value
STATE_COMPLETED = BridgeRequestState.COMPLETED.value
STATE_FAILED = BridgeRequestState.FAILED.value
STATE_CANCELLED = BridgeRequestState.CANCELLED.value
STATE_EXPIRED = BridgeRequestState.EXPIRED.value


def _customer_agent_allowlist() -> set[str]:
    raw = os.environ.get(_ALLOWLIST_ENV, "")
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


class BridgeRequestIn(BaseModel):
    # Unknown properties are REFUSED, not ignored. An undeclared field is an
    # unbounded authority channel (tool_authority, system_prompt, overrides).
    model_config = ConfigDict(extra="forbid")

    agent_id: uuid.UUID
    phone: str = Field(min_length=3, max_length=64)
    text: str = Field(min_length=1)
    stable_request_id: str = Field(min_length=8, max_length=200)
    correlation_id: str | None = Field(default=None, max_length=200)
    external_conversation_id: str | None = Field(default=None, max_length=200)
    session_id: uuid.UUID | None = None
    # Minted by Foundation only. Lane 2 must never synthesise one.
    conversation_ref: str | None = Field(default=None, max_length=512)
    response_mode: str = Field(default="async")
    # Deliberately narrow: a free-form dict here would be an unbounded tool
    # authority channel. Only non-executable trace labels are accepted.
    metadata_labels: dict[str, str] | None = None


async def _resolve_terminal(db, session_id: uuid.UUID, after: datetime):
    """Same predicate as the proven `_read_last_assistant`, but returns the row."""
    result = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.conversation_id == str(session_id),
            ChatMessage.role == "assistant",
            ChatMessage.created_at >= after,
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(1)
    )
    message = result.scalar_one_or_none()
    if message is None:
        return None
    if not (message.content or "").strip():
        return None
    return message


_INSERT_SQL = text(
    """
    INSERT INTO isola_bridge_requests (
        id, tenant_id, stable_request_id, correlation_id, external_conversation_id,
        contact_ref, agent_id, clawith_user_id, session_id, run_id,
        initiating_message_id, state, accepted_at, started_at, last_checked_at,
        idempotency_key, expires_at, metadata_labels
    ) VALUES (
        :id, :tenant_id, :stable_request_id, :correlation_id, :external_conversation_id,
        :contact_ref, :agent_id, :clawith_user_id, :session_id, :run_id,
        :initiating_message_id, :state, :accepted_at, :started_at, :last_checked_at,
        :idempotency_key, :expires_at, CAST(:metadata_labels AS JSONB)
    )
    ON CONFLICT (tenant_id, stable_request_id) DO NOTHING
    RETURNING id
    """
)

_SELECT_BY_STABLE_SQL = text(
    "SELECT * FROM isola_bridge_requests WHERE tenant_id = :tenant_id "
    "AND stable_request_id = :stable_request_id"
)

_SELECT_BY_ID_SQL = text("SELECT * FROM isola_bridge_requests WHERE id = :id")


class _Row:
    """Attribute view over a RowMapping so one formatter serves both paths."""

    def __init__(self, mapping):
        self._m = mapping

    def __getattr__(self, name):
        try:
            return self._m[name]
        except KeyError as exc:  # pragma: no cover - programming error
            raise AttributeError(name) from exc


def _json_labels(labels: dict[str, str] | None) -> str:
    import json

    return json.dumps(labels or {})


def _handle(row, replayed: bool) -> dict:
    return {
        "bridge_request_id": str(row.id),
        "state": row.state,
        "agent_id": str(row.agent_id),
        "session_id": str(row.session_id) if row.session_id else None,
        "run_id": str(row.run_id) if row.run_id else None,
        "tenant_id": str(row.tenant_id),
        "accepted_at": _iso(row.accepted_at),
        "correlation_id": row.correlation_id,
        "external_conversation_id": row.external_conversation_id,
        "stable_request_id": row.stable_request_id,
        "idempotent_replay": replayed,
        "result_url": f"/api/isola/bridge/v2/requests/{row.id}",
        "retry_after_seconds": _RETRY_AFTER_S,
    }


@router.post("/requests")
async def submit_request(
    body: BridgeRequestIn,
    x_isola_secret: str | None = Header(default=None, alias="X-Isola-Secret"),
) -> JSONResponse:
    denied = _authorize(x_isola_secret)
    if denied is not None:
        return denied

    if body.response_mode not in ("async", "async_only"):
        return JSONResponse(status_code=400, content={"error": "unsupported_response_mode"})

    allowlist = _customer_agent_allowlist()
    if not allowlist:
        return JSONResponse(
            status_code=503,
            content={
                "error": "customer_agent_allowlist_unset",
                "detail": "ISOLA_BRIDGE_V2_CUSTOMER_AGENTS is empty; refusing every agent.",
            },
        )
    if str(body.agent_id).lower() not in allowlist:
        return JSONResponse(
            status_code=403,
            content={"error": "agent_not_customer_facing", "agent_id": str(body.agent_id)},
        )

    phone = body.phone.strip()

    try:
        async with async_session() as db:
            async with db.begin():
                agent = await db.get(Agent, body.agent_id)
                if agent is None or agent.tenant_id is None:
                    return JSONResponse(status_code=404, content={"error": "agent_not_found"})
                tenant_id = agent.tenant_id

                existing = (
                    await db.execute(
                        _SELECT_BY_STABLE_SQL,
                        {"tenant_id": str(tenant_id), "stable_request_id": body.stable_request_id},
                    )
                ).mappings().first()
                if existing is not None:
                    return JSONResponse(
                        status_code=200, content=_handle(_Row(existing), replayed=True)
                    )

                model = None
                if agent.primary_model_id is not None:
                    model = await db.get(LLMModel, agent.primary_model_id)
                if model is None and agent.fallback_model_id is not None:
                    model = await db.get(LLMModel, agent.fallback_model_id)
                if model is None:
                    return JSONResponse(status_code=409, content={"error": "agent_has_no_model"})

                user_id = _stable_user_id(tenant_id, phone)
                await db.execute(
                    pg_insert(User)
                    .values(
                        id=user_id,
                        tenant_id=tenant_id,
                        display_name=phone,
                        role="member",
                        is_active=True,
                        registration_source="isola_bridge",
                    )
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                user = await db.get(User, user_id)
                if user is None:
                    return JSONResponse(status_code=500, content={"error": "user_provision_failed"})

                await db.execute(
                    pg_insert(AgentUserOnboarding)
                    .values(agent_id=agent.id, user_id=user.id, phase="completed")
                    .on_conflict_do_update(
                        index_elements=["agent_id", "user_id"], set_={"phase": "completed"}
                    )
                )

                session = await ensure_primary_platform_session(db, agent.id, user.id)
                if body.session_id is not None and body.session_id != session.id:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "error": "session_mismatch",
                            "requested": str(body.session_id),
                            "resolved": str(session.id),
                        },
                    )

                caller_directive = (
                    "The customer you are assisting is contacting EPIC "
                    f"Communications from the verified account phone number {phone}. "
                    "Treat this as their confirmed identity. When a tool or account "
                    f"lookup needs the customer's phone number, use {phone} directly "
                    "and do not ask them to provide or confirm it."
                )
                if body.conversation_ref:
                    caller_directive += (
                        " If the customer explicitly asks for a human, or the "
                        "request is out of scope for you, call the "
                        "escalate_to_human tool with conversation_ref="
                        f'"{body.conversation_ref}" and a short reason. This '
                        "conversation_ref is single-use for this turn only -- "
                        "never repeat it, explain it, or include it in any reply "
                        "to the customer, and never describe what it contains."
                    )

                async with open_run_state_reader(db) as reader:
                    intake = await enqueue_chat_runtime(
                        db,
                        agent=agent,
                        user=user,
                        session=session,
                        model=model,
                        content=body.text,
                        source_channel="web",
                        runtime_instruction=caller_directive,
                        run_state_reader=reader,
                    )
                if intake is None:
                    return JSONResponse(
                        status_code=409, content={"error": "runtime_v2_disabled_for_agent"}
                    )

                accepted_at = _now()
                bridge_request_id = uuid.uuid4()
                params = {
                    "id": str(bridge_request_id),
                    "tenant_id": str(tenant_id),
                    "stable_request_id": body.stable_request_id,
                    "correlation_id": body.correlation_id,
                    "external_conversation_id": body.external_conversation_id,
                    "contact_ref": phone,
                    "agent_id": str(agent.id),
                    "clawith_user_id": str(user.id),
                    "session_id": str(session.id),
                    "run_id": str(intake.handle.run_id),
                    "initiating_message_id": str(intake.message_id),
                    "state": STATE_RUNNING,
                    "accepted_at": accepted_at,
                    "started_at": accepted_at,
                    "last_checked_at": accepted_at,
                    "idempotency_key": f"{tenant_id}:{body.stable_request_id}",
                    "expires_at": accepted_at + timedelta(hours=_DEFAULT_RETENTION_H),
                    "metadata_labels": _json_labels(body.metadata_labels),
                }
                inserted = (await db.execute(_INSERT_SQL, params)).first()
                if inserted is None:
                    row = (
                        await db.execute(
                            _SELECT_BY_STABLE_SQL,
                            {
                                "tenant_id": str(tenant_id),
                                "stable_request_id": body.stable_request_id,
                            },
                        )
                    ).mappings().first()
                    return JSONResponse(status_code=200, content=_handle(_Row(row), replayed=True))
    except ChatRuntimeIntakeError as exc:
        return JSONResponse(status_code=409, content={"error": exc.code, "detail": str(exc)})

    await asyncio.sleep(_ACCEPT_PEEK_S)
    async with async_session() as db:
        row = (
            await db.execute(_SELECT_BY_ID_SQL, {"id": str(bridge_request_id)})
        ).mappings().first()
    return JSONResponse(status_code=202, content=_handle(_Row(row), replayed=False))


@router.get("/requests/{bridge_request_id}")
async def get_request(
    bridge_request_id: uuid.UUID,
    tenant_id: str | None = None,
    x_isola_secret: str | None = Header(default=None, alias="X-Isola-Secret"),
    x_isola_tenant: str | None = Header(default=None, alias="X-Isola-Tenant"),
) -> JSONResponse:
    denied = _authorize(x_isola_secret)
    if denied is not None:
        return denied

    claimed_tenant = x_isola_tenant or tenant_id

    async with async_session() as db:
        row = (
            await db.execute(_SELECT_BY_ID_SQL, {"id": str(bridge_request_id)})
        ).mappings().first()
        if row is None:
            return JSONResponse(status_code=404, content={"state": BridgeResultStatus.NOT_FOUND.value})

        # Cross-tenant retrieval is refused as not_found: a holder of the
        # shared secret must not be able to confirm a request exists in
        # another tenant by probing ids.
        if claimed_tenant and str(row["tenant_id"]).lower() != claimed_tenant.strip().lower():
            return JSONResponse(status_code=404, content={"state": BridgeResultStatus.NOT_FOUND.value})

        r = _Row(row)

        if r.state == STATE_COMPLETED:
            return JSONResponse(status_code=200, content=await _completed_payload(db, r))

        if r.state in (STATE_FAILED, STATE_CANCELLED, STATE_EXPIRED):
            return JSONResponse(
                status_code=200,
                content={
                    "state": result_status_for(r.state, False).value,
                    "bridge_request_id": str(r.id),
                    "error_class": r.error_class,
                    "correlation_id": r.correlation_id,
                    "external_conversation_id": r.external_conversation_id,
                },
            )

        if r.expires_at is not None and _now() >= r.expires_at.astimezone(UTC):
            await _mark(db, r.id, STATE_EXPIRED, error_class="retention_expired")
            return JSONResponse(
                status_code=200,
                content={
                    "state": result_status_for(BridgeRequestState.EXPIRED.value, False).value,
                    "bridge_request_id": str(r.id),
                },
            )

        run_status: str | None = None
        try:
            async with open_run_state_reader(db) as reader:
                view = await reader.get_run_state(
                    uuid.UUID(str(r.tenant_id)), uuid.UUID(str(r.run_id))
                )
                run_status = view.execution_status
        except (RunStateReadError, ValueError, TypeError):
            run_status = None

        initiating_at = await _initiating_created_at(db, r)
        message = None
        if initiating_at is not None:
            message = await _resolve_terminal(db, uuid.UUID(str(r.session_id)), initiating_at)

        if message is not None:
            await _mark(
                db,
                r.id,
                STATE_COMPLETED,
                terminal_message_id=str(message.id),
                completed_at=message.created_at,
            )
            fresh = (await db.execute(_SELECT_BY_ID_SQL, {"id": str(r.id)})).mappings().first()
            return JSONResponse(status_code=200, content=await _completed_payload(db, _Row(fresh)))

        if run_status in _FAILED_STATUSES:
            await _mark(db, r.id, STATE_FAILED, error_class=f"run_{run_status}")
            return JSONResponse(
                status_code=200,
                content={
                    "state": STATE_FAILED,
                    "bridge_request_id": str(r.id),
                    "error_class": f"run_{run_status}",
                },
            )

        await _touch(db, r.id)
        return JSONResponse(
            status_code=200,
            content={
                "state": result_status_for(r.state, False).value,
                "bridge_request_id": str(r.id),
                "run_state": run_status,
                "settled": run_status in _SETTLED_STATUSES if run_status else False,
                "retry_after_seconds": _RETRY_AFTER_S,
                "guidance": "poll this url; do not resubmit, a resubmission is deduplicated",
                "correlation_id": r.correlation_id,
                "external_conversation_id": r.external_conversation_id,
                "elapsed_ms": int((_now() - r.accepted_at.astimezone(UTC)).total_seconds() * 1000),
            },
        )


async def _initiating_created_at(db, r) -> datetime | None:
    if r.initiating_message_id is None:
        return r.accepted_at
    msg = await db.get(ChatMessage, uuid.UUID(str(r.initiating_message_id)))
    return msg.created_at if msg is not None else r.accepted_at


async def _completed_payload(db, r) -> dict:
    content = None
    escalation = "none"
    if r.terminal_message_id is not None:
        msg = await db.get(ChatMessage, uuid.UUID(str(r.terminal_message_id)))
        content = (msg.content or "") if msg is not None else None
    tool_summaries = await _tool_summaries(db, r)
    if any(t["tool"] == "escalate_to_human" and t["ok"] for t in tool_summaries):
        escalation = "escalated"
    elif any(t["tool"] == "escalate_to_human" for t in tool_summaries):
        escalation = "attempted_failed"
    elapsed = None
    if r.completed_at is not None and r.accepted_at is not None:
        elapsed = int(
            (r.completed_at.astimezone(UTC) - r.accepted_at.astimezone(UTC)).total_seconds() * 1000
        )
    return {
        "state": result_status_for(r.state, bool(content and content.strip())).value,
        "bridge_request_id": str(r.id),
        "response": content,
        "terminal_message_id": str(r.terminal_message_id) if r.terminal_message_id else None,
        "completed_at": _iso(r.completed_at),
        "agent_id": str(r.agent_id),
        "session_id": str(r.session_id),
        "run_id": str(r.run_id) if r.run_id else None,
        "correlation_id": r.correlation_id,
        "external_conversation_id": r.external_conversation_id,
        "tool_outcomes": tool_summaries,
        "escalation_state": escalation,
        "elapsed_ms": elapsed,
        "retrieval_method": "clawith_bridge_v2",
    }


async def _tool_summaries(db, r) -> list[dict]:
    """Names and success flags only. No arguments, no reasoning, no secrets."""
    import json

    if r.initiating_message_id is None:
        return []
    msg = await db.get(ChatMessage, uuid.UUID(str(r.initiating_message_id)))
    if msg is None:
        return []
    rows = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(r.session_id),
                ChatMessage.role == "tool_call",
                ChatMessage.created_at >= msg.created_at,
            )
            .order_by(ChatMessage.created_at.asc())
        )
    ).scalars().all()
    out: list[dict] = []
    for row in rows:
        name = None
        ok = True
        try:
            parsed = json.loads(row.content or "{}")
            name = parsed.get("name")
            status = str(parsed.get("execution_status", "")).lower()
            ok = status not in ("failed", "error")
        except Exception:
            name = None
        if name:
            out.append({"tool": name, "ok": ok})
    return out


async def _mark(
    db, request_id, state, *, terminal_message_id=None, completed_at=None, error_class=None
) -> None:
    await db.execute(
        text(
            """
            UPDATE isola_bridge_requests
               SET state = :state,
                   terminal_message_id = COALESCE(CAST(:tmid AS UUID), terminal_message_id),
                   completed_at = COALESCE(:completed_at, completed_at),
                   error_class = COALESCE(:error_class, error_class),
                   last_checked_at = :now
             WHERE id = :id
            """
        ),
        {
            "state": state,
            "tmid": terminal_message_id,
            "completed_at": completed_at,
            "error_class": error_class,
            "now": _now(),
            "id": str(request_id),
        },
    )
    await db.commit()


async def _touch(db, request_id) -> None:
    await db.execute(
        text("UPDATE isola_bridge_requests SET last_checked_at = :now WHERE id = :id"),
        {"now": _now(), "id": str(request_id)},
    )
    await db.commit()


__all__ = ["router"]
