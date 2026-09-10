"""Enforced per-agent LLM usage limits + usable cost/usage accounting.

WHY THIS EXISTS
---------------
Measured on the isolaruntime estate 2026-09-10 (read-only):

* ``app/services/quota_guard.check_agent_llm_quota()`` is the only function
  that reads ``agents.max_llm_calls_per_day``. It is **imported** in
  ``app/api/websocket.py`` and **never called**. Nothing else in the tree
  calls it. The cap therefore fails open on every path.
* ``app/services/heartbeat.py::_execute_heartbeat()`` calls
  ``client.complete()`` in a ``for round_i in range(20)`` loop and never
  touches quota_guard. Autonomous wakes (278 agents on a 240 minute
  heartbeat) are therefore entirely unmetered against the cap.
* Across 444 agents the sum of ``llm_calls_today`` is **2**, last touched
  2026-05-06, while ``daily_token_usage`` recorded ~50.9M tokens on
  2026-09-10 alone. The counter is not a cap; it is a stub.
* ``llm_call_telemetry`` (the only table with ``cost_cents``) has no model
  and no writer anywhere in ``/app``; its last row is 2026-07-09.

This module replaces the read-modify-write quota with a single atomic
reservation statement, restores the telemetry write path, and provides the
daily reset that ``last_daily_reset`` / ``llm_calls_reset_at`` never got.

DESIGN NOTES
------------
Reservation is ONE SQL statement (check + increment + rollover) so that two
agents arriving at the boundary concurrently cannot both be admitted: the
row lock is held for the whole check-and-increment, and a caller that is
refused never increments (no counter leak). Retries reserve per attempt on
purpose -- a retried provider call is a real provider call and must be paid
for out of the same budget.

All timestamps are passed in as bound parameters rather than using ``now()``
so the same SQL runs on Postgres in production and on SQLite in tests.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text

# Fallback cap applied when an agent row carries NULL. NULL currently means
# "unlimited" on this estate; here it means "the platform default", which is
# the entire point of the repair.
DEFAULT_MAX_LLM_CALLS_PER_DAY = 100

# llm_models.cost_per_1k_input_cents / cost_per_1k_output_cents are NULL for
# all 235 model rows in production, so a DB-only cost calculation yields
# nothing. This fallback price book makes cost reportable today without a
# data migration; DB values always win when present. Cents per 1k tokens.
FALLBACK_PRICE_BOOK: dict[tuple[str, str], tuple[Decimal, Decimal]] = {
    ("deepseek", "deepseek-chat"): (Decimal("0.027"), Decimal("0.110")),
    ("deepseek", "deepseek-reasoner"): (Decimal("0.055"), Decimal("0.219")),
    ("deepseek", "deepseek-v4-pro"): (Decimal("0.055"), Decimal("0.219")),
    ("openai", "gpt-4o-mini"): (Decimal("0.015"), Decimal("0.060")),
    ("openai", "gpt-4o"): (Decimal("0.250"), Decimal("1.000")),
    ("anthropic", "claude-sonnet-4"): (Decimal("0.300"), Decimal("1.500")),
    ("ollama", "*"): (Decimal("0"), Decimal("0")),
}


class UsageLimitExceeded(Exception):
    """Raised when an agent has no remaining budget for this call.

    Deliberately distinct from ``quota_guard.QuotaExceeded`` so a caller can
    tell "refused by the enforced meter" from "refused by the legacy
    (unenforced) guard" while both exist.
    """

    def __init__(self, agent_id: uuid.UUID, used: int, cap: int, limit_kind: str = "llm_calls"):
        self.agent_id = agent_id
        self.used = used
        self.cap = cap
        self.limit_kind = limit_kind
        self.message = (
            f"Agent {agent_id} has reached its daily {limit_kind} limit ({used}/{cap}). "
            f"No further provider calls will be made today."
        )
        super().__init__(self.message)


@dataclass(frozen=True)
class Reservation:
    """A granted unit of budget. One reservation == one provider call."""

    agent_id: uuid.UUID
    calls_today: int
    cap: int

    @property
    def remaining(self) -> int:
        return max(self.cap - self.calls_today, 0)


def _day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------
# Reservation -- the enforcement point
# --------------------------------------------------------------------------

# Rollover and admission are evaluated inside the same statement. If the row
# has not been reset today the reservation is granted as call #1 and the
# reset stamp is moved forward; otherwise admission requires
# llm_calls_today < cap. Zero rows returned == refused.
_RESERVE_SQL = text(
    """
    UPDATE agents
       SET llm_calls_today = CASE
               WHEN llm_calls_reset_at IS NULL OR llm_calls_reset_at < :day_start THEN 1
               ELSE llm_calls_today + 1
           END,
           llm_calls_reset_at = CASE
               WHEN llm_calls_reset_at IS NULL OR llm_calls_reset_at < :day_start THEN :now
               ELSE llm_calls_reset_at
           END
     WHERE id = :agent_id
       AND (
             llm_calls_reset_at IS NULL
          OR llm_calls_reset_at < :day_start
          OR llm_calls_today < COALESCE(max_llm_calls_per_day, :default_cap)
           )
    RETURNING llm_calls_today, COALESCE(max_llm_calls_per_day, :default_cap)
    """
)

_READ_USAGE_SQL = text(
    """
    SELECT llm_calls_today, COALESCE(max_llm_calls_per_day, :default_cap)
      FROM agents
     WHERE id = :agent_id
    """
)


async def reserve_llm_call(
    agent_id: uuid.UUID,
    session,
    *,
    now: datetime | None = None,
    default_cap: int = DEFAULT_MAX_LLM_CALLS_PER_DAY,
) -> Reservation:
    """Reserve budget for exactly one provider call, or refuse.

    Atomic: check, daily rollover and increment happen in one statement, so
    two callers at the boundary cannot both be admitted and a refusal never
    increments the counter.

    Raises:
        UsageLimitExceeded: the agent is at or over its daily cap.
        LookupError: no such agent.
    """
    now = now or datetime.now(timezone.utc)
    params: dict[str, Any] = {
        "agent_id": str(agent_id),
        "now": now,
        "day_start": _day_start(now),
        "default_cap": default_cap,
    }

    row = (await session.execute(_RESERVE_SQL, params)).first()
    if row is not None:
        await session.commit()
        return Reservation(agent_id=agent_id, calls_today=int(row[0]), cap=int(row[1]))

    # No row updated: either refused, or the agent does not exist.
    await session.rollback()
    current = (
        await session.execute(
            _READ_USAGE_SQL, {"agent_id": str(agent_id), "default_cap": default_cap}
        )
    ).first()
    if current is None:
        raise LookupError(f"agent {agent_id} not found")
    raise UsageLimitExceeded(agent_id, int(current[0]), int(current[1]))


# --------------------------------------------------------------------------
# Daily reset -- the counter the estate never had
# --------------------------------------------------------------------------

_RESET_CALLS_SQL = text(
    """
    UPDATE agents
       SET llm_calls_today = 0,
           llm_calls_reset_at = :now
     WHERE llm_calls_reset_at IS NULL OR llm_calls_reset_at < :day_start
    """
)

_RESET_TOKENS_SQL = text(
    """
    UPDATE agents
       SET tokens_used_today = 0,
           last_daily_reset = :now
     WHERE last_daily_reset IS NULL OR last_daily_reset < :day_start
    """
)


async def reset_daily_counters(session, *, now: datetime | None = None) -> dict[str, int]:
    """Roll the daily counters over. Idempotent -- a second run in the same
    UTC day touches zero rows.

    ``agents.last_daily_reset`` has not advanced since 2026-06-07 in
    production, which is why ``tokens_used_today`` there is a lifetime
    counter. Reservation does its own lazy per-row rollover, so this job is
    a safety net and a way to keep ``tokens_used_today`` meaningful for
    reporting; enforcement does not depend on it having run.
    """
    now = now or datetime.now(timezone.utc)
    params = {"now": now, "day_start": _day_start(now)}
    calls = (await session.execute(_RESET_CALLS_SQL, params)).rowcount or 0
    tokens = (await session.execute(_RESET_TOKENS_SQL, params)).rowcount or 0
    await session.commit()
    return {"calls_reset": calls, "tokens_reset": tokens}


# --------------------------------------------------------------------------
# Accounting -- restore the cost/usage write path
# --------------------------------------------------------------------------


def price_for(provider: str | None, model: str | None,
              db_in: Any = None, db_out: Any = None) -> tuple[Decimal, Decimal] | None:
    """Cents per 1k input / output tokens. DB values win; else price book."""
    if db_in is not None and db_out is not None:
        return Decimal(str(db_in)), Decimal(str(db_out))
    p = (provider or "").lower()
    m = (model or "").lower()
    return FALLBACK_PRICE_BOOK.get((p, m)) or FALLBACK_PRICE_BOOK.get((p, "*"))


def compute_cost_cents(
    input_tokens: int | None,
    output_tokens: int | None,
    provider: str | None,
    model: str | None,
    db_in: Any = None,
    db_out: Any = None,
) -> Decimal | None:
    """Cost of one call in cents, or None when the model is unpriced.

    None is returned rather than 0 on purpose: "we do not know what this
    cost" and "this cost nothing" must not look the same in the ledger.
    """
    price = price_for(provider, model, db_in, db_out)
    if price is None:
        return None
    cin, cout = price
    return (Decimal(input_tokens or 0) / 1000 * cin) + (Decimal(output_tokens or 0) / 1000 * cout)


_INSERT_TELEMETRY_SQL = text(
    """
    INSERT INTO llm_call_telemetry (
        tenant_id, agent_id, selected_model_id, selected_provider, selected_model,
        input_tokens, output_tokens, cost_cents, cost_billed_to,
        latency_ms, success, error_class, intent, created_at
    ) VALUES (
        :tenant_id, :agent_id, :selected_model_id, :selected_provider, :selected_model,
        :input_tokens, :output_tokens, :cost_cents, :cost_billed_to,
        :latency_ms, :success, :error_class, :intent, :created_at
    )
    """
)


async def record_llm_call(
    session,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    provider: str | None,
    model: str | None,
    model_id: uuid.UUID | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    success: bool = True,
    error_class: str | None = None,
    intent: str | None = None,
    cost_billed_to: str = "epic",
    db_price_in: Any = None,
    db_price_out: Any = None,
    now: datetime | None = None,
) -> Decimal | None:
    """Write one row to ``llm_call_telemetry``. Returns the cost in cents.

    Failed calls are recorded too (``success=False``) -- a provider call that
    errored after the tokens were sent still cost money, and a run that
    cannot see its failures cannot report what it spent.
    """
    now = now or datetime.now(timezone.utc)
    cost = compute_cost_cents(input_tokens, output_tokens, provider, model, db_price_in, db_price_out)
    await session.execute(
        _INSERT_TELEMETRY_SQL,
        {
            "tenant_id": str(tenant_id),
            "agent_id": str(agent_id),
            "selected_model_id": str(model_id) if model_id else None,
            "selected_provider": provider,
            "selected_model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_cents": cost,
            "cost_billed_to": cost_billed_to,
            "latency_ms": latency_ms,
            "success": success,
            "error_class": error_class,
            "intent": intent,
            "created_at": now,
        },
    )
    await session.commit()
    return cost


_SPEND_SQL = text(
    """
    SELECT COUNT(*) AS calls,
           COALESCE(SUM(input_tokens), 0)  AS input_tokens,
           COALESCE(SUM(output_tokens), 0) AS output_tokens,
           SUM(cost_cents)                 AS cost_cents,
           COUNT(*) FILTER (WHERE cost_cents IS NULL) AS unpriced_calls
      FROM llm_call_telemetry
     WHERE created_at >= :since
       AND (:agent_id IS NULL OR agent_id = :agent_id)
    """
)


async def spend_since(session, since: datetime, agent_id: uuid.UUID | None = None) -> dict[str, Any]:
    """What did it cost? The question a controlled run has to be able to answer.

    ``unpriced_calls`` is reported alongside the total so a caller can see how
    much of the window the cost figure actually covers.
    """
    row = (
        await session.execute(
            _SPEND_SQL, {"since": since, "agent_id": str(agent_id) if agent_id else None}
        )
    ).first()
    return {
        "calls": int(row[0]),
        "input_tokens": int(row[1]),
        "output_tokens": int(row[2]),
        "cost_cents": row[3],
        "unpriced_calls": int(row[4]),
    }
