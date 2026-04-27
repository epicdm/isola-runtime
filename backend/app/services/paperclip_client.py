"""HTTP client for Paperclip's pull-on-invocation API surface.

Per ADR-0070 / Day 4b: Clawith fetches SOUL.md, desiredSkills, and
SKILL.md content from Paperclip on each invocation -- the tight-coupling
pattern that makes Paperclip canonical for static agent config.

All calls are FAIL-FAST per Eric's Day-4b decision #4 -- the new dispatch
path fails closed when Paperclip is unreachable. internal.py raises
HTTPException(503), BFF then dead-letters the inbound message. This is
distinct from `paperclip_mirror.py` which remains fire-and-forget for
the existing customer-message-fanout plane (Phase B.6).

Env vars (set via docker-compose.override.yml -> /opt/isola-runtime/.env):
  PAPERCLIP_API_URL    Base URL reachable from inside the container
  PAPERCLIP_API_TOKEN  Bearer token for Paperclip Authorization header
"""

from __future__ import annotations

import os

import httpx
from loguru import logger


class PaperclipUnreachable(Exception):
    """Raised when Paperclip is unreachable, returns >=400, or times out.

    Caller (internal.py dispatch handler) propagates as HTTPException(503)
    so BFF can dead-letter the inbound message rather than serving a stale
    or degraded reply to the customer.
    """


PAPERCLIP_API_URL = os.environ.get("PAPERCLIP_API_URL", "")
PAPERCLIP_API_TOKEN = os.environ.get("PAPERCLIP_API_TOKEN", "")
_TIMEOUT_SECONDS = 5.0  # fail-fast: customer is on Meta's retry window


def _headers() -> dict[str, str]:
    if not PAPERCLIP_API_TOKEN:
        raise PaperclipUnreachable("PAPERCLIP_API_TOKEN not set in container env")
    return {
        "Authorization": f"Bearer {PAPERCLIP_API_TOKEN}",
        "Content-Type": "application/json",
    }


async def _get_json(path: str) -> dict:
    if not PAPERCLIP_API_URL:
        raise PaperclipUnreachable("PAPERCLIP_API_URL not set in container env")
    url = f"{PAPERCLIP_API_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            r = await client.get(url, headers=_headers())
    except httpx.HTTPError as e:
        raise PaperclipUnreachable(f"GET {path}: {type(e).__name__}: {e}") from e
    if r.status_code >= 400:
        raise PaperclipUnreachable(f"GET {path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


async def fetch_paperclip_soul(paperclip_agent_id: str) -> str | None:
    """GET /api/agents/{id} -> runtimeConfig.soul (str or None)."""
    body = await _get_json(f"/api/agents/{paperclip_agent_id}")
    soul = (body.get("runtimeConfig") or {}).get("soul")
    if soul is None:
        return None
    if not isinstance(soul, str):
        raise PaperclipUnreachable(
            f"runtimeConfig.soul has unexpected type: {type(soul).__name__}"
        )
    return soul


async def fetch_paperclip_desired_skills(
    paperclip_agent_id: str,
    paperclip_company_id: str,
) -> list[dict]:
    """Return [{key, slug, description, id, sourceLocator}] for each
    customer-facing desired skill (excludes bundled paperclipai/* keys).
    """
    agent_skills = await _get_json(f"/api/agents/{paperclip_agent_id}/skills")
    desired_keys = agent_skills.get("desiredSkills") or []
    company_skills = await _get_json(f"/api/companies/{paperclip_company_id}/skills")
    by_key = {s["key"]: s for s in company_skills}

    result = []
    for key in desired_keys:
        if key.startswith("paperclipai/"):
            continue  # bundled Paperclip-internal, not customer-facing
        skill = by_key.get(key)
        if skill is None:
            logger.warning(
                f"[paperclip_client] desired skill key {key} not found in "
                f"company {paperclip_company_id} skills"
            )
            continue
        result.append({
            "key": key,
            "slug": skill["slug"],
            "description": skill.get("description") or "",
            "id": skill["id"],
            "sourceLocator": skill.get("sourceLocator", ""),
        })
    return result


async def fetch_paperclip_skill_body(
    paperclip_company_id: str,
    skill_id: str,
) -> str:
    """GET /api/companies/{cid}/skills/{id}/files?path=SKILL.md -> body['content']."""
    body = await _get_json(
        f"/api/companies/{paperclip_company_id}/skills/{skill_id}/files?path=SKILL.md"
    )
    content = body.get("content")
    if not isinstance(content, str):
        raise PaperclipUnreachable(
            f"skill {skill_id} files response missing 'content' string field"
        )
    return content
