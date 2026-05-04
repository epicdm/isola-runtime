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
from fastapi import APIRouter, Depends, HTTPException, Query
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


# L4 S3 fix: cross-store join uses BFF tenant_registry.clawithTenantId
# (FK to Clawith local tenants.id) — the slug-derive heuristic only worked
# for saga-provisioned tenants, missing pre-saga rows like EPIC tenant zero
# whose Clawith slug is "epic-c1de29", not iso-{tenantId}.

# Tenants whose status the operator console hides by default. Test rows are
# scaffolding; archived rows are pre-saga seed/legacy that aren't actionable.
_HIDDEN_BY_DEFAULT_STATUSES = {"test", "archived"}


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
    include_test: bool = Query(False, alias="includeTest"),
) -> dict[str, Any]:
    """List tenants joined across BFF tenant_registry + local Clawith tenants.

    BFF is canonical for cross-namespace IDs and deferredOperatorActions;
    local DB provides the runtime-side name + agent count, joined via
    BFF.clawithTenantId -> Clawith.tenants.id.

    By default, hides rows with status in {test, archived}. Pass
    ?includeTest=true to surface them (operator's "show all" toggle).
    """
    bff_data = await _bff_get("/api/internal/cross-store/tenants")
    bff_rows_all: list[dict] = bff_data.get("tenants", [])
    bff_rows = (
        bff_rows_all if include_test
        else [r for r in bff_rows_all if r.get("status") not in _HIDDEN_BY_DEFAULT_STATUSES]
    )

    if not bff_rows:
        return {"total": 0, "tenants": [], "hiddenCount": len(bff_rows_all) - len(bff_rows)}

    # Collect Clawith local IDs from the BFF rows. Skip rows where the FK is
    # null (legacy/partial entries that never bridged to Clawith).
    clawith_ids: list[UUID] = []
    for r in bff_rows:
        cid = r.get("clawithTenantId")
        if not cid:
            continue
        try:
            clawith_ids.append(UUID(cid))
        except (ValueError, TypeError):
            continue

    locals_by_id: dict[str, Tenant] = {}
    agent_counts: dict[UUID, int] = {}
    if clawith_ids:
        result = await db.execute(select(Tenant).where(Tenant.id.in_(clawith_ids)))
        locals_by_id = {str(t.id): t for t in result.scalars().all()}

        ac = await db.execute(
            select(Agent.tenant_id, sqla_func.count())
            .where(Agent.tenant_id.in_(clawith_ids))
            .group_by(Agent.tenant_id)
        )
        agent_counts = {tid: cnt for tid, cnt in ac.all()}

    out: list[dict[str, Any]] = []
    for bff_row in bff_rows:
        cid = bff_row.get("clawithTenantId") or ""
        local = locals_by_id.get(cid)
        local_view = _local_tenant_view(local) if local else None
        if local_view is not None:
            local_view["agent_count"] = agent_counts.get(local.id, 0)
        out.append({"bff": bff_row, "local": local_view})

    return {
        "total": len(out),
        "tenants": out,
        "hiddenCount": len(bff_rows_all) - len(bff_rows),
    }


# ─── Detail endpoint ────────────────────────────────────────────────

@router.get("/tenants/{tenant_id}")
async def get_cross_store_tenant(
    tenant_id: str,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # tenant_id is BFF tenant_registry's PK — heterogeneous shape (UUIDs for
    # saga-provisioned, slugs for pre-saga legacy). Accept any string;
    # validation lives at the storage layer.
    if not tenant_id or len(tenant_id) > 100:
        raise HTTPException(status_code=400, detail="tenant_id must be 1-100 chars")

    bff_data = await _bff_get(f"/api/internal/cross-store/tenants/{tenant_id}")
    if bff_data.get("_status") == 404:
        raise HTTPException(status_code=404, detail="Tenant not found in tenant_registry")
    bff_row = bff_data.get("tenant")
    if not bff_row:
        raise HTTPException(status_code=502, detail="BFF returned malformed response")

    # Join Clawith local via BFF.clawithTenantId (FK), not slug-derive.
    local: Tenant | None = None
    cid = bff_row.get("clawithTenantId")
    if cid:
        try:
            result = await db.execute(select(Tenant).where(Tenant.id == UUID(cid)))
            local = result.scalar_one_or_none()
        except (ValueError, TypeError):
            local = None

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
    tenant_id: str,
    body: ResolveActionBody,
    current_user: User = Depends(require_role("platform_admin")),
) -> dict[str, Any]:
    if not tenant_id or len(tenant_id) > 100:
        raise HTTPException(status_code=400, detail="tenant_id must be 1-100 chars")
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
