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

# ─── L5 S1 follow-up: Langfuse @observe wrapper for channel_turn ─────────────
try:
    from langfuse import observe as _lf_observe, get_client as _lf_get_client
    _LANGFUSE_OK = True
except ImportError:
    _LANGFUSE_OK = False
    def _lf_observe(*args, **kwargs):  # noqa: ARG001
        def deco(fn):
            return fn
        return deco
    def _lf_get_client():
        return None
# _channel_common_followup_done



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


@_lf_observe(name="channel_turn", as_type="agent", capture_input=False, capture_output=False)
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

    # ─── L5 S1 follow-up: trace metadata + paperclip resolution ────
    _lf = _lf_get_client() if _LANGFUSE_OK else None
    if _lf is not None:
        try:
            from app.observability.tenant_resolver import resolve_paperclip_company_id
            _clawith_tid = str(agent.tenant_id) if agent.tenant_id else None
            _paperclip_id = await resolve_paperclip_company_id(_clawith_tid) if _clawith_tid else None
            _lf.update_current_span(metadata={
                "agent_id": str(agent_id),
                "agent_name": agent.name,
                "paperclip_company_id": _paperclip_id,
                "clawith_tenant_id": _clawith_tid,
                "session_id": session_id,
                "channel": "wa_or_chat",
                "tags": (
                    ([f"paperclip:{_paperclip_id}"] if _paperclip_id else [])
                    + ([f"clawith:{_clawith_tid}"] if _clawith_tid else [])
                ),
                "outcome": "pending",
            })
        except Exception as _e:
            logger.warning("[langfuse] channel_turn metadata setup failed: {}".format(_e))

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

    # ── Phase 2: LCR model selection ─────────────────────────────────────────
    # Active when agent.vertical is set; falls back silently to primary_model_id on any failure.
    # FLOOR-13: replace with LiteLLM virtual key selection
    _lcr_result = None
    _intent_pattern: str | None = None
    _intent_difficulty: str | None = None
    _intent_confidence: float = 1.0
    _bucket_renews_at = None

    if getattr(agent, "vertical", None):
        try:
            from sqlalchemy import text as _sa_text
            from app.services.intent_classifier import classify_intent as _classify_intent
            from app.services.lcr import (
                lcr_detailed as _lcr_detailed,
                PoolModel as _PoolModel,
                IntentRow as _IntentRow,
                TenantBucket as _TenantBucket,
            )
            from app.services.cost_estimator import (
                estimate_call_cost as _estimate_call_cost,
                enforce_pre_call_cap as _enforce_pre_call_cap,
            )

            _intent_pattern, _intent_difficulty, _intent_confidence = _classify_intent(
                user_text, agent.vertical
            )

            _ir_res = await db.execute(
                _sa_text(
                    "SELECT preferred_tier, capability_required, max_cost_cents, fallback_tier"
                    " FROM intent_routing"
                    " WHERE vertical = :v AND intent_pattern = :p LIMIT 1"
                ),
                {"v": agent.vertical, "p": _intent_pattern},
            )
            _ir_row = _ir_res.fetchone()

            _sub_res = await db.execute(
                _sa_text(
                    "SELECT ts.bucket_state, ts.credit_balance_cents,"
                    "       p.tier_access, p.included_tokens, p.overage_policy, p.id as plan_id"
                    " FROM tenant_subscription ts JOIN plan p ON p.id = ts.plan_id"
                    " WHERE ts.tenant_id = :tid LIMIT 1"
                ),
                {"tid": str(agent.tenant_id)},
            )
            _sub_row = _sub_res.fetchone()

            if _ir_row and _sub_row:
                _bkt = _sub_row.bucket_state or {}
                _bucket_renews_at = _bkt.get("renewed_at")
                _tb = _TenantBucket(
                    plan_id=str(_sub_row.plan_id),
                    entitled_tiers=list(_sub_row.tier_access or []),
                    tokens_used=int(_bkt.get("tokens_used", 0)),
                    included_tokens=int(_sub_row.included_tokens or 0),
                    overage_policy=_sub_row.overage_policy or "degrade",
                    credit_balance_cents=int(_sub_row.credit_balance_cents or 0),
                )
                _intent_row = _IntentRow(
                    vertical=agent.vertical,
                    intent_pattern=_intent_pattern,
                    difficulty=_intent_difficulty,
                    preferred_tier=_ir_row.preferred_tier or "standard",
                    capability_required=list(_ir_row.capability_required or []),
                    max_cost_cents=_ir_row.max_cost_cents,
                )
                _pool_res = await db.execute(
                    _sa_text(
                        "SELECT id, provider, model, tier, enabled, health_status,"
                        "       latency_p95_ms, cost_per_1k_output_cents, capability_tags"
                        " FROM llm_models"
                        " WHERE tenant_id IS NULL AND enabled = TRUE AND health_status = 'healthy'"
                    )
                )
                _pool = [
                    _PoolModel(
                        id=str(_r.id), provider=_r.provider, model=_r.model,
                        tier=_r.tier, enabled=_r.enabled, health_status=_r.health_status,
                        latency_p95_ms=_r.latency_p95_ms,
                        cost_per_1k_output_cents=float(_r.cost_per_1k_output_cents or 0),
                        capability_tags=list(_r.capability_tags or []),
                    )
                    for _r in _pool_res.fetchall()
                ]
                _lcr_result = _lcr_detailed(_intent_row, _tb, _pool)

                if _lcr_result:
                    # Pre-call cost guardrail (S8) — FLOOR-13: replaceable by LiteLLM spend limits
                    _est_cost = _estimate_call_cost(user_text, _lcr_result.model)
                    _cap = _enforce_pre_call_cap(_est_cost, _intent_row.max_cost_cents)
                    if not _cap.allowed:
                        from decimal import Decimal as _Decimal
                        from app.services.telemetry import write_telemetry_row as _write_telem
                        asyncio.create_task(_write_telem(
                            tenant_id=str(agent.tenant_id), agent_id=str(agent_id),
                            intent=_intent_pattern, difficulty=_intent_difficulty,
                            selected_model_id=_lcr_result.model.id,
                            selected_provider=_lcr_result.model.provider,
                            selected_model_name=_lcr_result.model.model,
                            byo_used=False, fallback_used=_lcr_result.fallback_used,
                            input_tokens=0, output_tokens=0,
                            cost_cents=_Decimal("0"), cost_billed_to="epic",
                            latency_ms=None, success=False,
                            error_class="pre_call_cap_exceeded",
                            classified_by="rules",
                            classifier_confidence=_Decimal(str(_intent_confidence)),
                        ))
                        return (
                            "This question requires more detail than your current plan allows per message. "
                            "Try a shorter version, or contact us directly."
                        )

                    # Override model with LCR-selected pool row ORM object
                    _pool_orm = (
                        await db.execute(
                            select(LLMModel).where(
                                LLMModel.id == uuid.UUID(_lcr_result.model.id)
                            )
                        )
                    ).scalar_one_or_none()
                    if _pool_orm and _pool_orm.enabled:
                        model = _pool_orm
                        fallback_model = None  # LCR owns selection; primary_model_id dormant
                        logger.info(
                            "[LCR] agent={} vertical={} intent={}/{} → {}/{}"
                            " degraded={} safety_override={}",
                            agent_id, agent.vertical,
                            _intent_pattern, _intent_difficulty,
                            _lcr_result.model.provider, _lcr_result.model.model,
                            _lcr_result.degraded, _lcr_result.safety_override,
                        )
        except Exception as _lcr_exc:
            logger.warning("[LCR] routing failed, using primary_model_id: {}", _lcr_exc)
            _lcr_result = None
    # ── end Phase 2 LCR ──────────────────────────────────────────────────────

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
        # ── Phase 2: post-call debit + telemetry + footer ────────────────────
        # FLOOR-13: debit/telemetry replaceable by LiteLLM callbacks; footer stays in Clawith
        if agent.vertical and _lcr_result:
            from decimal import Decimal as _Decimal
            from app.services.bucket import debit_bucket as _debit_bucket
            from app.services.telemetry import write_telemetry_row as _write_telem, compute_cost as _compute_cost
            from app.services.degradation_footer import apply_degradation_footer as _apply_footer
            from app.services.cost_estimator import estimate_tokens_from_chars as _estimate_tokens
            _in_tok = _estimate_tokens(user_text)
            _out_tok = _estimate_tokens(reply)
            asyncio.create_task(_debit_bucket(str(agent.tenant_id), _in_tok, _out_tok))
            asyncio.create_task(_write_telem(
                tenant_id=str(agent.tenant_id), agent_id=str(agent_id),
                intent=_intent_pattern, difficulty=_intent_difficulty,
                selected_model_id=_lcr_result.model.id,
                selected_provider=_lcr_result.model.provider,
                selected_model_name=_lcr_result.model.model,
                byo_used=False, fallback_used=_lcr_result.fallback_used,
                input_tokens=_in_tok, output_tokens=_out_tok,
                cost_cents=_compute_cost(_lcr_result.model, _out_tok),
                cost_billed_to="epic",
                latency_ms=None, success=True, error_class=None,
                classified_by="rules",
                classifier_confidence=_Decimal(str(_intent_confidence)),
            ))
            try:
                reply = _apply_footer(
                    base_reply=reply,
                    lcr_result=_lcr_result,
                    intent_difficulty=_intent_difficulty or "low",
                    bucket_renews_at=_bucket_renews_at,
                )
            except Exception as _fe:
                logger.warning("[footer] failed: %s", _fe)
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
