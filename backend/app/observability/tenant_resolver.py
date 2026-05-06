"""L5 S1 — Resolve Clawith tenant_id -> Paperclip company_id via BFF /resolve.

Probe-at-trace-time per Q1 sub-question (a) lock at S0 close.
5-min in-memory cache; missing or failing resolution yields None (no trace
tag rather than crash). All failures swallow into best-effort logging.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300  # 5 min
RESOLVE_TIMEOUT_SECONDS = 5.0

# clawith_tenant_id -> (paperclip_company_id, expires_at)
_cache: dict[str, tuple[Optional[str], float]] = {}
_lock = asyncio.Lock()


async def resolve_paperclip_company_id(clawith_tenant_id: Optional[str]) -> Optional[str]:
    """Resolve a Clawith tenant UUID to a Paperclip company UUID.

    Returns None on missing input, BFF unavailable, auth failure, or 404.
    Caches positive AND negative answers for 5 minutes.
    """
    if not clawith_tenant_id:
        return None

    now = time.time()
    cached = _cache.get(clawith_tenant_id)
    if cached and cached[1] > now:
        return cached[0]

    bff_base = os.environ.get("BFF_API_BASE_URL") or "https://bff.epic.dm"
    secret = os.environ.get("BFF_INTERNAL_SECRET")
    if not secret:
        logger.warning("[langfuse:resolve] BFF_INTERNAL_SECRET unset; cannot resolve")
        return None

    url = f"{bff_base.rstrip('/')}/api/internal/cross-store/clawith-tenants/{clawith_tenant_id}"
    paperclip_id: Optional[str] = None
    try:
        async with httpx.AsyncClient(timeout=RESOLVE_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers={"X-Internal-Secret": secret})
            if resp.status_code == 200:
                data = resp.json()
                paperclip_id = data.get("paperclipCompanyId")
            elif resp.status_code != 404:
                logger.warning(
                    "[langfuse:resolve] unexpected status %d for %s",
                    resp.status_code, clawith_tenant_id,
                )
    except Exception as e:  # noqa: BLE001 — never break agent path
        logger.warning("[langfuse:resolve] exception: %s", e)

    async with _lock:
        _cache[clawith_tenant_id] = (paperclip_id, now + CACHE_TTL_SECONDS)
    return paperclip_id
