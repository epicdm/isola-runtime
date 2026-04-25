"""Phase F.1.c-2 — outbound reply marker parser.

The LLM writes its reply as plain text that gets delivered to WhatsApp as-is.
Rex's skills teach the LLM to append markers like `[escalate: reason]` to
trigger side-effects (escalation ping, location send, file attach, pay link).

This module scans the LLM's reply for those markers, dispatches the matching
action asynchronously, and returns the cleaned reply text (with markers
stripped) for delivery to the customer.

Supported markers:
  [escalate: <reason>]                              fire escalation ping (F.1.c-2)
  [location: lat=N,lon=N[,name="..."][,address="..."]]  send WA location message (F.1.c-3)
  [ask_owner: <question>]                           live owner-ask loop (F.1.5a / #109)

Planned:
  [attach:<path>]               send workspace file as WA media (F.1.c-3b)
  [pay:amount=N;ref=X]          generate Fiserv pay link (F.1.d)
  [template:name=X;params=...]  Meta template send — prefer the
                                whatsapp_send_template LLM tool instead.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Single regex that matches any marker. We then dispatch by kind based on
# which alternative matched (captured in the `kind` named group).
_MARKER_RE = re.compile(
    r"\[(?P<kind>escalate|location|ask_owner)\s*:\s*(?P<body>[^\]\n]+?)\s*\]",
    re.IGNORECASE,
)

# [location: lat=X,lon=Y,name="...",address="..."] — body is the key=value
# list after the colon. Values support both quoted and unquoted forms.
_LOCATION_KV_RE = re.compile(
    r'(?P<key>\w+)\s*=\s*(?:"(?P<qv>[^"]*)"|(?P<v>[^,]+?))(?=\s*,|\s*$)',
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
        kind = (m.group("kind") or "").lower()
        body = m.group("body") or ""
        try:
            if kind == "escalate":
                await _dispatch_escalate(body, ctx)
            elif kind == "location":
                await _dispatch_location(body, ctx)
            elif kind == "ask_owner":
                await _dispatch_ask_owner(body, ctx)
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


async def _dispatch_location(body: str, ctx: ReplyContext) -> None:
    """Send a native WA location message to the customer.

    Marker grammar: `[location: lat=15.3,lon=-61.38,name="Isola Bistro",address="34 Great Marlborough St"]`
    Only lat+lon are required; name/address are optional. Silently skips on
    malformed coords so the customer still gets the cleaned reply text.
    """
    kv: dict[str, str] = {}
    for m in _LOCATION_KV_RE.finditer(body):
        key = m.group("key").lower()
        value = m.group("qv") if m.group("qv") is not None else (m.group("v") or "").strip()
        kv[key] = value

    try:
        lat = float(kv.get("lat") or kv.get("latitude") or "")
        lon = float(kv.get("lon") or kv.get("longitude") or "")
    except ValueError:
        logger.warning(
            f"[reply_markers] location marker missing or invalid lat/lon "
            f"for agent={ctx.agent_id}: {body!r}"
        )
        return

    from app.services.whatsapp_service import WhatsAppService

    svc = WhatsAppService()
    await svc.send_location(
        phone_number_id=ctx.phone_number_id,
        access_token=ctx.access_token,
        to=ctx.customer_phone,
        latitude=lat,
        longitude=lon,
        name=kv.get("name", ""),
        address=kv.get("address", ""),
    )
    logger.info(
        f"[reply_markers] location sent for agent={ctx.agent_id} "
        f"lat={lat} lon={lon} name={kv.get('name', '')!r}"
    )


async def _dispatch_ask_owner(question: str, ctx: ReplyContext) -> None:
    """Live owner-ask loop trigger (Tier 1.5a / #109).

    1. Insert OwnerAskInFlight row (so the WA inbound webhook can pair
       the owner's reply back to this customer's conversation).
    2. WA-ping the owner with the customer's question.

    The customer-facing wait message is whatever the LLM wrote around
    the marker (typically "Give me a moment, checking with Eric.").
    Marker is stripped before send. If owner_phone is missing, we silently
    drop — knowledge_gap_capture catches the same question as a fallback.
    """
    q = question.strip()
    if not q:
        logger.warning(
            f"[reply_markers] ask_owner empty question for agent={ctx.agent_id}"
        )
        return
    if not ctx.owner_phone:
        logger.info(
            f"[reply_markers] ask_owner skipped (no owner_phone) for "
            f"agent={ctx.agent_id}; gap-capture will catch it"
        )
        return

    from app.database import async_session
    from app.services import owner_ask
    from app.services.whatsapp_service import whatsapp_service

    async with async_session() as db:
        await owner_ask.create_ask(
            db,
            agent_id=ctx.agent_id,
            customer_phone=ctx.customer_phone,
            customer_name=ctx.customer_name,
            customer_conversation_id=ctx.conversation_id,
            question=q,
            owner_phone=ctx.owner_phone,
        )
        await db.commit()

    cust = ctx.customer_name or ctx.customer_phone
    snippet = q[:300]
    body = (
        f"❓ {ctx.agent_name or 'Agent'} needs your help.\n"
        f"{cust} just asked: \"{snippet}\"\n"
        f"Reply with the answer (I'll teach Rex and pass it on), "
        f"or type 'skip' to handle later."
    )
    try:
        await whatsapp_service.send_text_message(
            phone_number_id=ctx.phone_number_id,
            access_token=ctx.access_token,
            to=ctx.owner_phone,
            text=body,
        )
        logger.info(
            f"[reply_markers] ask_owner sent for agent={ctx.agent_id} "
            f"q={q[:60]!r}"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[reply_markers] ask_owner ping failed for agent={ctx.agent_id}: {e}"
        )
