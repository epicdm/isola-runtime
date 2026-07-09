"""Pre-call cost estimator + max_cost_cents guardrail.

Pure functions; no DB calls. Called in channel_common BEFORE model dispatch.

Conservative upper-bound estimate:
  - Input tokens: len(text) // 3 heuristic (no tiktoken; consistent with codebase)
  - Output tokens: max_output_tokens argument (worst-case upper bound)
  - Cost: model.cost_per_1k_input_cents + model.cost_per_1k_output_cents

Phase 2 note: cost_per_1k_input_cents defaults to 0.0 in PoolModel for all
current models (DB column lands in Floor #13 when LiteLLM spend tracking lands).

Guardrail fires rarely in Phase 2 (Ollama is free; DeepSeek at real prices
stays under typical intent_routing.max_cost_cents for normal messages).
Exists primarily to protect against edge cases (large document pastes).

# FLOOR-13: replaceable by LiteLLM virtual key spend limits + router conditions
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.services.lcr import PoolModel

_QUANTIZE = Decimal("0.000001")
_MAX_OUTPUT_TOKENS_DEFAULT = 2000


def estimate_tokens_from_chars(text: str) -> int:
    """Token count heuristic: len(text) // 3. No tiktoken per Q6 grilling decision."""
    return len(text) // 3


def estimate_call_cost(
    input_text: str,
    model: PoolModel,
    max_output_tokens: int = _MAX_OUTPUT_TOKENS_DEFAULT,
) -> Decimal:
    """Project upper-bound cost in cents for a prospective call.

    Conservative: uses max_output_tokens (worst-case) rather than predicted output.
    """
    input_tokens = estimate_tokens_from_chars(input_text)
    input_cost = Decimal(str(model.cost_per_1k_input_cents)) * input_tokens / 1000
    output_cost = Decimal(str(model.cost_per_1k_output_cents)) * max_output_tokens / 1000
    return (input_cost + output_cost).quantize(_QUANTIZE)


@dataclass
class PreCallDecision:
    allowed: bool
    reason: str
    estimated_cost_cents: Decimal
    cap_cents: Optional[int]


def enforce_pre_call_cap(
    estimated_cost_cents: Decimal,
    max_cost_cents: Optional[int],
) -> PreCallDecision:
    """Reject call when projected cost exceeds intent_routing.max_cost_cents.

    max_cost_cents=None means no guardrail configured for this intent → always allow.
    # FLOOR-13: replaceable by LiteLLM virtual key spend limits
    """
    if max_cost_cents is None:
        return PreCallDecision(
            allowed=True,
            reason="no_cap_configured",
            estimated_cost_cents=estimated_cost_cents,
            cap_cents=None,
        )
    if estimated_cost_cents > Decimal(str(max_cost_cents)):
        return PreCallDecision(
            allowed=False,
            reason="projected_cost_exceeds_cap",
            estimated_cost_cents=estimated_cost_cents,
            cap_cents=max_cost_cents,
        )
    return PreCallDecision(
        allowed=True,
        reason="within_cap",
        estimated_cost_cents=estimated_cost_cents,
        cap_cents=max_cost_cents,
    )
