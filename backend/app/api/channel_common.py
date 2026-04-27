"""Shared LLM invocation plumbing used by all channel adapters.

Originally lived inside feishu.py; extracted here so every channel adapter
(slack, discord, whatsapp, etc.) imports from a neutral place. No
channel-specific logic lives in this module.
"""

import asyncio
import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import is_agent_expired


# Default LLM timeout (seconds). Fallback when a model has no
# request_timeout set. Per-model request_timeout takes precedence via
# _get_llm_timeout().
_LLM_TIMEOUT_SECONDS_DEFAULT = 180.0


# File-received acknowledgement messages (rotated randomly by channel
# adapters that need to ack an inbound file before the LLM responds).
_FILE_ACK_MESSAGES = [
    "Got your file — how can I help with it?",
    "File received! What would you like me to do with it?",
    "Thanks, I have the file. What's the next step?",
    "File in hand. Ready whenever you are.",
    "Received. What would you like me to do with it?",
]


def _get_llm_timeout(model) -> float:
    """Effective LLM timeout for channel-side calls.

    Prefer model-level request_timeout so each model has its own budget
    (local vLLM may need 300 s, cloud APIs often only need 60 s). Falls
    back to _LLM_TIMEOUT_SECONDS_DEFAULT when the field is absent or zero.
    """
    timeout = getattr(model, "request_timeout", None)
    if timeout and float(timeout) > 0:
        return float(timeout)
    return _LLM_TIMEOUT_SECONDS_DEFAULT


async def _call_agent_llm(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_text: str,
    history: list[dict] | None = None,
    user_id=None,
    session_id: str = "",
    on_chunk=None,
    on_thinking=None,
    on_tool_call=None,
    *,
    role_description_override: str | None = None,  # Day 4b -- ADR-0070 #2
) -> str:
    """Call the agent's configured LLM model with conversation history.

    Reuses the same call_llm function as the WebSocket chat endpoint so
    all providers (OpenRouter, Qwen, etc.) work identically across
    channels.
    """
    from app.models.agent import Agent, DEFAULT_CONTEXT_WINDOW_SIZE
    from app.models.llm import LLMModel
    from app.services.llm import call_llm

    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ Agent not found"

    if is_agent_expired(agent):
        return "This Agent has expired and is off duty. Please contact your admin to extend its service."

    model = None
    if agent.primary_model_id:
        model_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
        model = model_result.scalar_one_or_none()
        if model and not model.enabled:
            logger.info(f"[Channel] Primary model {model.model} is disabled, skipping")
            model = None

    fallback_model = None
    if agent.fallback_model_id:
        fb_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.fallback_model_id))
        fallback_model = fb_result.scalar_one_or_none()
        if fallback_model and not fallback_model.enabled:
            logger.info(f"[Channel] Fallback model {fallback_model.model} is disabled, skipping")
            fallback_model = None

    if not model and fallback_model:
        model = fallback_model
        fallback_model = None
        logger.warning(f"[Channel] Primary model unavailable, using fallback: {model.model}")

    if not model:
        return f"⚠️ {agent.name} has no LLM model configured. Set one in admin."

    messages: list[dict] = []
    ctx_size = agent.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
    if history:
        messages.extend(history[-ctx_size:])
    messages.append({"role": "user", "content": user_text})

    effective_user_id = user_id or agent_id
    _timeout = _get_llm_timeout(model)

    # Day 4b -- ADR-0070 #2: SOUL.md from Paperclip overrides role_description
    # when present. Falls back to existing local field for non-dispatch callers.
    effective_role = (
        role_description_override
        if role_description_override is not None
        else (agent.role_description or "")
    )

    try:
        reply = await asyncio.wait_for(
            call_llm(
                model,
                messages,
                agent.name,
                effective_role,
                agent_id=agent_id,
                user_id=effective_user_id,
                session_id=session_id,
                supports_vision=getattr(model, "supports_vision", False),
                on_chunk=on_chunk,
                on_thinking=on_thinking,
                on_tool_call=on_tool_call,
            ),
            timeout=_timeout,
        )
        return reply
    except asyncio.TimeoutError:
        logger.error(
            f"[LLM] Call timed out after {_timeout}s "
            f"(agent_id={agent_id}, model={getattr(model, 'model', 'unknown')})"
        )
        if fallback_model:
            _fb_timeout = _get_llm_timeout(fallback_model)
            logger.info(
                f"[LLM] Retrying timed-out request with fallback model: "
                f"{fallback_model.model} (timeout={_fb_timeout}s)"
            )
            try:
                reply = await asyncio.wait_for(
                    call_llm(
                        fallback_model,
                        messages,
                        agent.name,
                        effective_role,
                        agent_id=agent_id,
                        user_id=effective_user_id,
                        session_id=session_id,
                        supports_vision=getattr(fallback_model, "supports_vision", False),
                        on_chunk=on_chunk,
                        on_thinking=on_thinking,
                        on_tool_call=on_tool_call,
                    ),
                    timeout=_fb_timeout,
                )
                return reply
            except asyncio.TimeoutError:
                logger.error(
                    f"[LLM] Fallback call also timed out after {_fb_timeout}s "
                    f"(agent_id={agent_id}, model={getattr(fallback_model, 'model', 'unknown')})"
                )
                return (
                    f"⚠️ Model response timed out (>{int(_fb_timeout)}s). "
                    "Please retry or shorten your request."
                )
            except Exception as e2:
                import traceback

                traceback.print_exc()
                return f"⚠️ Model error: Primary Timeout | Fallback: {str(e2)[:80]}"
        return (
            f"⚠️ Model response timed out (>{int(_timeout)}s). "
            "Please retry or shorten your request."
        )
    except Exception as e:
        import traceback

        traceback.print_exc()
        error_msg = str(e) or repr(e)
        logger.error(f"[LLM] Primary model error: {error_msg}")
        if fallback_model:
            logger.info(f"[LLM] Retrying with fallback model: {fallback_model.model}")
            try:
                _fb_timeout = _get_llm_timeout(fallback_model)
                reply = await asyncio.wait_for(
                    call_llm(
                        fallback_model,
                        messages,
                        agent.name,
                        effective_role,
                        agent_id=agent_id,
                        user_id=effective_user_id,
                        session_id=session_id,
                        supports_vision=getattr(fallback_model, "supports_vision", False),
                        on_chunk=on_chunk,
                        on_thinking=on_thinking,
                        on_tool_call=on_tool_call,
                    ),
                    timeout=_fb_timeout,
                )
                return reply
            except asyncio.TimeoutError:
                logger.error(
                    f"[LLM] Fallback call timed out after {_fb_timeout}s "
                    f"(agent_id={agent_id}, model={getattr(fallback_model, 'model', 'unknown')})"
                )
                return f"⚠️ Model error: Primary: {str(e)[:80]} | Fallback Timeout"
            except Exception as e2:
                traceback.print_exc()
                return f"⚠️ Model error: Primary: {str(e)[:80]} | Fallback: {str(e2)[:80]}"
        return f"⚠️ Model call failed: {error_msg[:150]}"
