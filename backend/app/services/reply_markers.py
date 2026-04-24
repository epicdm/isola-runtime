"""Phase F.1.c-2 — outbound reply marker parser.

The LLM writes its reply as plain text that gets delivered to WhatsApp as-is.
Rex's skills teach the LLM to append markers like `[escalate: reason]` to
trigger side-effects (escalation ping, location send, file attach, pay link).

This module scans the LLM's reply for those markers, dispatches the matching
action asynchronously, and returns the cleaned reply text (with markers
stripped) for delivery to the customer.

Supported markers (Phase F.1.c-2 — escalate only):
  [escalate: <reason>]   fire escalation ping to owner

Planned for F.1.c-3:
  [attach:<path>]               send workspace file as WA media
  [location:lat=...,lon=...,...] send WA location message
  [pay:amount=N;ref=X]          generate Fiserv pay link
  [template:name=X;params=...]  send Meta template (for >24h outbound)
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Single regex that captures ANY known marker. Each named group corresponds
# to one marker type. Expand here when adding new markers.
_MARKER_RE = re.compile(
    r"\[escalate:\s*(?P<escalate>[^\]\n]+?)\s*\]",
    re.IGNORECASE,
)


@dataclass
class ReplyContext:
    """Everything the dispatchers might need. Caller fills this from the
    inbound webhook's live state and passes it in unchanged."""

    agent_id: uuid.UUID
    agent_name: str
    owner_phone: str | None
    phone_number_id: str
    access_token: str
    customer_phone: str
    customer_name: str | None
    conversation_id: uuid.UUID | None
    paperclip_issue_id: str | None
    # The triggering inbound message, for escalation context.
    last_inbound: str


async def parse_and_dispatch(reply_text: str, ctx: ReplyContext) -> str:
    """Strip markers from `reply_text`, dispatch their actions, return cleaned text.

    Never raises: a broken marker logs a warning and is stripped silently so
    the customer never sees raw syntax. Dispatch failures log a warning and
    proceed — the reply still goes out.
    """
    if not reply_text:
        return reply_text

    matches = list(_MARKER_RE.finditer(reply_text))
    if not matches:
        return reply_text

    for m in matches:
        try:
            if (reason := m.group("escalate")):
                await _dispatch_escalate(reason, ctx)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[reply_markers] dispatch failed for marker "
                f"{m.group(0)!r} on agent={ctx.agent_id}: {e}"
            )

    # Strip all marker occurrences. Collapse any blank lines that result
    # from stripping a marker that was on its own line.
    cleaned = _MARKER_RE.sub("", reply_text)
    cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned).strip()
    return cleaned


async def _dispatch_escalate(reason: str, ctx: ReplyContext) -> None:
    """Fire the escalation ping to the owner. Uses the same dedup window as
    keyword-triggered escalations (both paths share escalation._record_ping),
    so Rex saying `[escalate: complaint]` twice in 10 minutes won't double-ping.
    """
    from app.services import escalation

    reason_clean = reason.strip()[:60] or "unspecified"

    sent = await escalation.ping_operator(
        agent_id=ctx.agent_id,
        agent_name=ctx.agent_name,
        owner_phone=ctx.owner_phone,
        phone_number_id=ctx.phone_number_id,
        access_token=ctx.access_token,
        customer_phone=ctx.customer_phone,
        customer_name=ctx.customer_name,
        message_body=ctx.last_inbound,
        keywords=[f"llm:{reason_clean}"],
        paperclip_issue_id=ctx.paperclip_issue_id,
    )
    if sent:
        logger.info(
            f"[reply_markers] escalate fired for agent={ctx.agent_id} "
            f"reason={reason_clean!r}"
        )
    else:
        logger.info(
            f"[reply_markers] escalate suppressed (dedup or no owner_phone) "
            f"for agent={ctx.agent_id}"
        )
