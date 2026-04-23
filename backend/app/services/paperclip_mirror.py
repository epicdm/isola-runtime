"""Paperclip management-plane mirror for the WhatsApp adapter (Phase B.6).

Port of BFF v2's app/lib/paperclip.ts pattern. For every inbound and
outbound customer message, append a shadow comment on Paperclip's
one-issue-per-customer thread so operators see the conversation in the
Paperclip UI alongside the native ChatMessage history.

All calls are fire-and-forget + error-swallowing. A Paperclip outage
must never break customer messaging.

Config (env):
  PAPERCLIP_API_URL          Base URL (default: http://127.0.0.1:3200)
  PAPERCLIP_API_TOKEN        Bearer token (required; mirror no-ops without it)
  PAPERCLIP_FANOUT_ENABLED   "1" or "true" to enable (default: off)
  EPIC_PAPERCLIP_COMPANY_ID  Fallback company_id when agent has none set
"""

from __future__ import annotations

import os
import uuid

import httpx
from loguru import logger

PAPERCLIP_API_URL = os.environ.get("PAPERCLIP_API_URL") or "http://127.0.0.1:3200"
PAPERCLIP_API_TOKEN = os.environ.get("PAPERCLIP_API_TOKEN") or ""
PAPERCLIP_FANOUT_ENABLED = os.environ.get("PAPERCLIP_FANOUT_ENABLED", "").lower() in (
    "1",
    "true",
    "yes",
)
EPIC_PAPERCLIP_COMPANY_ID = (
    os.environ.get("EPIC_PAPERCLIP_COMPANY_ID", "").strip() or None
)


def is_enabled() -> bool:
    """Gate flag — mirror is a no-op unless both enabled + token set."""
    return PAPERCLIP_FANOUT_ENABLED and bool(PAPERCLIP_API_TOKEN)


def resolve_company_id(agent_company_id: str | None) -> str | None:
    """Prefer agent-specific paperclip_company_id, fall back to env default."""
    return (agent_company_id or "").strip() or EPIC_PAPERCLIP_COMPANY_ID


async def _fetch(path: str, method: str = "GET", json_body: dict | None = None) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {PAPERCLIP_API_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await client.request(
            method, f"{PAPERCLIP_API_URL}{path}", headers=headers, json=json_body
        )


async def create_issue(
    *,
    company_id: str,
    title: str,
    description: str | None = None,
    priority: str = "medium",
    assignee_agent_id: str | None = None,
) -> dict | None:
    """POST /api/companies/{company_id}/issues. Returns the created issue dict or None."""
    if not is_enabled():
        return None
    body: dict = {"title": title, "priority": priority}
    if description:
        body["description"] = description
    if assignee_agent_id:
        body["assigneeAgentId"] = assignee_agent_id
    try:
        resp = await _fetch(f"/api/companies/{company_id}/issues", "POST", body)
        if resp.status_code >= 400:
            logger.warning(
                f"[Paperclip] create_issue failed status={resp.status_code} "
                f"body={resp.text[:200]}"
            )
            return None
        return resp.json()
    except Exception as e:
        logger.warning(f"[Paperclip] create_issue exception: {e}")
        return None


async def post_comment(*, issue_id: str, body: str) -> bool:
    """POST /api/issues/{issue_id}/comments. Returns True on 2xx."""
    if not is_enabled() or not issue_id:
        return False
    try:
        resp = await _fetch(f"/api/issues/{issue_id}/comments", "POST", {"body": body})
        if resp.status_code >= 400:
            logger.warning(
                f"[Paperclip] post_comment failed status={resp.status_code} "
                f"body={resp.text[:200]}"
            )
            return False
        return True
    except Exception as e:
        logger.warning(f"[Paperclip] post_comment exception: {e}")
        return False


async def ensure_issue_for_session(
    *,
    agent_name: str | None,
    customer_name: str | None,
    customer_phone: str,
    paperclip_company_id: str | None,
    paperclip_agent_id: str | None,
    existing_issue_id: str | None,
    first_message_snippet: str,
) -> str | None:
    """Return an open Paperclip issue id for this customer; create if missing.

    If ChatSession already has paperclip_issue_id, return it as-is (caller may
    want to check status later). Otherwise create_issue and return the new id.

    Caller is expected to persist the returned id on the session.
    """
    if not is_enabled():
        return None
    if existing_issue_id:
        return existing_issue_id
    company_id = resolve_company_id(paperclip_company_id)
    if not company_id:
        logger.warning(
            "[Paperclip] No company_id (agent has none and EPIC_PAPERCLIP_COMPANY_ID unset)"
        )
        return None

    name_frag = agent_name or "Isola"
    cust_frag = customer_name or customer_phone
    snippet = (first_message_snippet or "").strip()[:80]
    title = f"{name_frag} · {cust_frag} — {snippet}" if snippet else f"{name_frag} · {cust_frag}"

    issue = await create_issue(
        company_id=company_id,
        title=title[:200],
        description=f"Customer phone: {customer_phone}",
        priority="medium",
        assignee_agent_id=paperclip_agent_id,
    )
    return (issue or {}).get("id")


async def mirror_message(*, issue_id: str, direction: str, body: str) -> bool:
    """Post a comment tagged with direction label (inbound / outbound)."""
    prefix = "🟦 Customer" if direction == "inbound" else "🟩 Agent"
    return await post_comment(issue_id=issue_id, body=f"{prefix}: {body}")


__all__ = [
    "is_enabled",
    "resolve_company_id",
    "create_issue",
    "post_comment",
    "ensure_issue_for_session",
    "mirror_message",
    "PAPERCLIP_API_URL",
    "EPIC_PAPERCLIP_COMPANY_ID",
]
