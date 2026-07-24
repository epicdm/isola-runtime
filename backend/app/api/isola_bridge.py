# ISOLA GLUE — additive bridge endpoint (not upstream)
"""Synchronous per-customer chat bridge for the external Isola spine.

Replaces the legacy synchronous ``/dispatch`` seam. It drives exactly one
chat turn through the plain, durable chat runtime (``enqueue_chat_runtime``)
and returns the settled assistant reply synchronously — WITHOUT using the
native channel-delivery path (which requires a hardcoded channel allowlist we
do not fork). Instead it reuses the ordinary Web-Chat runtime and reads the
settled Run's assistant reply from the persisted ChatMessage stream.

All net-new logic lives in this single additive file. Only one
``include_router`` line is added to ``main.py``.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

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

router = APIRouter(prefix="/isola/bridge", tags=["isola-bridge"])

# Stable namespace so a given phone always maps to the same tenant User row.
_ISOLA_USER_NS = uuid.UUID("a1f0c0de-1501-4a00-b000-000000000000")
_SECRET_ENV = "ISOLA_BRIDGE_SECRET"

# A Run is "settled enough to answer" once it is terminal OR it has parked
# waiting on the user/agent/external (i.e. it already delivered a reply, such
# as an identity-confirmation question). {created, queued, running, verifying}
# mean it is still producing and we keep polling.
_SETTLED_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "waiting_user", "waiting_external", "waiting_agent"}
)
_POLL_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.75
_MESSAGE_GRACE_S = 5.0


class BridgeMessageIn(BaseModel):
    agent_id: uuid.UUID
    phone: str = Field(min_length=3, max_length=64)
    text: str = Field(min_length=1)
    external_conversation_id: str | None = None
    # Opaque, short-lived scoped-capability minted by Foundation per turn
    # (lib/escalation-ref.ts). Carries no raw conversation/tenant/account/
    # inbox id of its own -- handed to the agent via caller_directive
    # below, never as a tool argument the agent could omit or forge.
    conversation_ref: str | None = None
    # Non-secret per-turn trace id, safe to log on either side.
    correlation_id: str | None = None


def _authorize(secret_header: str | None) -> JSONResponse | None:
    expected = os.environ.get(_SECRET_ENV, "")
    if not expected:
        return JSONResponse(status_code=503, content={"error": "isola_bridge_secret_unset"})
    if not secret_header or not hmac.compare_digest(str(secret_header), expected):
        return JSONResponse(status_code=401, content={"error": "invalid_isola_bridge_secret"})
    return None


def _stable_user_id(tenant_id: uuid.UUID, phone: str) -> uuid.UUID:
    return uuid.uuid5(_ISOLA_USER_NS, f"isola-bridge:{tenant_id}:{phone.strip()}")


async def _read_last_assistant(db, session_id: uuid.UUID, after: datetime) -> str | None:
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
    content = (message.content or "").strip()
    return content or None


@router.post("/message")
async def bridge_message(
    body: BridgeMessageIn,
    x_isola_secret: str | None = Header(default=None, alias="X-Isola-Secret"),
) -> JSONResponse:
    denied = _authorize(x_isola_secret)
    if denied is not None:
        return denied

    phone = body.phone.strip()
    text = body.text

    # --- Turn 1: create identity + session, persist user message, enqueue run ---
    try:
        async with async_session() as db:
            async with db.begin():
                agent = await db.get(Agent, body.agent_id)
                if agent is None or agent.tenant_id is None:
                    return JSONResponse(
                        status_code=404, content={"error": "agent_not_found"}
                    )
                tenant_id = agent.tenant_id

                model = None
                if agent.primary_model_id is not None:
                    model = await db.get(LLMModel, agent.primary_model_id)
                if model is None and agent.fallback_model_id is not None:
                    model = await db.get(LLMModel, agent.fallback_model_id)
                if model is None:
                    return JSONResponse(
                        status_code=409, content={"error": "agent_has_no_model"}
                    )

                # Resolve-or-create a stable User keyed by phone under the tenant.
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
                    return JSONResponse(
                        status_code=500, content={"error": "user_provision_failed"}
                    )

                # Bypass the per-(agent,user) onboarding ritual so a brand-new
                # customer never gets trapped in "define who I am" calibration.
                await db.execute(
                    pg_insert(AgentUserOnboarding)
                    .values(agent_id=agent.id, user_id=user.id, phase="completed")
                    .on_conflict_do_update(
                        index_elements=["agent_id", "user_id"],
                        set_={"phase": "completed"},
                    )
                )

                # Find-or-create the primary direct session for (agent,user);
                # same phone -> same user -> same reused session.
                session = await ensure_primary_platform_session(db, agent.id, user.id)

                # Convey the verified caller phone as a trusted per-turn runtime
                # instruction so the agent can drive its account-lookup tool
                # without asking the customer to re-state their number. This is
                # re-declared every call, so the bridge stays stateless.
                caller_directive = (
                    "The customer you are assisting is contacting EPIC "
                    f"Communications from the verified account phone number {phone}. "
                    "Treat this as their confirmed identity. When a tool or account "
                    f"lookup needs the customer's phone number, use {phone} directly "
                    "and do not ask them to provide or confirm it."
                )
                # Escalation instruction, only when Foundation minted a ref for
                # this turn (public Clawith path). The ref is a one-turn,
                # single-purpose capability -- the agent must never repeat,
                # explain, or disclose it, and it carries no decoded ownership
                # id of its own.
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
                        content=text,
                        source_channel="web",
                        runtime_instruction=caller_directive,
                        run_state_reader=reader,
                    )
                if intake is None:
                    return JSONResponse(
                        status_code=409,
                        content={"error": "runtime_v2_disabled_for_agent"},
                    )

                run_id = intake.handle.run_id
                session_id = session.id
                user_msg = await db.get(ChatMessage, intake.message_id)
                user_created_at = (
                    user_msg.created_at if user_msg is not None else datetime.now(UTC)
                )
    except ChatRuntimeIntakeError as exc:
        return JSONResponse(
            status_code=409, content={"error": exc.code, "detail": str(exc)}
        )

    # --- Poll the settled Run state, then read its assistant reply ---
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _POLL_TIMEOUT_S
    final_status: str | None = None
    waiting_reason: str | None = None
    result_summary: str | None = None

    async with async_session() as poll_db:
        async with open_run_state_reader(poll_db) as reader:
            settled = False
            while True:
                poll_db.expire_all()
                try:
                    view = await reader.get_run_state(tenant_id, run_id)
                    final_status = view.execution_status
                    waiting_reason = view.waiting_reason
                    result_summary = view.result_summary
                except RunStateReadError:
                    final_status = None
                if final_status in _SETTLED_STATUSES:
                    settled = True
                    break
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(_POLL_INTERVAL_S)

            if not settled:
                return JSONResponse(
                    status_code=504,
                    content={
                        "error": "runtime_timeout",
                        "run_id": str(run_id),
                        "matched_session": str(session_id),
                        "last_status": final_status,
                    },
                )

            # Once settled, the assistant reply is persisted as a ChatMessage.
            # Give delivery a short grace window in case the message row lands
            # a beat after the status transition.
            reply: str | None = None
            grace_deadline = loop.time() + _MESSAGE_GRACE_S
            while True:
                poll_db.expire_all()
                reply = await _read_last_assistant(poll_db, session_id, user_created_at)
                if reply is not None or loop.time() >= grace_deadline:
                    break
                await asyncio.sleep(_POLL_INTERVAL_S)

    if reply is None:
        # Defensive fallbacks so the spine still gets the model's output text.
        reply = (waiting_reason or result_summary or "").strip() or None

    if reply is None:
        return JSONResponse(
            status_code=502,
            content={
                "error": "no_assistant_reply",
                "run_id": str(run_id),
                "matched_session": str(session_id),
                "status": final_status,
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "reply": reply,
            "run_id": str(run_id),
            "matched_session": str(session_id),
            "status": final_status,
            "correlation_id": body.correlation_id,
        },
    )


__all__ = ["router"]

