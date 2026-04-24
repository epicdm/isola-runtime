"""Phase E.6c: /api/internal/* service-to-service endpoints.

All routes here are gated by X-Internal-Secret (require_internal_secret).
Purpose: apps/isola server-side proxy calls into isola-runtime without
round-tripping a user JWT.

Current endpoints:
- POST /api/internal/tenants/ensure   create-or-return a runtime tenant
                                       keyed on external_id (the apps/isola
                                       tenant UUID). Idempotent.
- GET  /api/internal/tenants/{id}      read tenant (incl. runtime_mode)
- PUT  /api/internal/tenants/{id}      update tenant (runtime_mode etc)
"""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.internal_auth import require_internal_secret
from app.database import get_db
from app.models.tenant import Tenant

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_internal_secret)],
)


# ─── Schemas ─────────────────────────────────────────────────────────


class InternalTenantEnsureRequest(BaseModel):
    external_id: str = Field(
        ...,
        description=(
            "Stable ID from the caller (apps/isola tenants.id). Used as the "
            "idempotency key — repeated calls with the same external_id "
            "return the same runtime tenant."
        ),
        min_length=1,
        max_length=200,
    )
    name: str = Field(..., min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=50)


class InternalTenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    runtime_mode: str = "hosted"
    created: bool = False  # true if this call freshly created the row

    model_config = {"from_attributes": True}


class InternalTenantUpdate(BaseModel):
    runtime_mode: Literal["hosted", "edge"] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)


# ─── Helpers ────────────────────────────────────────────────────────


def _slugify_external(external_id: str) -> str:
    """Cheap slug derived from external_id — keeps the unique constraint happy.

    External IDs from apps/isola are already stable opaque strings; this just
    trims them into a 50-char slug.
    """
    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in external_id)
    cleaned = "-".join(filter(None, cleaned.split("-")))
    return (cleaned or "tenant")[:50]


# ─── Routes ─────────────────────────────────────────────────────────


@router.post("/tenants/ensure", response_model=InternalTenantOut, status_code=status.HTTP_200_OK)
async def ensure_tenant(
    data: InternalTenantEnsureRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create-or-return a runtime tenant keyed on external_id.

    The external_id is stored in tenants.slug as `iso-{external_id_slug}`
    so lookups are idempotent without a new column.
    """
    slug = data.slug or f"iso-{_slugify_external(data.external_id)}"
    # Clamp to 50 to satisfy the Tenant.slug column length.
    slug = slug[:50]

    result = await db.execute(select(Tenant).where(Tenant.slug == slug))
    tenant = result.scalar_one_or_none()
    created = False

    if tenant is None:
        tenant = Tenant(name=data.name, slug=slug, im_provider="web_only")
        db.add(tenant)
        try:
            await db.flush()
        except Exception as exc:  # pragma: no cover — race against unique index
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Tenant slug collision: {slug!r} ({exc!r})",
            ) from exc
        created = True

    await db.commit()
    await db.refresh(tenant)

    out = InternalTenantOut.model_validate(tenant).model_dump()
    out["created"] = created
    return out


@router.get("/tenants/{tenant_id}", response_model=InternalTenantOut)
async def internal_get_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Read tenant fields including runtime_mode. Service-secret gated."""
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    out = InternalTenantOut.model_validate(tenant).model_dump()
    out["created"] = False
    return out


@router.put("/tenants/{tenant_id}", response_model=InternalTenantOut)
async def internal_update_tenant(
    tenant_id: uuid.UUID,
    data: InternalTenantUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update runtime_mode (or name) on a tenant. Service-secret gated."""
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    patch = data.model_dump(exclude_unset=True)
    for k, v in patch.items():
        setattr(tenant, k, v)
    await db.commit()
    await db.refresh(tenant)

    out = InternalTenantOut.model_validate(tenant).model_dump()
    out["created"] = False
    return out

# ─── User auth bridge (Phase E.2.1) ─────────────────────────────────


class InternalUserEnsureRequest(BaseModel):
    tenant_runtime_id: uuid.UUID = Field(
        ...,
        description="Runtime tenant id returned by /api/internal/tenants/ensure.",
    )
    external_user_id: str = Field(
        ...,
        description="Stable id from caller (apps/isola users.id). Used as idempotency key.",
        min_length=1,
        max_length=200,
    )
    email: str = Field(..., min_length=3, max_length=255)
    display_name: str = Field(..., min_length=1, max_length=100)


class InternalUserEnsureResponse(BaseModel):
    user_id: uuid.UUID
    access_token: str
    expires_in_minutes: int
    created: bool = False


@router.post(
    "/users/ensure-and-mint",
    response_model=InternalUserEnsureResponse,
)
async def ensure_user_and_mint(
    data: InternalUserEnsureRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create-or-return a runtime User tied to the given tenant, mint a JWT.

    Idempotency:
      - Identity looked up by email; created if not found.
      - User looked up by (tenant_id, identity_id); created if not found.
      - Repeat calls with the same (email, tenant_runtime_id) return the same user_id.
    """
    from app.core.security import create_access_token
    from app.models.tenant import Tenant
    from app.models.user import Identity, User

    # 1. Tenant must exist.
    t_r = await db.execute(select(Tenant).where(Tenant.id == data.tenant_runtime_id))
    tenant = t_r.scalar_one_or_none()
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Runtime tenant {data.tenant_runtime_id} not found",
        )

    created = False

    # 2. Identity by email (global across tenants).
    email = data.email.lower().strip()
    i_r = await db.execute(select(Identity).where(Identity.email == email))
    identity = i_r.scalar_one_or_none()
    if identity is None:
        identity = Identity(
            email=email,
            is_active=True,
            email_verified=False,
        )
        db.add(identity)
        await db.flush()
        created = True

    # 3. User by (tenant_id, identity_id).
    u_r = await db.execute(
        select(User).where(
            User.tenant_id == tenant.id,
            User.identity_id == identity.id,
        )
    )
    user = u_r.scalar_one_or_none()
    if user is None:
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name=data.display_name[:100],
            role="org_admin",  # apps/isola tenants are single-operator at MVP
            registration_source="apps_isola_bridge",
            is_active=True,
            quota_message_limit=tenant.default_message_limit,
            quota_message_period=tenant.default_message_period,
            quota_max_agents=tenant.default_max_agents,
            quota_agent_ttl_hours=tenant.default_agent_ttl_hours,
        )
        db.add(user)
        await db.flush()
        created = True

    await db.commit()

    # 4. Mint JWT (uses existing JWT_SECRET_KEY + JWT_ALGORITHM from config).
    from app.config import get_settings
    settings = get_settings()
    token = create_access_token(str(user.id), user.role)
    return InternalUserEnsureResponse(
        user_id=user.id,
        access_token=token,
        expires_in_minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
        created=created,
    )

