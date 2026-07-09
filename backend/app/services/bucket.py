"""Bucket accounting — debit and lazy renewal for tenant_subscription.bucket_state.

compute_debit and extract_usage are pure functions (unit-testable without DB).
debit_bucket is the async DB write; callers fire via asyncio.create_task.

bucket_state JSONB shape:
  {tokens_used: int, messages_used: int, last_call_at: str|null, renewed_at: str|null}

# FLOOR-13: bucket accounting replaceable by LiteLLM virtual key budgets
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

_RENEWAL_DAYS = 30


def extract_usage(usage: dict | None) -> tuple[int, int]:
    """Normalize provider usage dict → (input_tokens, output_tokens).

    Handles:
      OpenAI-compat (deepseek, Ollama): prompt_tokens / completion_tokens
      Anthropic / Gemini-normalized:    input_tokens / output_tokens
    Returns (0, 0) on None or missing keys.
    """
    if not usage:
        return 0, 0
    inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    return int(inp), int(out)


def compute_debit(
    bucket: dict,
    input_tokens: int,
    output_tokens: int,
    now: datetime,
) -> dict:
    """Return updated bucket_state after debiting input_tokens + output_tokens.

    Lazy renewal: if now - renewed_at >= 30 days, counters reset before
    debiting the current call. renewed_at advances to now on renewal.
    """
    renewed_at_str = bucket.get("renewed_at")
    if renewed_at_str:
        try:
            renewed_at = datetime.fromisoformat(renewed_at_str)
            if renewed_at.tzinfo is None:
                renewed_at = renewed_at.replace(tzinfo=timezone.utc)
            if (now - renewed_at) >= timedelta(days=_RENEWAL_DAYS):
                return {
                    "tokens_used": input_tokens + output_tokens,
                    "messages_used": 1,
                    "last_call_at": now.isoformat(),
                    "renewed_at": now.isoformat(),
                }
        except (ValueError, TypeError):
            pass  # malformed timestamp — skip renewal check, debit normally

    return {
        "tokens_used": (bucket.get("tokens_used") or 0) + input_tokens + output_tokens,
        "messages_used": (bucket.get("messages_used") or 0) + 1,
        "last_call_at": now.isoformat(),
        "renewed_at": bucket.get("renewed_at"),
    }


async def debit_bucket(tenant_id: str, input_tokens: int, output_tokens: int) -> None:
    """Fire-and-forget bucket debit. Callers: asyncio.create_task(debit_bucket(...)).

    Reads bucket_state, applies compute_debit (with lazy renewal), writes back.
    Failure is logged as a warning; it never surfaces to the customer path.
    # FLOOR-13: bucket accounting replaceable by LiteLLM virtual key budgets
    """
    from app.database import async_session  # local import avoids circular at module load

    try:
        async with async_session() as session:
            result = await session.execute(
                text(
                    "SELECT bucket_state FROM tenant_subscription "
                    "WHERE tenant_id = :tid"
                ),
                {"tid": str(tenant_id)},
            )
            row = result.fetchone()
            if row is None:
                logger.warning("debit_bucket: no tenant_subscription for %s", tenant_id)
                return

            current = row[0] or {}
            now = datetime.now(timezone.utc)
            new_bucket = compute_debit(current, input_tokens, output_tokens, now)

            await session.execute(
                text(
                    "UPDATE tenant_subscription "
                    "SET bucket_state = CAST(:bucket AS jsonb), updated_at = now() "
                    "WHERE tenant_id = :tid"
                ),
                {"bucket": json.dumps(new_bucket), "tid": str(tenant_id)},
            )
            await session.commit()
    except Exception as exc:
        logger.warning(
            "debit_bucket failed for tenant %s: %s", tenant_id, exc, exc_info=True
        )
