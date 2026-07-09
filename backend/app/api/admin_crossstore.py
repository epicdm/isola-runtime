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
from uuid import UUID, uuid4

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
        # L4 S7 Phase 2 (2026-05-05): expose retired_at + retired_by for operator UI gating
        "retired_at": getattr(t, "retired_at", None).isoformat() if getattr(t, "retired_at", None) else None,
        "retired_by": str(getattr(t, "retired_by", None)) if getattr(t, "retired_by", None) else None,
    }


def _agent_view(a: Agent) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "name": a.name,
        "agent_type": a.agent_type,
        "role_description": a.role_description,
        "welcome_message": a.welcome_message,
        "tone": a.tone,
        "status": a.status.value if hasattr(a.status, "value") else str(a.status),
        "owner_phone": a.owner_phone,
        "paperclip_agent_id": a.paperclip_agent_id,
        "paperclip_company_id": a.paperclip_company_id,
        "container_port": a.container_port,
        "retired_at": a.retired_at.isoformat() if a.retired_at else None,
        "retired_by": str(a.retired_by) if a.retired_by else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


async def _resolve_clawith_tenant_id(tenant_id: str) -> tuple[dict, UUID]:
    """Verify tenant_registry row exists, return (bff_row, clawith_uuid).
    Raises 404 if no tenant; 409 if tenant has no clawithTenantId; 502
    if clawithTenantId is malformed."""
    bff_data = await _bff_get(f"/api/internal/cross-store/tenants/{tenant_id}")
    if bff_data.get("_status") == 404:
        raise HTTPException(status_code=404, detail="Tenant not found in tenant_registry")
    bff_row = bff_data.get("tenant", {})
    if not bff_row:
        raise HTTPException(status_code=502, detail="BFF returned malformed response")
    cid = bff_row.get("clawithTenantId")
    if not cid:
        raise HTTPException(
            status_code=409,
            detail="Tenant has no clawithTenantId; agent CRUD requires a linked Clawith tenant",
        )
    try:
        return bff_row, UUID(cid)
    except (ValueError, TypeError):
        raise HTTPException(status_code=502, detail="Malformed clawithTenantId in tenant_registry")


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


async def _bff_patch(path: str, body: dict) -> tuple[int, dict]:
    base, headers = _bff_config()
    headers = {**headers, "content-type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_BFF_TIMEOUT_SECONDS) as client:
            r = await client.patch(f"{base}{path}", headers=headers, json=body)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"BFF unreachable: {type(e).__name__}: {e}")
    return r.status_code, (r.json() if r.content else {})


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


# --- Provision new tenant (drift retreat S3) ---

class ProvisionTenantBody(BaseModel):
    businessName: str
    ownerEmail: str
    plan: str = "base"
    didSource: str = "saga"
    tone: str = "direct-curious-helpful"
    agentTemplate: str | None = None
    test_mode: str | None = None
    meta_profile: str | None = None


@router.post("/provision", status_code=202)
async def provision_tenant(
    body: ProvisionTenantBody,
    current_user: User = Depends(require_role("platform_admin")),
) -> dict[str, Any]:
    """Trigger the tenant-provision Inngest saga.
    Generates tenantId + ownerUserId server-side; operator supplies
    businessName, ownerEmail, plan, didSource, tone.
    Auth: platform_admin Bearer JWT (same as all /api/admin/* routes).
    Proxies to POST /api/platform/trigger-provision on BFF.
    """
    tenant_id = str(uuid4())
    owner_user_id = str(uuid4())
    bff_body = {
        "tenantId": tenant_id,
        "businessName": body.businessName,
        "ownerEmail": body.ownerEmail,
        "ownerUserId": owner_user_id,
        "plan": body.plan,
        "didSource": body.didSource,
        "tone": body.tone,
        **({"agentTemplate": body.agentTemplate} if body.agentTemplate else {}),
        **({"_testMode": body.test_mode} if body.test_mode else {}),
        **({"metaProfile": body.meta_profile} if body.meta_profile else {}),
    }
    status, data = await _bff_post("/api/platform/trigger-provision", bff_body)
    if status >= 400:
        raise HTTPException(
            status_code=status,
            detail=data.get("error", "BFF trigger-provision failed"),
        )
    return {"status": "queued", "tenantId": tenant_id, "businessName": body.businessName}


# --- DID edit (drift retreat S4) ---

class PatchDidBody(BaseModel):
    channel: str | None = None
    agentId: str | None = None


@router.patch("/tenants/{tenant_id}/dids/{did_id}")
async def patch_tenant_did(
    tenant_id: str,
    did_id: str,
    body: PatchDidBody,
    current_user: User = Depends(require_role("platform_admin")),
) -> dict[str, Any]:
    if body.channel is None and body.agentId is None:
        raise HTTPException(status_code=400, detail="Provide at least one of: channel, agentId")
    bff_body: dict[str, Any] = {}
    if body.channel is not None:
        bff_body["channel"] = body.channel
    if body.agentId is not None:
        bff_body["agentId"] = body.agentId
    status, data = await _bff_patch(
        f"/api/internal/operator/dids/{did_id}",
        bff_body,
    )
    if status >= 400:
        raise HTTPException(status_code=status, detail=data.get("error", "BFF DID patch failed"))
    return data


# ─── Agent CRUD (S4) ─────────────────────────────────────────────────
# Direct ORM access to local Clawith agents table. Per ratification 16,
# agent data is Clawith-owned (BFF has no agents model); no BFF hop.
# Tenant validation goes through BFF (_bff_get -> clawithTenantId), then
# all CRUD is local SQLAlchemy on the Tenant + Agent tables.

import uuid as _uuidlib
from datetime import datetime as _datetime, timezone as _timezone


class AgentCreateBody(BaseModel):
    name: str
    role_description: str | None = None
    welcome_message: str | None = None
    tone: int | None = None


class AgentUpdateBody(BaseModel):
    name: str | None = None
    role_description: str | None = None
    welcome_message: str | None = None
    tone: int | None = None


def _validate_agent_id(agent_id: str) -> _uuidlib.UUID:
    if not agent_id or len(agent_id) > 100:
        raise HTTPException(status_code=400, detail="agent_id must be 1-100 chars")
    try:
        return _uuidlib.UUID(agent_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="agent_id must be a UUID")


@router.get("/tenants/{tenant_id}/agents")
async def list_agents(
    tenant_id: str,
    include_retired: bool = Query(False, alias="includeRetired"),
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not tenant_id or len(tenant_id) > 100:
        raise HTTPException(status_code=400, detail="tenant_id must be 1-100 chars")
    _, clawith_uuid = await _resolve_clawith_tenant_id(tenant_id)

    q = select(Agent).where(Agent.tenant_id == clawith_uuid)
    if not include_retired:
        q = q.where(Agent.retired_at.is_(None))
    q = q.order_by(Agent.created_at.desc())

    result = await db.execute(q)
    agents = result.scalars().all()
    return {"total": len(agents), "agents": [_agent_view(a) for a in agents]}


@router.post("/tenants/{tenant_id}/agents", status_code=201)
async def create_agent(
    tenant_id: str,
    body: AgentCreateBody,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not tenant_id or len(tenant_id) > 100:
        raise HTTPException(status_code=400, detail="tenant_id must be 1-100 chars")
    _, clawith_uuid = await _resolve_clawith_tenant_id(tenant_id)

    if body.tone is not None and (body.tone < 0 or body.tone > 2):
        raise HTTPException(status_code=400, detail="tone must be 0..2 inclusive")

    new_id = _uuidlib.uuid4()
    new_agent = Agent(
        id=new_id,
        name=body.name,
        role_description=body.role_description or "",
        welcome_message=body.welcome_message,
        tone=body.tone,
        creator_id=current_user.id,
        tenant_id=clawith_uuid,
        agent_type="native",
        external_id=str(new_id),
    )
    db.add(new_agent)
    await db.commit()
    await db.refresh(new_agent)
    return _agent_view(new_agent)


@router.get("/tenants/{tenant_id}/agents/{agent_id}")
async def get_agent(
    tenant_id: str,
    agent_id: str,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not tenant_id or len(tenant_id) > 100:
        raise HTTPException(status_code=400, detail="tenant_id must be 1-100 chars")
    aid = _validate_agent_id(agent_id)
    _, clawith_uuid = await _resolve_clawith_tenant_id(tenant_id)

    result = await db.execute(
        select(Agent).where(Agent.id == aid, Agent.tenant_id == clawith_uuid)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found under this tenant")
    return _agent_view(agent)


@router.patch("/tenants/{tenant_id}/agents/{agent_id}")
async def update_agent(
    tenant_id: str,
    agent_id: str,
    body: AgentUpdateBody,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not tenant_id or len(tenant_id) > 100:
        raise HTTPException(status_code=400, detail="tenant_id must be 1-100 chars")
    aid = _validate_agent_id(agent_id)
    _, clawith_uuid = await _resolve_clawith_tenant_id(tenant_id)

    if body.tone is not None and (body.tone < 0 or body.tone > 2):
        raise HTTPException(status_code=400, detail="tone must be 0..2 inclusive")

    result = await db.execute(
        select(Agent).where(Agent.id == aid, Agent.tenant_id == clawith_uuid)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found under this tenant")
    if agent.retired_at is not None:
        raise HTTPException(status_code=409, detail="Agent is retired; cannot edit")

    if body.name is not None:
        agent.name = body.name
    if body.role_description is not None:
        agent.role_description = body.role_description
    if body.welcome_message is not None:
        agent.welcome_message = body.welcome_message
    if body.tone is not None:
        agent.tone = body.tone

    await db.commit()
    await db.refresh(agent)
    return _agent_view(agent)


@router.post("/tenants/{tenant_id}/agents/{agent_id}/retire")
async def retire_agent(
    tenant_id: str,
    agent_id: str,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not tenant_id or len(tenant_id) > 100:
        raise HTTPException(status_code=400, detail="tenant_id must be 1-100 chars")
    aid = _validate_agent_id(agent_id)
    _, clawith_uuid = await _resolve_clawith_tenant_id(tenant_id)

    result = await db.execute(
        select(Agent).where(Agent.id == aid, Agent.tenant_id == clawith_uuid)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found under this tenant")
    if agent.retired_at is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Agent already retired at {agent.retired_at.isoformat()}",
        )

    agent.retired_at = _datetime.now(_timezone.utc)
    agent.retired_by = current_user.id
    await db.commit()
    await db.refresh(agent)
    return {
        "id": str(agent.id),
        "retired_at": agent.retired_at.isoformat(),
        "retired_by": str(agent.retired_by),
    }


# ── S5 R25 (2026-05-04): Paperclip ID resolution helper ──────────────────────


async def _resolve_paperclip_ids(
    tenant_id: str,
    agent_id: str,
    db: AsyncSession,
) -> tuple[str, str]:
    """Verify tenant + agent exist, return (paperclip_company_id, paperclip_agent_id).

    S5 R25 (2026-05-04): returns 409 with detail {error: 'agent_not_paperclip_managed',
    agent_id} if EITHER paperclip_company_id or paperclip_agent_id is NULL on the
    Clawith Agent row (legacy/orphan/smoke-residue). Mirrors S4 clawithTenantId-NULL
    409 precedent.

    Raises 400 on malformed agent_id; 404 on tenant or agent missing; 409 per R25.
    """
    bff_data = await _bff_get(f"/api/internal/cross-store/tenants/{tenant_id}")
    if bff_data.get("_status") == 404:
        raise HTTPException(status_code=404, detail="Tenant not found in tenant_registry")

    try:
        agent_uuid = UUID(agent_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="agent_id must be a valid UUID")

    result = await db.execute(select(Agent).where(Agent.id == agent_uuid))
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not agent.paperclip_company_id or not agent.paperclip_agent_id:
        raise HTTPException(
            status_code=409,
            detail={"error": "agent_not_paperclip_managed", "agent_id": agent_id},
        )

    return agent.paperclip_company_id, agent.paperclip_agent_id


# ── S5 R18-R25 (2026-05-04): SOUL editor + skill attachment ambassador ────────
# 4 endpoints under /api/admin/cross-store/tenants/{tid}/agents/{aid}:
#   GET  /soul    — fetch SOUL.md from Paperclip (source of truth)
#   PUT  /soul    — write Paperclip + R20a write-through Clawith Agent.soul
#   GET  /skills  — fetch catalog + agent's attached desiredSkills
#   PUT  /skills  — POST Paperclip skills/sync; no Clawith cache (ADR-0070)


import hashlib as _hashlib
import time as _time

from app.models.audit import AuditLog
from app.services.paperclip_client import (
    PaperclipUnreachable,
    _get_json as _pcp_get_json,
    fetch_paperclip_soul,
    update_paperclip_desired_skills,
    update_paperclip_soul,
)


# In-memory idempotency cache for SOUL writes (S5 R23 last-write-wins; 60s window).
# Module-level dict; lost on container restart (Wave-1 single-operator acceptable).
_SOUL_IDEMPOTENCY: dict[tuple[str, str], tuple[dict, float]] = {}
_SOUL_IDEMPOTENCY_TTL = 60.0


def _soul_idemp_get(agent_id: str, content_sha: str) -> dict | None:
    entry = _SOUL_IDEMPOTENCY.get((agent_id, content_sha))
    if entry is None:
        return None
    response, ts = entry
    if _time.time() - ts > _SOUL_IDEMPOTENCY_TTL:
        _SOUL_IDEMPOTENCY.pop((agent_id, content_sha), None)
        return None
    return response


def _soul_idemp_put(agent_id: str, content_sha: str, response: dict) -> None:
    _SOUL_IDEMPOTENCY[(agent_id, content_sha)] = (response, _time.time())
    now = _time.time()
    stale = [k for k, (_, t) in _SOUL_IDEMPOTENCY.items() if now - t > _SOUL_IDEMPOTENCY_TTL]
    for k in stale:
        _SOUL_IDEMPOTENCY.pop(k, None)


class SoulPutBody(BaseModel):
    content: str


@router.get("/tenants/{tenant_id}/agents/{agent_id}/soul")
async def get_agent_soul(
    tenant_id: str,
    agent_id: str,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    pcp_co, pcp_ag = await _resolve_paperclip_ids(tenant_id, agent_id, db)
    try:
        # runtimeConfig.soul is the dispatch-time hydrated view; null on agents
        # that haven't been dispatched yet. Fall back to bundle-file source of truth.
        content = await fetch_paperclip_soul(pcp_ag)
        if content is None:
            body = await _pcp_get_json(
                f"/api/agents/{pcp_ag}/instructions-bundle/file?path=SOUL.md"
            )
            content = body.get("content") or ""
    except PaperclipUnreachable as e:
        raise HTTPException(status_code=502, detail=f"Paperclip unreachable: {e}")
    return {
        "content": content,
        "source": "paperclip",
        "agent_id": agent_id,
        "tenant_id": tenant_id,
    }


@router.put("/tenants/{tenant_id}/agents/{agent_id}/soul")
async def put_agent_soul(
    tenant_id: str,
    agent_id: str,
    body: SoulPutBody,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not body.content or not body.content.strip():
        raise HTTPException(status_code=422, detail="content must not be empty")
    if len(body.content) > 50000:
        raise HTTPException(status_code=422, detail="content exceeds 50000 char limit")

    pcp_co, pcp_ag = await _resolve_paperclip_ids(tenant_id, agent_id, db)

    new_sha = _hashlib.sha256(body.content.encode("utf-8")).hexdigest()
    cached = _soul_idemp_get(agent_id, new_sha)
    if cached is not None:
        return cached

    agent_uuid = _uuidlib.UUID(agent_id)
    agent_r = await db.execute(select(Agent).where(Agent.id == agent_uuid))
    agent_row = agent_r.scalar_one_or_none()
    if agent_row is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    prev_sha = (
        _hashlib.sha256(agent_row.soul.encode("utf-8")).hexdigest()
        if agent_row.soul else None
    )

    try:
        await update_paperclip_soul(pcp_ag, body.content)
    except PaperclipUnreachable as e:
        raise HTTPException(status_code=502, detail=f"Paperclip write failed: {e}")

    # R20a write-through: update Clawith cache from request body, NOT from a fetch.
    # fetch_paperclip_soul reads runtimeConfig.soul (dispatch-time cache); the
    # bundle-file write doesn't repopulate that until next dispatch.
    agent_row.soul = body.content

    db.add(AuditLog(
        user_id=current_user.id,
        agent_id=agent_uuid,
        action="platform_admin.soul_edit",
        details={
            "tenant_id": tenant_id,
            "paperclip_agent_id": pcp_ag,
            "paperclip_company_id": pcp_co,
            "content_sha256_before": prev_sha,
            "content_sha256_after": new_sha,
            "content_length": len(body.content),
        },
    ))
    await db.commit()

    response = {
        "content": body.content,
        "content_sha256": new_sha,
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "cache_refreshed": True,
        "source": "paperclip",
    }
    _soul_idemp_put(agent_id, new_sha, response)
    return response


@router.get("/tenants/{tenant_id}/agents/{agent_id}/skills")
async def get_agent_skills(
    tenant_id: str,
    agent_id: str,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    pcp_co, pcp_ag = await _resolve_paperclip_ids(tenant_id, agent_id, db)
    try:
        catalog = await _pcp_get_json(f"/api/companies/{pcp_co}/skills")
        agent_skills = await _pcp_get_json(f"/api/agents/{pcp_ag}/skills")
    except PaperclipUnreachable as e:
        raise HTTPException(status_code=502, detail=f"Paperclip unreachable: {e}")
    attached = agent_skills.get("desiredSkills") or []
    available = [
        {
            "key": s.get("key"),
            "slug": s.get("slug"),
            "description": s.get("description") or "",
            "id": s.get("id"),
        }
        for s in catalog
        if not (s.get("key") or "").startswith("paperclipai/")
    ]
    return {
        "available": available,
        "attached": attached,
        "agent_id": agent_id,
        "tenant_id": tenant_id,
    }


class SkillsPutBody(BaseModel):
    desired: list[str]


@router.put("/tenants/{tenant_id}/agents/{agent_id}/skills")
async def put_agent_skills(
    tenant_id: str,
    agent_id: str,
    body: SkillsPutBody,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    pcp_co, pcp_ag = await _resolve_paperclip_ids(tenant_id, agent_id, db)
    agent_uuid = _uuidlib.UUID(agent_id)

    try:
        before_resp = await _pcp_get_json(f"/api/agents/{pcp_ag}/skills")
        before = before_resp.get("desiredSkills") or []
    except PaperclipUnreachable:
        before = []  # read failure shouldn't block write attempt

    try:
        pcp_response = await update_paperclip_desired_skills(pcp_ag, body.desired)
    except PaperclipUnreachable as e:
        msg = str(e)
        # Surface Paperclip's 422 (catalog-validation reject) verbatim
        if "HTTP 422" in msg:
            raise HTTPException(status_code=422, detail=msg[:300])
        raise HTTPException(status_code=502, detail=f"Paperclip skills/sync failed: {e}")

    after = pcp_response.get("desiredSkills") or body.desired

    db.add(AuditLog(
        user_id=current_user.id,
        agent_id=agent_uuid,
        action="platform_admin.skills_edit",
        details={
            "tenant_id": tenant_id,
            "paperclip_agent_id": pcp_ag,
            "paperclip_company_id": pcp_co,
            "before": before,
            "after": after,
        },
    ))
    await db.commit()

    return {
        "desired": after,
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "source": "paperclip",
    }


# ── S6 R28-revised + R34 + R35 + R36 (2026-05-04 night): policy + channels ──
# 6 endpoints under /api/admin/cross-store/tenants/{tid}/agents/{aid}:
#   GET    /policy             — assembled state with 9-key autonomy + badges
#   PATCH  /policy             — autonomy + escalation_keywords (table-direct DB write)
#   GET    /channels           — list ChannelConfig rows (table-direct, R34)
#   POST   /channels           — dispatch to per-channel handler (R34, R36 plaintext)
#   PATCH  /channels/{cid}     — conditional dispatch (R34, R36 plaintext)
#   DELETE /channels/{cid}     — table-direct (R34)
#
# R36: encryption deferred to W2-HW pre-launch gate. Phase 1 stores plaintext
# at rest. Per-channel handlers (slack.py / discord_bot.py / teams.py /
# whatsapp.py) UNTOUCHED — read paths unchanged.
#
# R35: 9-key autonomy whitelist with per-key enforcement_status badge.
# Map-only orphans (web_search, execute_code, send_feishu_message) NEVER exposed.

import json as _json
import re as _re

from fastapi import Request, Response
from app.models.channel_config import ChannelConfig


# 9-key autonomy whitelist (R35). Source: agents.autonomy_policy schema default
# in app/models/agent.py line 76. access_business_system splits into _read + _write
# (NOT 8 keys as v1.0 plan body claimed; corrected to 9 per probe surface).
_AUTONOMY_KEY_WHITELIST = (
    "write_workspace_files",
    "delete_files",
    "read_files",
    "send_external_message",
    "modify_soul",
    "access_business_system_read",
    "access_business_system_write",
    "create_calendar_event",
    "financial_operations",
)

# Wave-1 enforcement reality (R35): only these 2 keys actually gate via
# _TOOL_AUTONOMY_MAP in agent_tools.py:1309. The other 7 are scaffolded —
# saved + persisted but no tool currently checks them.
_AUTONOMY_ENFORCED_WAVE1 = frozenset(("write_workspace_files", "delete_files"))

_AUTONOMY_VALUES = ("L1", "L2", "L3")

# Hard limits per R31
_ESCAL_MAX_KEYWORDS = 50
_ESCAL_MAX_CHAR = 100


# In-memory idempotency cache for PATCH /policy (60s TTL, S5 R23 pattern).
# Module-level dict; lost on container restart (Wave-1 single-operator acceptable).
_POLICY_IDEMPOTENCY: dict[tuple[str, str], tuple[dict, float]] = {}
_POLICY_IDEMPOTENCY_TTL = 60.0


def _policy_idemp_get(agent_id: str, body_sha: str) -> dict | None:
    entry = _POLICY_IDEMPOTENCY.get((agent_id, body_sha))
    if entry is None:
        return None
    response, ts = entry
    if _time.time() - ts > _POLICY_IDEMPOTENCY_TTL:
        _POLICY_IDEMPOTENCY.pop((agent_id, body_sha), None)
        return None
    return response


def _policy_idemp_put(agent_id: str, body_sha: str, response: dict) -> None:
    _POLICY_IDEMPOTENCY[(agent_id, body_sha)] = (response, _time.time())
    now = _time.time()
    stale = [k for k, (_, t) in _POLICY_IDEMPOTENCY.items() if now - t > _POLICY_IDEMPOTENCY_TTL]
    for k in stale:
        _POLICY_IDEMPOTENCY.pop(k, None)


# ── Channel dispatch table (R34) ─────────────────────────────────────────
# Maps channel_type → (per-channel POST URL suffix, body adapter).
# Per-channel handlers live at /api/agents/{aid}/<type>-channel.
# is_agent_creator (line 57 of permissions.py) treats platform_admin as creator,
# so admin's Bearer pass-through is accepted by per-channel handlers without
# touching their auth. R36 forbids touching slack.py / discord_bot.py /
# teams.py / whatsapp.py read paths — dispatch via HTTP self-call is the
# zero-touch pattern.

_HANDLER_HOST = "http://127.0.0.1:8000"  # uvicorn binds 8000 inside container
                                          # (host port 8800 maps to 8000)


def _shape_slack_body(body: "ChannelMutateBody") -> dict:
    """Slack handler expects {bot_token, signing_secret}.
    Admin generic body uses app_secret + extra_config.signing_secret."""
    return {
        "bot_token": (body.app_secret or "").strip(),
        "signing_secret": ((body.extra_config or {}).get("signing_secret") or "").strip(),
    }


def _shape_discord_body(body: "ChannelMutateBody") -> dict:
    """Discord handler expects {connection_mode, bot_token, application_id, public_key}.
    Admin app_id → application_id; extra_config carries connection_mode + public_key."""
    extra = body.extra_config or {}
    return {
        "connection_mode": (extra.get("connection_mode") or "webhook").strip(),
        "bot_token": (body.app_secret or "").strip(),
        "application_id": (body.app_id or "").strip(),
        "public_key": (extra.get("public_key") or "").strip(),
    }


def _shape_teams_body(body: "ChannelMutateBody") -> dict:
    """Teams handler expects {app_id, app_secret, tenant_id?, use_managed_identity?}."""
    extra = body.extra_config or {}
    return {
        "app_id": (body.app_id or "").strip(),
        "app_secret": (body.app_secret or "").strip(),
        "tenant_id": (extra.get("tenant_id") or "").strip(),
        "use_managed_identity": bool(extra.get("use_managed_identity") or False),
    }


def _shape_whatsapp_body(body: "ChannelMutateBody") -> dict:
    """WA handler expects {phone_number_id, waba_id, access_token, verify_token, app_secret}.
    All live in extra_config; admin-layer body.app_secret seeds extra_config.app_secret too."""
    extra = body.extra_config or {}
    return {
        "phone_number_id": (extra.get("phone_number_id") or "").strip(),
        "waba_id": (extra.get("waba_id") or "").strip(),
        "access_token": (extra.get("access_token") or "").strip(),
        "verify_token": (extra.get("verify_token") or "").strip(),
        "app_secret": (extra.get("app_secret") or body.app_secret or "").strip(),
    }


# Order of registration matches the 4 channel_type enum values.
# Adding a new channel_type requires: (a) new per-channel handler in
# app/api/<type>.py, (b) new entry here. Until both exist, POST 422s.
_DISPATCH_TABLE: dict[str, tuple[str, callable]] = {
    "slack":           ("slack-channel",     _shape_slack_body),
    "discord":         ("discord-channel",   _shape_discord_body),
    "microsoft_teams": ("teams-channel",     _shape_teams_body),
    "whatsapp":        ("whatsapp-channel",  _shape_whatsapp_body),
}


async def _dispatch_to_channel_handler(
    channel_type: str,
    agent_id: str,
    handler_body: dict,
    auth_header: str,
) -> tuple[int, dict]:
    """HTTP self-call to per-channel POST handler. Forwards Bearer; returns
    (status, body). Caller has already validated channel_type ∈ dispatch table."""
    suffix, _ = _DISPATCH_TABLE[channel_type]
    url = f"{_HANDLER_HOST}/api/agents/{agent_id}/{suffix}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, json=handler_body, headers={"authorization": auth_header})
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"detail": (r.text or "")[:300]}


def _validate_channel_id(channel_id: str) -> _uuidlib.UUID:
    if not channel_id or len(channel_id) > 100:
        raise HTTPException(status_code=400, detail="channel_id must be 1-100 chars")
    try:
        return _uuidlib.UUID(channel_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="channel_id must be a UUID")


# ── NULL agent.tenant_id → 409 helper (R35 plan finding) ──────────────────
async def _resolve_agent_for_policy(
    tenant_id: str, agent_id: str, db: AsyncSession,
) -> tuple[Agent, UUID, dict]:
    """Verify tenant_registry → clawith UUID → agent row. Returns
    (agent, clawith_uuid, bff_row). Raises:
      - 400 on malformed agent_id or tenant_id
      - 404 on tenant not found OR agent not found OR agent not under tenant
      - 409 on agent.tenant_id IS NULL (legacy/orphan agent per pre-flight finding)
      - 409 on tenant has no clawithTenantId (S4 carry)
    """
    if not tenant_id or len(tenant_id) > 100:
        raise HTTPException(status_code=400, detail="tenant_id must be 1-100 chars")
    aid = _validate_agent_id(agent_id)
    bff_row, clawith_uuid = await _resolve_clawith_tenant_id(tenant_id)

    result = await db.execute(select(Agent).where(Agent.id == aid))
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.tenant_id is None:
        raise HTTPException(
            status_code=409,
            detail={"error": "agent_not_clawith_managed", "agent_id": agent_id},
        )
    if agent.tenant_id != clawith_uuid:
        raise HTTPException(status_code=404, detail="Agent not found under this tenant")
    return agent, clawith_uuid, bff_row


# ── SOUL frontmatter parser (lightweight, no PyYAML dep) ──────────────────
# Frontmatter delimiter: leading "---\n...\n---" block. Extracts hours_*
# fields if present. Returns None when no frontmatter or no hours fields.
_SOUL_FRONTMATTER_RE = _re.compile(r"^---\s*\n(.*?)\n---", _re.DOTALL)


def _parse_soul_business_hours(content: str | None) -> dict | None:
    if not content:
        return None
    m = _SOUL_FRONTMATTER_RE.search(content)
    if not m:
        return None
    fm = m.group(1)
    out: dict[str, str] = {}
    for line in fm.split("\n"):
        line = line.strip()
        if not line or ":" not in line or line.startswith("#"):
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k in ("hours_weekday", "hours_saturday", "hours_sunday"):
            out[k] = v
    if not out:
        return None
    return {"source": "soul.md", **out}


# ── Channel binding view from BFF tenant_registry row ────────────────────
def _channel_binding_view(bff_row: dict) -> dict:
    """Extract WA + voice binding fields from BFF tenant_registry row.
    voice section omitted (None) when neither magnusDidId nor didNumber set."""
    wa = {
        k: bff_row.get(k)
        for k in (
            "whatsappStatus", "displayPhone", "waPhoneNumberId", "wabaId",
            "ownerPhone", "didNumber", "magnusDidId",
            "waProvisioningJobId", "waProvisioningState", "whatsappActivatedAt",
        )
    }
    voice_keys = {k: bff_row.get(k) for k in ("magnusDidId", "didNumber") if bff_row.get(k)}
    voice = voice_keys if voice_keys else None
    return {"whatsapp": wa, "voice": voice}


# ── Autonomy view: filter to whitelist + add per-key enforcement badge ────
def _autonomy_view(autonomy_policy: dict | None) -> dict:
    """R35 envelope: 9-key list filtered + per-key enforcement_status badge.
    Map-only orphans NEVER exposed. Default value = 'L2' per autonomy_service:51."""
    policy = autonomy_policy or {}
    keys = []
    for k in _AUTONOMY_KEY_WHITELIST:
        v = policy.get(k, "L2")
        if v not in _AUTONOMY_VALUES:
            v = "L2"
        keys.append({
            "key": k,
            "value": v,
            "enforcement_status": "enforced" if k in _AUTONOMY_ENFORCED_WAVE1 else "scaffolded",
        })
    return {"keys": keys}


# ── /policy endpoints ─────────────────────────────────────────────────────


@router.get("/tenants/{tenant_id}/agents/{agent_id}/policy")
async def get_agent_policy(
    tenant_id: str,
    agent_id: str,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    agent, _, bff_row = await _resolve_agent_for_policy(tenant_id, agent_id, db)

    business_hours = None
    if agent.paperclip_agent_id:
        try:
            content = await fetch_paperclip_soul(agent.paperclip_agent_id)
            if content is None:
                # runtimeConfig.soul not hydrated; fall back to bundle file (S5 pattern)
                try:
                    body_resp = await _pcp_get_json(
                        f"/api/agents/{agent.paperclip_agent_id}/instructions-bundle/file?path=SOUL.md"
                    )
                    content = body_resp.get("content") or ""
                except PaperclipUnreachable:
                    content = None
            if content:
                business_hours = _parse_soul_business_hours(content)
        except PaperclipUnreachable:
            business_hours = None

    return {
        "autonomy_policy": _autonomy_view(agent.autonomy_policy),
        "escalation_keywords": list(agent.escalation_keywords or []),
        "channel_binding": _channel_binding_view(bff_row),
        "business_hours_readonly": business_hours,
        "agent_id": agent_id,
        "tenant_id": tenant_id,
    }


class PolicyPatchBody(BaseModel):
    autonomy_policy: dict[str, str] | None = None
    escalation_keywords: list[str] | None = None


@router.patch("/tenants/{tenant_id}/agents/{agent_id}/policy")
async def patch_agent_policy(
    tenant_id: str,
    agent_id: str,
    body: PolicyPatchBody,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if body.autonomy_policy is None and body.escalation_keywords is None:
        raise HTTPException(
            status_code=422,
            detail="At least one of autonomy_policy or escalation_keywords is required",
        )

    agent, _, _ = await _resolve_agent_for_policy(tenant_id, agent_id, db)

    if body.autonomy_policy is not None:
        for k, v in body.autonomy_policy.items():
            if k not in _AUTONOMY_KEY_WHITELIST:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown autonomy key '{k}'. Allowed: {list(_AUTONOMY_KEY_WHITELIST)}",
                )
            if v not in _AUTONOMY_VALUES:
                raise HTTPException(
                    status_code=422,
                    detail=f"autonomy value for '{k}' must be one of {list(_AUTONOMY_VALUES)}; got '{v}'",
                )

    normalized_keywords = None
    if body.escalation_keywords is not None:
        if len(body.escalation_keywords) > _ESCAL_MAX_KEYWORDS:
            raise HTTPException(
                status_code=422,
                detail=f"escalation_keywords exceeds max {_ESCAL_MAX_KEYWORDS}",
            )
        seen: set[str] = set()
        out: list[str] = []
        for kw in body.escalation_keywords:
            if not isinstance(kw, str):
                raise HTTPException(
                    status_code=422,
                    detail="escalation_keywords entries must be strings",
                )
            stripped = kw.strip()
            if not stripped:
                raise HTTPException(
                    status_code=422,
                    detail="escalation_keywords entries must be non-empty after strip",
                )
            if len(stripped) > _ESCAL_MAX_CHAR:
                raise HTTPException(
                    status_code=422,
                    detail=f"escalation_keyword exceeds {_ESCAL_MAX_CHAR} chars (got {len(stripped)})",
                )
            lower = stripped.lower()
            if lower in seen:
                continue  # dedup case-insensitively, preserve first-seen casing
            seen.add(lower)
            out.append(stripped)
        normalized_keywords = out

    body_payload = {
        "autonomy_policy": body.autonomy_policy,
        "escalation_keywords": normalized_keywords,
    }
    body_sha = _hashlib.sha256(
        _json.dumps(body_payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    cached = _policy_idemp_get(agent_id, body_sha)
    if cached is not None:
        return cached

    before_autonomy = dict(agent.autonomy_policy or {})
    before_keywords = list(agent.escalation_keywords or [])

    if body.autonomy_policy is not None:
        merged = dict(before_autonomy)
        merged.update(body.autonomy_policy)
        agent.autonomy_policy = merged
    if normalized_keywords is not None:
        agent.escalation_keywords = normalized_keywords

    db.add(AuditLog(
        user_id=current_user.id,
        agent_id=agent.id,
        action="platform_admin.policy_edit",
        details={
            "tenant_id": tenant_id,
            "autonomy_policy_before": before_autonomy,
            "autonomy_policy_after": dict(agent.autonomy_policy or {}),
            "escalation_keywords_before": before_keywords,
            "escalation_keywords_after": list(agent.escalation_keywords or []),
        },
    ))
    await db.commit()
    await db.refresh(agent)

    response = {
        "autonomy_policy": _autonomy_view(agent.autonomy_policy),
        "escalation_keywords": list(agent.escalation_keywords or []),
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "updated_at": agent.updated_at.isoformat() if agent.updated_at else None,
    }
    _policy_idemp_put(agent_id, body_sha, response)
    return response


# ── /channels endpoints ──────────────────────────────────────────────────


def _channel_view(c: ChannelConfig) -> dict:
    """ChannelConfig → safe response dict. NEVER includes app_secret /
    encrypt_key / verification_token / extra_config (R36 stores plaintext
    but security policy still bars surfacing creds to API consumers).
    display_name lives in extra_config['display_name'] (no schema column)."""
    extra = c.extra_config or {}
    return {
        "channel_id": str(c.id),
        "channel_type": c.channel_type,
        "is_configured": bool(c.is_configured),
        "is_connected": bool(c.is_connected),
        "display_name": extra.get("display_name"),
        "app_id": c.app_id,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


@router.get("/tenants/{tenant_id}/agents/{agent_id}/channels")
async def list_agent_channels(
    tenant_id: str,
    agent_id: str,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    agent, _, _ = await _resolve_agent_for_policy(tenant_id, agent_id, db)
    result = await db.execute(
        select(ChannelConfig)
        .where(ChannelConfig.agent_id == agent.id)
        .order_by(ChannelConfig.created_at.desc())
    )
    rows = result.scalars().all()
    return {
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "channels": [_channel_view(c) for c in rows],
        "supported_channel_types": list(_DISPATCH_TABLE.keys()),
    }


class ChannelMutateBody(BaseModel):
    channel_type: str | None = None         # required for POST; ignored for PATCH
    display_name: str | None = None
    app_id: str | None = None
    app_secret: str | None = None
    extra_config: dict | None = None
    is_connected: bool | None = None


@router.post("/tenants/{tenant_id}/agents/{agent_id}/channels", status_code=201)
async def create_agent_channel(
    tenant_id: str,
    agent_id: str,
    body: ChannelMutateBody,
    request: Request,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not body.channel_type:
        raise HTTPException(status_code=422, detail="channel_type is required")
    if body.channel_type not in _DISPATCH_TABLE:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_channel_type",
                "channel_type": body.channel_type,
                "supported": list(_DISPATCH_TABLE.keys()),
                "message": (
                    f"channel_type '{body.channel_type}' not yet supported in this Wave-1 build "
                    f"(no per-channel handler registered)"
                ),
            },
        )
    if body.display_name is not None and len(body.display_name) > 100:
        raise HTTPException(status_code=422, detail="display_name max 100 chars")

    agent, _, _ = await _resolve_agent_for_policy(tenant_id, agent_id, db)

    auth_header = request.headers.get("authorization", "")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    _, shaper = _DISPATCH_TABLE[body.channel_type]
    handler_body = shaper(body)
    status_code, handler_resp = await _dispatch_to_channel_handler(
        body.channel_type, agent_id, handler_body, auth_header,
    )
    if status_code not in (200, 201):
        raise HTTPException(
            status_code=status_code if status_code in (400, 401, 403, 404, 422) else 502,
            detail={
                "error": "per_channel_handler_rejected",
                "channel_type": body.channel_type,
                "handler_status": status_code,
                "handler_detail": handler_resp.get("detail") if isinstance(handler_resp, dict) else None,
            },
        )

    # Re-fetch row to get the channel_id assigned by the per-channel handler,
    # then patch display_name into extra_config (handlers don't accept display_name).
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent.id,
            ChannelConfig.channel_type == body.channel_type,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "handler_succeeded_but_no_row",
                "channel_type": body.channel_type,
                "handler_status": status_code,
            },
        )
    if body.display_name is not None:
        merged_extra = dict(row.extra_config or {})
        merged_extra["display_name"] = body.display_name
        row.extra_config = merged_extra

    db.add(AuditLog(
        user_id=current_user.id,
        agent_id=agent.id,
        action="platform_admin.channel_create",
        details={
            "tenant_id": tenant_id,
            "channel_id": str(row.id),
            "channel_type": body.channel_type,
            "app_id": body.app_id,                    # identifier, not credential
            "display_name": body.display_name,        # operator label, not credential
            # NEVER log app_secret, extra_config (creds), encrypt_key, etc.
        },
    ))
    await db.commit()
    await db.refresh(row)
    return _channel_view(row)


@router.patch("/tenants/{tenant_id}/agents/{agent_id}/channels/{channel_id}")
async def patch_agent_channel(
    tenant_id: str,
    agent_id: str,
    channel_id: str,
    body: ChannelMutateBody,
    request: Request,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    cid = _validate_channel_id(channel_id)
    if body.display_name is not None and len(body.display_name) > 100:
        raise HTTPException(status_code=422, detail="display_name max 100 chars")

    agent, _, _ = await _resolve_agent_for_policy(tenant_id, agent_id, db)

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.id == cid,
            ChannelConfig.agent_id == agent.id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Channel config not found under this agent")

    has_credential_change = body.app_secret is not None or body.extra_config is not None
    has_metadata_change = body.display_name is not None or body.is_connected is not None
    if not has_credential_change and not has_metadata_change:
        raise HTTPException(
            status_code=422,
            detail="At least one of display_name, is_connected, app_secret, or extra_config required",
        )

    fields_changed: list[str] = []

    if has_credential_change:
        # Dispatch to per-channel handler (re-validates with channel API + writes plaintext).
        # Use the row's existing channel_type (operator can't change channel_type via PATCH).
        if row.channel_type not in _DISPATCH_TABLE:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "unsupported_channel_type_for_patch",
                    "channel_type": row.channel_type,
                },
            )
        auth_header = request.headers.get("authorization", "")
        if not auth_header:
            raise HTTPException(status_code=401, detail="Missing Authorization header")
        # PATCH-shaped body fills in current values for fields not provided so the
        # per-channel handler's required-field validation passes.
        existing_extra = dict(row.extra_config or {})
        merged_for_dispatch = ChannelMutateBody(
            channel_type=row.channel_type,
            app_id=body.app_id if body.app_id is not None else row.app_id,
            app_secret=body.app_secret if body.app_secret is not None else row.app_secret,
            extra_config={**existing_extra, **(body.extra_config or {})},
        )
        _, shaper = _DISPATCH_TABLE[row.channel_type]
        handler_body = shaper(merged_for_dispatch)
        status_code, handler_resp = await _dispatch_to_channel_handler(
            row.channel_type, agent_id, handler_body, auth_header,
        )
        if status_code not in (200, 201):
            raise HTTPException(
                status_code=status_code if status_code in (400, 401, 403, 404, 422) else 502,
                detail={
                    "error": "per_channel_handler_rejected",
                    "channel_type": row.channel_type,
                    "handler_status": status_code,
                    "handler_detail": handler_resp.get("detail") if isinstance(handler_resp, dict) else None,
                },
            )
        if body.app_secret is not None:
            fields_changed.append("app_secret")
        if body.extra_config is not None:
            fields_changed.append("extra_config")
        # Re-fetch the row after handler write
        await db.refresh(row)

    if has_metadata_change:
        if body.display_name is not None:
            merged_extra = dict(row.extra_config or {})
            merged_extra["display_name"] = body.display_name
            row.extra_config = merged_extra
            fields_changed.append("display_name")
        if body.is_connected is not None:
            row.is_connected = body.is_connected
            fields_changed.append("is_connected")

    db.add(AuditLog(
        user_id=current_user.id,
        agent_id=agent.id,
        action="platform_admin.channel_update",
        details={
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "channel_type": row.channel_type,
            "fields_changed": fields_changed,    # KEY NAMES only, never values
        },
    ))
    await db.commit()
    await db.refresh(row)
    return _channel_view(row)


@router.delete("/tenants/{tenant_id}/agents/{agent_id}/channels/{channel_id}", status_code=204)
async def delete_agent_channel(
    tenant_id: str,
    agent_id: str,
    channel_id: str,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    cid = _validate_channel_id(channel_id)
    agent, _, _ = await _resolve_agent_for_policy(tenant_id, agent_id, db)

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.id == cid,
            ChannelConfig.agent_id == agent.id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Channel config not found under this agent")

    deleted_channel_type = row.channel_type
    await db.delete(row)
    db.add(AuditLog(
        user_id=current_user.id,
        agent_id=agent.id,
        action="platform_admin.channel_delete",
        details={
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "channel_type": deleted_channel_type,
            "wave_2_followup": "per-channel deauth hook not yet wired; webhook subscription on channel side may persist",
        },
    ))
    await db.commit()
    return Response(status_code=204)


# ── L4 S7 R42 (2026-05-05): cross-store tenant retire orchestrator ───
# DELETE /api/admin/cross-store/tenants/{tenant_id}
#
# Operator-driven retire across 3 stores. Sequence (R42):
#   1. Paperclip POST /api/companies/{cid}/archive  (memory: archive not DELETE
#      for company lifecycle)
#   2. Clawith soft-retire: tenants.retired_at = NOW(); retired_by = operator
#   3. BFF NULL clawithTenantId via /api/internal/cross-store/tenants/{tid}/unlink-clawith
#   4. Audit row platform_admin.tenant_retire (only on full success)
#
# Failure semantics: 409 with envelope
#   { error: "partial_state", succeeded: [...], failed: <step>, details: <reason> }
# Caller can retry — each step is idempotent so re-runs no-op the
# already-completed work and re-attempt the failing step.
#
# Idempotency on full retry of already-fully-retired tenant:
#   Paperclip archive: assumed idempotent server-side (POST /:id/archive on
#     archived row → 200 ok or 409 already-archived; we treat 409 as success
#     since the desired end state holds).
#   Clawith retire: noop branch (existing retired_at preserved).
#   BFF unlink-clawith: noop branch (already-NULL handled in route).
#   Audit row: written on every successful run (intentional — preserves
#     per-retry timestamp trail).

from app.services.paperclip_client import _request_json as _pcp_request_json


@router.delete("/tenants/{tenant_id}", status_code=200)
async def retire_cross_store_tenant(
    tenant_id: str,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cross-store soft-retire orchestrator (R42 sequence)."""
    if not tenant_id or len(tenant_id) > 100:
        raise HTTPException(status_code=400, detail="tenant_id must be 1-100 chars")

    bff_row, clawith_uuid = await _resolve_clawith_tenant_id(tenant_id)
    paperclip_company_id = bff_row.get("paperclipCompanyId")

    succeeded: list[str] = []

    # ── Step 1: Paperclip archive (memory: POST /:id/archive, NOT DELETE) ──
    if paperclip_company_id:
        try:
            await _pcp_request_json("POST", f"/api/companies/{paperclip_company_id}/archive")
            succeeded.append("paperclip_archive")
        except PaperclipUnreachable as e:
            msg = str(e)
            # Treat "already archived" (Paperclip returns 409 in this shape) as success
            if "HTTP 409" in msg or "already archived" in msg.lower():
                succeeded.append("paperclip_archive_already")
            else:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "partial_state",
                        "succeeded": succeeded,
                        "failed": "paperclip_archive",
                        "details": msg[:300],
                    },
                )
    else:
        # No Paperclip linkage on this BFF row — explicit skip marker for audit.
        succeeded.append("paperclip_skipped_no_linkage")

    # ── Step 2: Clawith soft-retire (table-direct since admin_crossstore is Clawith) ──
    try:
        result = await db.execute(select(Tenant).where(Tenant.id == clawith_uuid))
        tenant = result.scalar_one_or_none()
        if tenant is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "partial_state",
                    "succeeded": succeeded,
                    "failed": "clawith_retire",
                    "details": f"Clawith tenant {clawith_uuid} not found",
                },
            )
        if getattr(tenant, "retired_at", None) is None:
            tenant.retired_at = _datetime.now(_timezone.utc)
            tenant.retired_by = current_user.id
            await db.commit()
            await db.refresh(tenant)
            succeeded.append("clawith_retire")
        else:
            succeeded.append("clawith_retire_already")
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "error": "partial_state",
                "succeeded": succeeded,
                "failed": "clawith_retire",
                "details": f"{type(e).__name__}: {str(e)[:300]}",
            },
        )

    # ── Step 3: BFF NULL clawithTenantId via internal route ──────────────
    try:
        bff_status, bff_data = await _bff_post(
            f"/api/internal/cross-store/tenants/{tenant_id}/unlink-clawith",
            {},
        )
        if bff_status >= 400:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "partial_state",
                    "succeeded": succeeded,
                    "failed": "bff_unlink_clawith",
                    "details": (bff_data.get("error") if isinstance(bff_data, dict) else str(bff_data))[:300],
                },
            )
        succeeded.append(
            "bff_unlink_clawith_already" if bff_data.get("noop") else "bff_unlink_clawith"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "partial_state",
                "succeeded": succeeded,
                "failed": "bff_unlink_clawith",
                "details": f"{type(e).__name__}: {str(e)[:300]}",
            },
        )

    # ── Step 4: Audit row (only on full success) ──────────────────────────
    db.add(AuditLog(
        user_id=current_user.id,
        action="platform_admin.tenant_retire",
        details={
            "bff_tenant_id": tenant_id,
            "paperclip_company_id": paperclip_company_id,
            "clawith_tenant_id": str(clawith_uuid),
            "succeeded_steps": succeeded,
        },
    ))
    await db.commit()

    return {
        "bff_tenant_id": tenant_id,
        "paperclip_company_id": paperclip_company_id,
        "clawith_tenant_id": str(clawith_uuid),
        "retired": True,
        "succeeded_steps": succeeded,
    }
