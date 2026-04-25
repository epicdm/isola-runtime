"""Phase F.1.5a / Tier 1.5a — owner-ask state service.

Manages the OwnerAskInFlight rows that track which questions Rex has
in-flight to the owner. Two surfaces:

  - reply_markers `_dispatch_ask_owner` calls `create_ask` when the LLM
    emits `[ask_owner: ...]`.
  - whatsapp.py inbound webhook calls `find_oldest_pending_for_agent`
    when a message arrives from the agent's owner_phone, then
    `mark_answered` / `mark_skipped` based on the reply.

24h TTL: rows older than that get expired so the owner doesn't rapidly
accumulate stale "awaiting" rows. Expiration is opportunistic on lookup;
no separate sweeper job for v1.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from loguru import logger
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.owner_ask import (
    STATUS_ANSWERED,
    STATUS_AWAITING,
    STATUS_EXPIRED,
    STATUS_SKIPPED,
    OwnerAskInFlight,
)


# Rows older than this with status=awaiting are expired on the next lookup.
ASK_TTL_HOURS = 24

# Skip keywords — matched case-insensitive against owner's full reply
# (after .strip()). "skip" is the canonical; the others are forgiveness
# for terse owner shorthand.
_SKIP_PHRASES = {"skip", "pass", "later", "not now", "n/a"}


def _is_skip(reply: str) -> bool:
    return reply.strip().lower() in _SKIP_PHRASES


async def create_ask(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    customer_phone: str,
    customer_name: str | None,
    customer_conversation_id: uuid.UUID | None,
    question: str,
    owner_phone: str,
) -> OwnerAskInFlight:
    """Insert a new awaiting-row. Caller commits."""
    row = OwnerAskInFlight(
        agent_id=agent_id,
        customer_phone=customer_phone,
        customer_name=customer_name,
        customer_conversation_id=customer_conversation_id,
        question=question.strip()[:1000],
        owner_phone=owner_phone,
        status=STATUS_AWAITING,
    )
    db.add(row)
    await db.flush()
    return row


async def _expire_stale_for_agent(
    db: AsyncSession, agent_id: uuid.UUID
) -> int:
    """Mark any awaiting rows older than ASK_TTL_HOURS as expired."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ASK_TTL_HOURS)
    res = await db.execute(
        update(OwnerAskInFlight)
        .where(
            and_(
                OwnerAskInFlight.agent_id == agent_id,
                OwnerAskInFlight.status == STATUS_AWAITING,
                OwnerAskInFlight.asked_at < cutoff,
            )
        )
        .values(status=STATUS_EXPIRED)
        .execution_options(synchronize_session=False)
    )
    return res.rowcount or 0


async def find_oldest_pending_for_agent(
    db: AsyncSession, agent_id: uuid.UUID
) -> OwnerAskInFlight | None:
    """Return the oldest awaiting row for `agent_id`, or None if none.

    Side effect: also expires stale rows for this agent so the next
    pending row returned is fresh. Caller commits.
    """
    expired = await _expire_stale_for_agent(db, agent_id)
    if expired:
        logger.info(
            f"[owner_ask] expired {expired} stale ask rows for agent={agent_id}"
        )

    res = await db.execute(
        select(OwnerAskInFlight)
        .where(
            and_(
                OwnerAskInFlight.agent_id == agent_id,
                OwnerAskInFlight.status == STATUS_AWAITING,
            )
        )
        .order_by(OwnerAskInFlight.asked_at.asc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def mark_answered(
    db: AsyncSession, ask: OwnerAskInFlight, owner_reply: str
) -> None:
    """Stamp the row as answered. Caller commits."""
    ask.status = STATUS_ANSWERED
    ask.answered_at = datetime.now(timezone.utc)
    ask.owner_reply = owner_reply.strip()[:2000]
    await db.flush()


async def mark_skipped(db: AsyncSession, ask: OwnerAskInFlight) -> None:
    """Stamp the row as skipped. Caller commits."""
    ask.status = STATUS_SKIPPED
    ask.answered_at = datetime.now(timezone.utc)
    await db.flush()


__all__ = [
    "ASK_TTL_HOURS",
    "create_ask",
    "find_oldest_pending_for_agent",
    "mark_answered",
    "mark_skipped",
    "_is_skip",
]
