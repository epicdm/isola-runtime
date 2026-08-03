# ISOLA GLUE — additive helper (not upstream)
"""Run-scoped assistant-reply lookup via the durable delivery receipt.

Replaces the session-plus-time ``ChatMessage`` scan the legacy
(``isola_bridge.py``) and structured (``isola_bridge_structured.py``) bridges
previously used to find "the" assistant reply for a turn — a query with no
run boundary that can return a DIFFERENT run's message when two turns
overlap on the same reused session
(`defect-clawith-assistant-reply-selection-not-run-scoped-2026-08-03`).

RATIFIED DESIGN (`dec-clawith-run-scoped-assistant-reply-ownership-2026-08-03`)
--------------------------------------------------------------------------
* Option B: the reply for a run is read through that run's own
  ``agent_run_events`` delivery receipt (``event_type='delivery_succeeded'``),
  written durably and transactionally by
  ``app.services.agent_runtime.delivery.deliver_runtime_message`` — never by
  scanning ``chat_messages`` for "the newest assistant row in this session".
* Option C2 hardening: the receipt's ``payload.message_id`` must equal the
  runtime's own deterministic derivation,
  ``uuid5(run_id, "delivery-message:" + idempotency_key)`` (mirrors
  ``delivery._message_id``) — a receipt whose message id was not produced by
  that derivation is rejected rather than trusted.

No migration, backfill, index or in-memory ownership map is introduced —
this reads relations and the index (``ix_agent_run_events_run_created`` on
``(run_id, created_at)``) that already exist.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.audit import ChatMessage

DeliveryKind = Literal["terminal", "waiting"]

# Preference order when a run has produced more than one live receipt (e.g.
# a "waiting" prompt followed later by a "terminal" result): terminal wins.
_KIND_PRIORITY: dict[str, int] = {"terminal": 0, "waiting": 1}


@dataclass(frozen=True, slots=True)
class RunOwnedReply:
    """The one reply this run is durably on record as having delivered."""

    message_id: uuid.UUID
    content: str
    delivery_kind: DeliveryKind
    lifecycle_status: str | None


class RunOwnedReplyError(RuntimeError):
    """A delivery receipt or the message/run it names is scope-inconsistent.

    Callers must fail closed on this — never fall back to scanning the
    session for a different assistant message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _expected_message_id(run_id: uuid.UUID, idempotency_key: str) -> uuid.UUID:
    # Mirrors app.services.agent_runtime.delivery._message_id exactly. Not
    # imported directly: that helper is private to delivery.py's module
    # namespace, and duplicating one deterministic one-line derivation here
    # is cheaper than exporting a private symbol across module boundaries.
    return uuid.uuid5(run_id, f"delivery-message:{idempotency_key}")


def _select_receipt_event(
    events: list[AgentRunEvent],
) -> AgentRunEvent | None:
    """Pick the single preferred receipt: terminal over waiting, then
    newest ``created_at``, then a deterministic id tie-break.

    ``events`` must already be ordered ``created_at DESC, id DESC`` (the
    order the caller's query produces, using ``ix_agent_run_events_run_created``
    for the ``created_at`` component) — sorting here is a *stable* sort on
    delivery-kind priority alone, so within each priority group the
    newest-first / id-desc order from the query is preserved verbatim."""
    candidates = []
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("status") != "delivered":
            continue
        if payload.get("delivery_kind") not in _KIND_PRIORITY:
            continue
        if not payload.get("message_id"):
            continue
        candidates.append(event)
    if not candidates:
        return None
    candidates.sort(key=lambda e: _KIND_PRIORITY[e.payload["delivery_kind"]])
    return candidates[0]


async def read_run_owned_reply(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    session_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
) -> RunOwnedReply | None:
    """Read the durable, run-owned assistant reply for one turn.

    Returns ``None`` when this run has not yet materialized a delivered
    terminal/waiting receipt — the caller's existing bounded grace polling
    may continue. Raises ``RunOwnedReplyError`` when a receipt (or the
    message/run it names) exists but fails an ownership/identity/scope
    check — this is always a fail-closed condition, never a signal to look
    elsewhere for a different message.
    """
    result = await db.execute(
        select(AgentRunEvent)
        .where(
            AgentRunEvent.tenant_id == tenant_id,
            AgentRunEvent.run_id == run_id,
            AgentRunEvent.event_type == "delivery_succeeded",
        )
        .order_by(AgentRunEvent.created_at.desc(), AgentRunEvent.id.desc())
    )
    events = list(result.scalars().all())

    event = _select_receipt_event(events)
    if event is None:
        return None

    payload = event.payload if isinstance(event.payload, dict) else {}

    if not event.idempotency_key.startswith(f"run:{run_id}:"):
        raise RunOwnedReplyError(
            "idempotency_key_run_prefix_mismatch",
            f"delivery event {event.id} idempotency_key does not carry the run:{run_id}: prefix",
        )

    raw_message_id = payload.get("message_id")
    try:
        message_id = uuid.UUID(str(raw_message_id))
    except (TypeError, ValueError) as exc:
        raise RunOwnedReplyError(
            "message_id_not_a_uuid",
            f"delivery event {event.id} payload.message_id is not a valid UUID",
        ) from exc

    expected_message_id = _expected_message_id(run_id, event.idempotency_key)
    if message_id != expected_message_id:
        raise RunOwnedReplyError(
            "message_id_uuid5_mismatch",
            f"delivery event {event.id} payload.message_id does not equal "
            "uuid5(run_id, 'delivery-message:' + idempotency_key)",
        )

    raw_actual_session_id = payload.get("actual_session_id")
    if raw_actual_session_id is None or str(raw_actual_session_id) != str(session_id):
        raise RunOwnedReplyError(
            "receipt_session_mismatch",
            f"delivery event {event.id} payload.actual_session_id does not match the requested session",
        )

    message = await db.get(ChatMessage, message_id)
    if message is None:
        raise RunOwnedReplyError(
            "message_not_found",
            f"ChatMessage {message_id} referenced by delivery event {event.id} does not exist",
        )
    if message.role != "assistant":
        raise RunOwnedReplyError(
            "message_wrong_role",
            f"ChatMessage {message_id} referenced by delivery event {event.id} has role {message.role!r}, not 'assistant'",
        )
    content = (message.content or "").strip()
    if not content:
        raise RunOwnedReplyError(
            "message_empty_content",
            f"ChatMessage {message_id} referenced by delivery event {event.id} has empty content",
        )
    if message.conversation_id != str(session_id):
        raise RunOwnedReplyError(
            "message_conversation_mismatch",
            f"ChatMessage {message_id} conversation_id does not match the requested session",
        )
    if message.agent_id != agent_id:
        raise RunOwnedReplyError(
            "message_agent_mismatch",
            f"ChatMessage {message_id} agent_id does not match the requested agent",
        )
    if message.user_id != user_id:
        raise RunOwnedReplyError(
            "message_user_mismatch",
            f"ChatMessage {message_id} user_id does not match the requested user",
        )

    run_result = await db.execute(
        select(AgentRun).where(AgentRun.tenant_id == tenant_id, AgentRun.id == run_id)
    )
    run = run_result.scalar_one_or_none()
    if run is None:
        raise RunOwnedReplyError(
            "run_not_found",
            f"AgentRun {run_id} does not exist in tenant {tenant_id}",
        )
    if (
        run.tenant_id != tenant_id
        or run.id != run_id
        or run.session_id != session_id
        or run.agent_id != agent_id
        or run.origin_user_id != user_id
    ):
        raise RunOwnedReplyError(
            "run_ownership_mismatch",
            f"AgentRun {run_id} does not match the requested tenant/session/agent/user scope",
        )

    return RunOwnedReply(
        message_id=message.id,
        content=content,
        delivery_kind=payload["delivery_kind"],
        lifecycle_status=payload.get("lifecycle_status"),
    )


__all__ = ["RunOwnedReply", "RunOwnedReplyError", "read_run_owned_reply"]
