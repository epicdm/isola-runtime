"""L5 S1 — Langfuse client singleton for isola-runtime backend.

Reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL from env
(provisioned at S0.5 from Infisical; passthrough via docker-compose.override.yml).

Returns None if env unset — callers must guard. Trace failures must NEVER
break the agent path; all instrumentation is best-effort.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_client = None
_init_attempted = False


def get_langfuse():
    """Return the Langfuse client singleton, or None if env unset / init fails."""
    global _client, _init_attempted
    if _init_attempted:
        return _client
    _init_attempted = True

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY") or ""
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY") or ""
    host = os.environ.get("LANGFUSE_BASE_URL") or ""

    if not (public_key and secret_key and host):
        logger.warning(
            "[langfuse] init skipped — missing env "
            "(public_key=%s, secret_key=%s, host=%s)",
            "set" if public_key else "unset",
            "set" if secret_key else "unset",
            "set" if host else "unset",
        )
        return None

    try:
        from langfuse import Langfuse  # type: ignore
        _client = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
        logger.info("[langfuse] client initialized host=%s", host)
    except Exception as e:  # noqa: BLE001 — never break agent path
        logger.warning("[langfuse] init failed: %s", e)
        _client = None
    return _client


# Anthropic + Gemini cost rate table (USD per 1K tokens).
# Keep parity with bff-v2 lib/llm-router.ts:103-149 (referenced at S1 step 4
# pre-build verification gate). Update on model price changes.
COST_PER_1K = {
    # Anthropic Claude
    "claude-3-5-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-5-sonnet-20241022": {"input": 0.003, "output": 0.015},
    "claude-3-5-haiku": {"input": 0.0008, "output": 0.004},
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-sonnet-4": {"input": 0.003, "output": 0.015},
    "claude-sonnet-4-5": {"input": 0.003, "output": 0.015},
    # Google Gemini
    "gemini-2.5-flash": {"input": 0.0003, "output": 0.0025},
    "gemini-2.5-flash-lite": {"input": 0.0001, "output": 0.0004},
    "gemini-2.5-pro": {"input": 0.00125, "output": 0.005},
    "gemini-1.5-pro": {"input": 0.00125, "output": 0.005},
    "gemini-1.5-flash": {"input": 0.000075, "output": 0.0003},
    # OpenAI / DeepSeek (rough placeholders; revise when bff-router parity confirmed)
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "deepseek-chat": {"input": 0.00027, "output": 0.0011},
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    """Best-effort cost in USD; returns None if model not in table."""
    if not model:
        return None
    rates = COST_PER_1K.get(model)
    if not rates:
        # Try prefix match (e.g., "anthropic/claude-sonnet-4-5" -> "claude-sonnet-4-5")
        bare = model.split("/")[-1]
        rates = COST_PER_1K.get(bare)
    if not rates:
        return None
    return round(
        (input_tokens / 1000.0) * rates["input"]
        + (output_tokens / 1000.0) * rates["output"],
        6,
    )
