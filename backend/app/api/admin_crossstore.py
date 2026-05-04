"""Cross-store admin: aggregates BFF tenant_registry + local Clawith tenants
for the platform_admin operator UI.

Auth: Bearer JWT + platform_admin role (matches /api/admin/companies family).
The runtime acts as an ambassador, calling BFF /api/internal/cross-store/*
server-side with X-Internal-Secret. The frontend never sees the internal
secret -- it stays server-side in BFF_INTERNAL_SECRET env.

URL family convention enforced by L4 S2 ratification:
    /api/admin/*    = human-facing-auth (Bearer JWT, role-gated)
    /api/internal/* = service-to-service (X-Internal-Secret)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func as sqla_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import require_role
from app.database import get_db
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import User

router = APIRouter(prefix="/admin/cross-store", tags=["admin", "cross-store"])

_BFF_TIMEOUT_SECONDS = 8.0


def _slugify_external(external_id: str) -> str:
    # Mirrors app.api.internal._slugify_external; duplicated to keep this
    # router free of internal.py's broader import surface.
    return "".join(c.lower() if c.isalnum() else "-" for c in external_id)


def _bff_slug(tenant_id: str) -> str:
    return f"iso-{_slugify_external(tenant_id)}"


def _bff_config() -> tuple[str, dict[str, str]]:
    settings = get_settings()
    base = (settings.BFF_API_BASE_URL or "").rstrip("/")
    secret = settings.BFF_INTERNAL_SECRET
    if not base or not secret:
        raise HTTPException(
            status_code=503,
            detail="BFF outbound not configured (BFF_API_BASE_URL / BFF_INTERNAL_SECRET)",
        )
    return base, {"x-internal-secret": secret}


async def _bff_get(path: str) -> dict:
    base, headers = _bff_config()
    try:
        async with httpx.AsyncClient(timeout=_BFF_TIMEOUT_SECONDS) as client:
            r = await client.get(f"{base}{path}", headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"BFF unreachable: {type(e).__name__}: {e}")
    if r.status_code == 404:
        return {"_status": 404}
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"BFF GET {path}: HTTP {r.status_code}")
    return r.json()


async def _bff_post(path: str, body: dict) -> tuple[int, dict]:
    base, headers = _bff_config()
    headers = {**headers, "content-type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_BFF_TIMEOUT_SECONDS) as client:
            r = await client.post(f"{base}{path}", headers=headers, json=body)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"BFF unreachable: {type(e).__name__}: {e}")
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:200]}
    return r.status_code, data


def _local_tenant_view(t: Tenant) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "name": t.name,
        "slug": t.slug,
        "is_active": t.is_active,
        "runtime_mode": t.runtime_mode,
        "im_provider": t.im_provider,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _agent_view(a: Agent) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "name": a.name,
        "agent_type": a.agent_type,
        "role_description": a.role_description,
        "owner_phone": a.owner_phone,
        "paperclip_agent_id": a.paperclip_agent_id,
        "paperclip_company_id": a.paperclip_company_id,
        "container_port": a.container_port,
    }


# ─── List endpoint ──────────────────────────────────────────────────

@router.get("/tenants")
async def list_cross_store_tenants(
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List tenants joined across BFF tenant_registry + local Clawith tenants.

    BFF is canonical for cross-namespace IDs and deferredOperatorActions;
    local DB provides the runtime-side name/slug + agent count.
    """
    bff_data = await _bff_get("/api/internal/cross-store/tenants")
    bff_rows: list[dict] = bff_data.get("tenants", [])

    if not bff_rows:
        return {"total": 0, "tenants": []}

    slugs = [_bff_slug(row["tenantId"]) for row in bff_rows]
    result = await db.execute(select(Tenant).where(Tenant.slug.in_(slugs)))
    locals_by_slug = {t.slug: t for t in result.scalars().all()}

    local_ids = [t.id for t in locals_by_slug.values()]
    agent_counts: dict = {}
    if local_ids:
        ac = await db.execute(
            select(Agent.tenant_id, sqla_func.count())
            .where(Agent.tenant_id.in_(local_ids))
            .group_by(Agent.tenant_id)
        )
        agent_counts = {tid: cnt for tid, cnt in ac.all()}

    out: list[dict[str, Any]] = []
    for bff_row in bff_rows:
        slug = _bff_slug(bff_row["tenantId"])
        local = locals_by_slug.get(slug)
        local_view = _local_tenant_view(local) if local else None
        if local_view is not None:
            local_view["agent_count"] = agent_counts.get(local.id, 0)
        out.append({"bff": bff_row, "local": local_view})

    return {"total": len(out), "tenants": out}


# ─── Detail endpoint ────────────────────────────────────────────────

@router.get("/tenants/{tenant_id}")
async def get_cross_store_tenant(
    tenant_id: UUID,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    bff_data = await _bff_get(f"/api/internal/cross-store/tenants/{tenant_id}")
    if bff_data.get("_status") == 404:
        raise HTTPException(status_code=404, detail="Tenant not found in tenant_registry")
    bff_row = bff_data.get("tenant")
    if not bff_row:
        raise HTTPException(status_code=502, detail="BFF returned malformed response")

    slug = _bff_slug(str(tenant_id))
    result = await db.execute(select(Tenant).where(Tenant.slug == slug))
    local = result.scalar_one_or_none()

    local_view = _local_tenant_view(local) if local else None
    agents: list[dict[str, Any]] = []
    if local:
        agents_q = await db.execute(
            select(Agent).where(Agent.tenant_id == local.id).order_by(Agent.name)
        )
        agents = [_agent_view(a) for a in agents_q.scalars().all()]

    return {"bff": bff_row, "local": local_view, "agents": agents}


# ─── Operator queue ─────────────────────────────────────────────────

@router.get("/operator-queue")
async def list_operator_queue(
    current_user: User = Depends(require_role("platform_admin")),
) -> dict[str, Any]:
    """Flattened deferredOperatorActions across all tenants. No status
    filter — the queue is the operator's daily-work surface and shouldn't
    hide actionable items even on test rows."""
    return await _bff_get("/api/internal/cross-store/operator-queue")


# ─── Resolve action ─────────────────────────────────────────────────

class ResolveActionBody(BaseModel):
    actionKind: str
    resolutionPayload: Any | None = None


@router.post("/tenants/{tenant_id}/resolve-action")
async def resolve_action(
    tenant_id: UUID,
    body: ResolveActionBody,
    current_user: User = Depends(require_role("platform_admin")),
) -> dict[str, Any]:
    bff_body: dict[str, Any] = {"actionKind": body.actionKind}
    if body.resolutionPayload is not None:
        bff_body["resolutionPayload"] = body.resolutionPayload
    status, data = await _bff_post(
        f"/api/internal/cross-store/tenants/{tenant_id}/resolve-action",
        bff_body,
    )
    if status >= 400:
        # Surface BFF's error verbatim (400/404/409 are all caller-actionable)
        raise HTTPException(status_code=status, detail=data.get("error", "BFF resolve-action failed"))
    return data
