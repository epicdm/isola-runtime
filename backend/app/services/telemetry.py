"""Telemetry write for LCR routing decisions.

compute_cost is a pure function (unit-testable without DB).
write_telemetry_row is async I/O; callers fire via asyncio.create_task.

Phase 2: cost_cents uses output tokens only (no cost_per_1k_input_cents in DB).
         cost_billed_to is always 'epic' for pool calls (Phase 3 expands).
         classified_by is 'rules' for Phase 2 (no LLM-based routing yet).

# FLOOR-13: replaceable by LiteLLM spend tracking + callbacks
"""
from __future__ import annotations
import logging
from decimal import Decimal
from typing import Optional

from sqlalchemy import text

from app.services.lcr import PoolModel

logger = logging.getLogger(__name__)

_QUANTIZE = Decimal("0.000001")


def compute_cost(model: PoolModel, output_tokens: int) -> Decimal:
    """Cost in cents for this call. Phase 2: output tokens only.

    deepseek-chat: 0.11¢/1k output  → 350 tokens → 0.038500¢
    Ollama models: 0¢               → any tokens → 0.000000¢
    """
    cents_per_token = Decimal(str(model.cost_per_1k_output_cents)) / 1000
    return (cents_per_token * output_tokens).quantize(_QUANTIZE)


async def write_telemetry_row(
    tenant_id: str,
    agent_id: str,
    intent: Optional[str],
    difficulty: Optional[str],
    selected_model_id: Optional[str],
    selected_provider: Optional[str],
    selected_model_name: Optional[str],
    byo_used: bool,
    fallback_used: bool,
    input_tokens: int,
    output_tokens: int,
    cost_cents: Decimal,
    cost_billed_to: str,
    latency_ms: Optional[int],
    success: bool,
    error_class: Optional[str],
    classified_by: Optional[str],
    classifier_confidence: Optional[Decimal],
) -> None:
    """Fire-and-forget telemetry write. Callers: asyncio.create_task(write_telemetry_row(...)).

    Failure is logged as a warning; it never surfaces to the customer path.
    # FLOOR-13: replaceable by LiteLLM spend tracking + callbacks
    """
    from app.database import async_session  # local import avoids circular at module load

    try:
        async with async_session() as session:
            await session.execute(
                text(
                    "INSERT INTO llm_call_telemetry ("
                    "  tenant_id, agent_id, intent, difficulty,"
                    "  selected_model_id, selected_provider, selected_model,"
                    "  byo_used, fallback_used,"
                    "  input_tokens, output_tokens,"
                    "  cost_cents, cost_billed_to,"
                    "  latency_ms, success, error_class,"
                    "  classified_by, classifier_confidence"
                    ") VALUES ("
                    "  :tenant_id, :agent_id, :intent, :difficulty,"
                    "  :selected_model_id, :selected_provider, :selected_model,"
                    "  :byo_used, :fallback_used,"
                    "  :input_tokens, :output_tokens,"
                    "  :cost_cents, :cost_billed_to,"
                    "  :latency_ms, :success, :error_class,"
                    "  :classified_by, :classifier_confidence"
                    ")"
                ),
                {
                    "tenant_id": str(tenant_id),
                    "agent_id": str(agent_id),
                    "intent": intent,
                    "difficulty": difficulty,
                    "selected_model_id": str(selected_model_id) if selected_model_id else None,
                    "selected_provider": selected_provider,
                    "selected_model": selected_model_name,
                    "byo_used": byo_used,
                    "fallback_used": fallback_used,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_cents": str(cost_cents),
                    "cost_billed_to": cost_billed_to,
                    "latency_ms": latency_ms,
                    "success": success,
                    "error_class": error_class,
                    "classified_by": classified_by,
                    "classifier_confidence": (
                        str(classifier_confidence) if classifier_confidence is not None else None
                    ),
                },
            )
            await session.commit()
    except Exception as exc:
        logger.warning(
            "write_telemetry_row failed for tenant %s: %s", tenant_id, exc, exc_info=True
        )
