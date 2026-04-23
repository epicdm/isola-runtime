"""Escalation detection + operator ping (Phase B.6).

Port of BFF v2's app/lib/escalation.ts. If a customer's inbound message
contains one of the agent's escalation keywords, ping the agent's owner
(Agent.owner_phone) via WhatsApp with a short summary + the customer's
phone number so the operator can jump in.

Fire-and-forget + error-swallowing — escalation failures must not
break the main inbound->LLM->reply pipeline.

Keyword resolution order:
  1. agent.escalation_keywords (per-agent JSON list)
  2. ESCALATION_KEYWORDS env var (comma-separated)
  3. Built-in defaults below

Dedup: skip a second ping if one already fired for this (agent_id,
customer_phone) pair within PING_DEDUP_WINDOW_MINUTES. Held in-process
for MVP; persistence via the EscalationPing model is a later addition.
"""

from __future__ import annotations

import os
import time
import uuid

from loguru import logger

from app.services.whatsapp_service import whatsapp_service


DEFAULT_KEYWORDS = [
    "complaint", "refund", "urgent", "manager", "supervisor", "speak to human",
    "angry", "lawsuit", "cancel my", "scam", "fraud", "stolen",
    "not working", "doesn't work", "broken", "terrible", "worst",
]


def _load_env_keywords() -> list[str] | None:
    raw = os.environ.get("ESCALATION_KEYWORDS", "").strip()
    if not raw:
        return None
    parsed = [s.strip().lower() for s in raw.split(",") if s.strip()]
    return parsed or None


_ENV_KEYWORDS = _load_env_keywords()

PING_DEDUP_WINDOW_MINUTES = 30
_recent_pings: dict[tuple[str, str], float] = {}


def resolve_keywords(agent_escalation_keywords: list | None) -> list[str]:
    """Return the effective keyword list for an agent (lowercased)."""
    per_agent = agent_escalation_keywords or []
    cleaned = [str(k).strip().lower() for k in per_agent if isinstance(k, str) and k.strip()]
    if cleaned:
        return cleaned
    if _ENV_KEYWORDS:
        return _ENV_KEYWORDS
    return DEFAULT_KEYWORDS


def detect(text: str, agent_escalation_keywords: list | None = None) -> tuple[bool, list[str]]:
    """Return (matched, [hit_keywords]). Case-insensitive substring match."""
    if not text:
        return False, []
    lower = text.lower()
    hits = [kw for kw in resolve_keywords(agent_escalation_keywords) if kw in lower]
    return bool(hits), hits


def _dedup_key(agent_id: uuid.UUID, customer_phone: str) -> tuple[str, str]:
    return (str(agent_id), customer_phone)


def _is_duplicate(agent_id: uuid.UUID, customer_phone: str) -> bool:
    now = time.monotonic()
    cutoff = now - PING_DEDUP_WINDOW_MINUTES * 60
    # Opportunistic cleanup of expired entries
    for k in list(_recent_pings):
        if _recent_pings[k] < cutoff:
            del _recent_pings[k]
    last = _recent_pings.get(_dedup_key(agent_id, customer_phone))
    return last is not None and last >= cutoff


def _record_ping(agent_id: uuid.UUID, customer_phone: str) -> None:
    _recent_pings[_dedup_key(agent_id, customer_phone)] = time.monotonic()


async def ping_operator(
    *,
    agent_id: uuid.UUID,
    agent_name: str,
    owner_phone: str | None,
    phone_number_id: str,
    access_token: str,
    customer_phone: str,
    customer_name: str | None,
    message_body: str,
    keywords: list[str],
    paperclip_issue_id: str | None = None,
) -> bool:
    """Send a priority ping to the agent's owner via WhatsApp.

    Returns True if sent, False on any failure or dedup skip. Never raises.
    """
    if not owner_phone:
        return False
    if _is_duplicate(agent_id, customer_phone):
        logger.info(
            f"[Escalation] dedup'd — recent ping exists for agent={agent_id} "
            f"customer={customer_phone}"
        )
        return False

    cust = customer_name or customer_phone
    kw_summary = ", ".join(keywords[:4]) if keywords else "flagged"
    snippet = (message_body or "").strip().replace("\n", " ")[:180]
    body = (
        f"🚨 Escalation: {agent_name or 'Agent'} flagged a message from {cust}.\n"
        f"Keywords: {kw_summary}\n"
        f"Snippet: \"{snippet}\"\n"
        f"Customer phone: +{customer_phone}"
    )
    if paperclip_issue_id:
        body += f"\nPaperclip issue: {paperclip_issue_id}"
    try:
        await whatsapp_service.send_text_message(
            phone_number_id=phone_number_id,
            access_token=access_token,
            to=owner_phone,
            text=body,
        )
        _record_ping(agent_id, customer_phone)
        return True
    except Exception as e:
        logger.warning(f"[Escalation] ping_operator failed: {e}")
        return False


def reset_dedup() -> None:
    """Testing hook — clear the in-process dedup cache."""
    _recent_pings.clear()


__all__ = [
    "detect",
    "resolve_keywords",
    "ping_operator",
    "reset_dedup",
    "DEFAULT_KEYWORDS",
    "PING_DEDUP_WINDOW_MINUTES",
]
