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


# ── S5 R22-R25 (2026-05-04): Paperclip writer methods ─────────────────────────
# Added for L4 S5 SOUL editor + skill attachment surface.
# - update_paperclip_soul: PUT /api/agents/{id}/instructions-bundle/file
# - update_paperclip_desired_skills: POST /api/agents/{id}/skills/sync
# Both reuse _headers() (PCP_BOARD_TOKEN auth per R24) + 5s fail-fast timeout.
# R23: Wave-1 last-write-wins; no etag/revision guard.


async def _request_json(method: str, path: str, body: dict | None = None) -> dict:
    """Generic Paperclip HTTP helper for non-GET methods.

    Mirrors _get_json semantics. _get_json kept unchanged for diff minimization;
    future refactor can collapse both into _request_json('GET', ...).
    """
    if not PAPERCLIP_API_URL:
        raise PaperclipUnreachable("PAPERCLIP_API_URL not set in container env")
    url = f"{PAPERCLIP_API_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            r = await client.request(method, url, headers=_headers(), json=body)
    except httpx.HTTPError as e:
        raise PaperclipUnreachable(f"{method} {path}: {type(e).__name__}: {e}") from e
    if r.status_code >= 400:
        raise PaperclipUnreachable(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


async def update_paperclip_soul(paperclip_agent_id: str, soul_md: str) -> dict:
    """PUT /api/agents/{id}/instructions-bundle/file body {path: SOUL.md, content}.

    S5 R22 (2026-05-04): no companyId in path; server derives from agent record.
    S5 R23: Wave-1 last-write-wins; no etag/revision guard.
    """
    body = {"path": "SOUL.md", "content": soul_md}
    return await _request_json(
        "PUT",
        f"/api/agents/{paperclip_agent_id}/instructions-bundle/file",
        body,
    )


async def update_paperclip_desired_skills(
    paperclip_agent_id: str,
    desired_skill_keys: list[str],
) -> dict:
    """POST /api/agents/{id}/skills/sync body {desiredSkills: [...]}.

    S5 R22 (2026-05-04): dedicated sync endpoint; server resolves + validates;
    Paperclip returns 422 on empty desiredSkills (we surface as PaperclipUnreachable).
    S5 R25: caller should resolve _resolve_paperclip_ids first for clean 409s.
    """
    body = {"desiredSkills": desired_skill_keys}
    return await _request_json(
        "POST",
        f"/api/agents/{paperclip_agent_id}/skills/sync",
        body,
    )
