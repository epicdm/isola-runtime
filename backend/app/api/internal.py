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
    timezone: str | None = Field(
        default=None,
        max_length=50,
        description=(
            "IANA timezone (e.g. 'America/Dominica'). Set on creation; "
            "ignored on subsequent ensure calls so tenants can update via "
            "the dedicated PUT /tenants/{id} path. Defaults to 'UTC'."
        ),
    )
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
        tenant = Tenant(
            name=data.name,
            slug=slug,
            im_provider="web_only",
            timezone=(data.timezone or "UTC")[:50],
        )
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
    role: str = "org_admin"  # platform_admin | org_admin | agent_admin | member


class InternalUserOut(BaseModel):
    """Lightweight read-only view used by apps/isola dashboard layout to
    surface the runtime role (e.g. show 'Platform Admin' menu item)."""
    user_id: uuid.UUID
    tenant_id: uuid.UUID | None
    role: str
    display_name: str
    email: str


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
        role=user.role,
    )


@router.get(
    "/users/by-email/{email}",
    response_model=InternalUserOut,
)
async def internal_get_user_by_email(
    email: str,
    db: AsyncSession = Depends(get_db),
):
    """Lookup runtime user by email — used by apps/isola dashboard layout
    to read the user's runtime role for the admin-menu gate (#98).
    Email is the stable join key since apps/isola users.id maps to runtime
    Identity.email (set during ensure-and-mint)."""
    from app.models.user import Identity, User

    email_norm = email.lower().strip()
    i_r = await db.execute(select(Identity).where(Identity.email == email_norm))
    identity = i_r.scalar_one_or_none()
    if not identity:
        raise HTTPException(status_code=404, detail="User not found")
    u_r = await db.execute(
        select(User).where(User.identity_id == identity.id).limit(1)
    )
    user = u_r.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return InternalUserOut(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        display_name=user.display_name or "",
        email=email_norm,
    )


# ─── Agent-sync (Phase F.1.a-2) ─────────────────────────────────────


class InternalAgentEnsureRequest(BaseModel):
    external_id: str = Field(
        ...,
        description="Stable id from caller (apps/isola agents.id). Used as idempotency key AND as the runtime agent UUID when it parses as one.",
        min_length=1,
        max_length=200,
    )
    tenant_runtime_id: uuid.UUID = Field(
        ...,
        description="Runtime tenant id returned by /api/internal/tenants/ensure.",
    )
    creator_id: uuid.UUID = Field(
        ...,
        description="Runtime user id returned by /api/internal/users/ensure-and-mint.",
    )
    name: str = Field(..., min_length=1, max_length=100)
    role_description: str = Field(default="", max_length=500)
    template_hint: str | None = Field(
        default=None,
        description=(
            "Dash-separated role-vertical, e.g. 'rex-restaurant'. "
            "Resolved to agent_templates row. Required for F.1 path; "
            "optional for flexibility."
        ),
        max_length=100,
    )
    tone: int | None = Field(default=None, ge=0, le=2)
    welcome_message: str | None = None
    escalation_keywords: list[str] | None = None
    # Owner's WhatsApp number (E.164 digits, with or without leading '+').
    # Required for the Tier 1.5a live owner-ask loop (#109) — when missing,
    # Rex falls back to silent knowledge_gap_capture instead of pinging.
    owner_phone: str | None = Field(default=None, max_length=32)


class InternalAgentEnsureResponse(BaseModel):
    id: uuid.UUID
    created: bool
    template_id: uuid.UUID | None = None
    skills_seeded: int = 0
    odoo_company_id: int | None = None


def _resolve_template_hint(hint: str | None) -> tuple[str | None, str | None]:
    """Parse 'role-vertical' into (role, vertical). Accepts 'rex-restaurant'
    -> ('rex', 'restaurant'). Multi-word verticals supported via remaining
    split ('tech-restaurant' works; 'mara-retail' works)."""
    if not hint:
        return None, None
    parts = hint.strip().lower().split("-", 1)
    if len(parts) != 2:
        return None, None
    return parts[0], parts[1]


@router.post("/agents/ensure", response_model=InternalAgentEnsureResponse)
async def ensure_agent(
    data: InternalAgentEnsureRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create-or-return a runtime agent mirrored from an apps/isola agent.

    Idempotency:
      - (external_id, tenant_id) -> returns the existing row; updates
        mutable fields (name, role_description, tone, welcome_message,
        escalation_keywords) and returns created=False.
      - First call uses external_id as the runtime Agent UUID when it
        parses as one; otherwise generates a fresh UUID.

    Preconditions:
      - tenant_runtime_id must exist (call /tenants/ensure first).
      - creator_id must exist (call /users/ensure-and-mint first).

    Post-F.1.b-3: will also call OdooService.ensure_company() inline.
    Post-F.1.c: will auto-seed template skills onto the new agent.
    """
    from app.models.agent import Agent, AgentTemplate
    from app.models.tenant import Tenant
    from app.models.user import User

    # 1. Tenant must exist.
    t_r = await db.execute(select(Tenant).where(Tenant.id == data.tenant_runtime_id))
    tenant = t_r.scalar_one_or_none()
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Runtime tenant {data.tenant_runtime_id} not found",
        )

    # 2. Creator must exist + belong to this tenant.
    u_r = await db.execute(select(User).where(User.id == data.creator_id))
    user = u_r.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Creator user {data.creator_id} not found",
        )
    if user.tenant_id != tenant.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Creator {data.creator_id} does not belong to tenant "
                f"{tenant.id}"
            ),
        )

    # 3. Resolve template_hint to AgentTemplate row (role, vertical).
    role, vertical = _resolve_template_hint(data.template_hint)
    template_id: uuid.UUID | None = None
    if role and vertical:
        tpl_r = await db.execute(
            select(AgentTemplate).where(
                AgentTemplate.role == role,
                AgentTemplate.vertical == vertical,
            )
        )
        tpl = tpl_r.scalar_one_or_none()
        if tpl is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"No agent_template matches role={role!r}, "
                    f"vertical={vertical!r} (hint={data.template_hint!r})"
                ),
            )
        template_id = tpl.id

    # 4. Idempotency lookup — try two paths:
    #    (a) by (external_id, tenant_id) — canonical ensure-agent key
    #    (b) by primary key id when external_id parses as a UUID — backfills
    #        rows that pre-dated F.1.a (e.g. Rex was manually inserted during
    #        E.2 dogfood before external_id existed as a column).
    a_r = await db.execute(
        select(Agent).where(
            Agent.external_id == data.external_id,
            Agent.tenant_id == tenant.id,
        )
    )
    existing = a_r.scalar_one_or_none()
    if existing is None:
        try:
            candidate_id = uuid.UUID(data.external_id)
        except (ValueError, AttributeError):
            candidate_id = None
        if candidate_id is not None:
            a_r2 = await db.execute(
                select(Agent).where(
                    Agent.id == candidate_id,
                    Agent.tenant_id == tenant.id,
                )
            )
            existing = a_r2.scalar_one_or_none()
            if existing is not None and existing.external_id is None:
                existing.external_id = data.external_id

    if existing is not None:
        # Update mutable fields, keep id + creator. Template gets backfilled
        # below when the existing row had no template (Rex from E.2 dogfood).
        existing.name = data.name[:100]
        # Only overwrite role_description if caller actually provided one —
        # otherwise we'd clobber a template-derived description with "" when
        # apps/isola doesn't carry that field today (Phase F.1 limitation).
        if data.role_description:
            existing.role_description = data.role_description[:500]
        if data.tone is not None:
            existing.tone = data.tone
        if data.welcome_message is not None:
            existing.welcome_message = data.welcome_message
        if data.escalation_keywords is not None:
            existing.escalation_keywords = data.escalation_keywords
        if data.owner_phone is not None:
            existing.owner_phone = data.owner_phone.strip() or None

        # Backfill template + autonomy + role_description from template defaults
        # for agents that were created before F.1.a-3. Without this, the
        # workspace UI shows "Role: unchanged" and Soul & Memory stays empty.
        if existing.template_id is None and template_id is not None:
            existing.template_id = template_id
            if not existing.role_description or existing.role_description == "unchanged":
                existing.role_description = (tpl.description or "")[:500]
            if not existing.autonomy_policy:
                existing.autonomy_policy = dict(tpl.default_autonomy_policy or {})

        await db.commit()

        # Backfill identity + workspace for agents created before the fix.
        # Each step is independently idempotent so retries are safe.
        from app.models.participant import Participant
        from app.models.agent import AgentPermission as _AgentPermission

        p_r = await db.execute(
            select(Participant).where(
                Participant.type == "agent",
                Participant.ref_id == existing.id,
            )
        )
        if p_r.scalar_one_or_none() is None:
            db.add(Participant(
                type="agent",
                ref_id=existing.id,
                display_name=existing.name,
                avatar_url=existing.avatar_url,
            ))

        perm_r = await db.execute(
            select(_AgentPermission).where(
                _AgentPermission.agent_id == existing.id,
                _AgentPermission.scope_type == "user",
                _AgentPermission.scope_id == user.id,
            )
        )
        if perm_r.scalar_one_or_none() is None:
            db.add(_AgentPermission(
                agent_id=existing.id,
                scope_type="user",
                scope_id=user.id,
                access_level="manage",
            ))

        await db.commit()

        # Workspace files (no-ops if agent_dir already exists).
        from app.services.agent_manager import agent_manager
        try:
            await agent_manager.initialize_agent_files(
                db, existing, personality="", boundaries=""
            )
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "ensure-agent: backfill initialize_agent_files failed for %s",
                existing.id,
            )

        # F.1.b-4 — ensure_company also runs on the existing-agent path so
        # tenants onboarded before Odoo existed still get their res.company
        # on next bridge hit.
        odoo_company_id = tenant.odoo_company_id
        if odoo_company_id is None:
            from app.config import get_settings
            if get_settings().ODOO_ENABLED:
                from app.services.odoo_service import odoo_service, OdooError
                try:
                    odoo_company_id = odoo_service().ensure_company(
                        name=tenant.name or "Isola Tenant",
                        external_ref=str(tenant.id),
                    )
                    tenant.odoo_company_id = odoo_company_id
                    await db.commit()
                except OdooError as e:
                    import logging
                    logging.getLogger(__name__).warning(
                        "ensure_company failed for tenant %s: %s (%s)",
                        tenant.id,
                        e,
                        e.detail,
                    )
                    odoo_company_id = None

        return InternalAgentEnsureResponse(
            id=existing.id,
            created=False,
            template_id=existing.template_id,
            skills_seeded=0,  # F.1.c will count existing skills
            odoo_company_id=odoo_company_id,
        )

    # 5. Create new agent. Reuse external_id as UUID when valid.
    try:
        new_id = uuid.UUID(data.external_id)
    except (ValueError, AttributeError):
        new_id = uuid.uuid4()

    # Resolve template defaults so the agent isn't a hollow shell. Without
    # these, the workspace UI shows "Role: unchanged", "Model: —", and an
    # empty Soul & Memory tab on first iframe load. Mirrors the Phase C
    # provision-from-template flow.
    autonomy_policy: dict = {}
    # Default role_description to the template's description when caller
    # didn't send one — apps/isola doesn't carry role_description on
    # agents today, so new tenants' Rex would otherwise come up with an
    # empty role and fall through to "a digital assistant" in soul.md.
    role_description = data.role_description[:500]
    if template_id is not None:
        # We already loaded the template above; reuse it.
        autonomy_policy = dict(tpl.default_autonomy_policy or {})
        if not role_description:
            role_description = (tpl.description or "")[:500]

    agent = Agent(
        id=new_id,
        external_id=data.external_id,
        name=data.name[:100],
        role_description=role_description,
        tenant_id=tenant.id,
        creator_id=user.id,
        template_id=template_id,
        autonomy_policy=autonomy_policy,
        status="idle",
        agent_type="native",
        tone=data.tone if data.tone is not None else 1,
        welcome_message=data.welcome_message,
        escalation_keywords=data.escalation_keywords or [],
        owner_phone=(data.owner_phone or "").strip() or None,
    )
    db.add(agent)
    await db.flush()

    # Identity row — the chat surface uses Participant for display names
    # and avatars across all conversation participants.
    from app.models.participant import Participant

    db.add(Participant(
        type="agent",
        ref_id=agent.id,
        display_name=agent.name,
        avatar_url=agent.avatar_url,
    ))

    # Creator gets manage access by default.
    from app.models.agent import AgentPermission

    db.add(AgentPermission(
        agent_id=agent.id,
        scope_type="user",
        scope_id=user.id,
        access_level="manage",
    ))

    await db.commit()
    await db.refresh(agent)

    # Seed workspace files (soul.md / memory.md / HEARTBEAT.md / state.json)
    # so Soul & Memory tab in the workspace shows real content instead of
    # blank placeholders. Idempotent — initialize_agent_files no-ops if the
    # agent_dir already exists, so safe across retries.
    from app.services.agent_manager import agent_manager

    try:
        await agent_manager.initialize_agent_files(
            db, agent, personality="", boundaries=""
        )
    except Exception:  # noqa: BLE001
        # Workspace seeding is best-effort — if AGENT_DATA_DIR isn't
        # writable in this deploy, log but don't fail the bridge call.
        # User can recover later via /agents/{id}/reinitialize.
        import logging
        logging.getLogger(__name__).exception(
            "ensure-agent: initialize_agent_files failed for %s", agent.id
        )

    # F.1.b-4 — Odoo company ensure. Runs lazily on the bridge: the first
    # time a tenant hits agent workspace we provision an Odoo res.company
    # for them and cache the id on tenants.odoo_company_id. Skipped when
    # already set OR when ODOO_ENABLED is False. Failures log and return
    # odoo_company_id=None so tenant provisioning never blocks on Odoo.
    odoo_company_id = tenant.odoo_company_id
    if odoo_company_id is None:
        from app.config import get_settings
        if get_settings().ODOO_ENABLED:
            from app.services.odoo_service import odoo_service, OdooError
            try:
                odoo_company_id = odoo_service().ensure_company(
                    name=tenant.name or "Isola Tenant",
                    external_ref=str(tenant.id),
                )
                tenant.odoo_company_id = odoo_company_id
                await db.commit()
            except OdooError as e:
                import logging
                logging.getLogger(__name__).warning(
                    "ensure_company failed for tenant %s: %s (%s)",
                    tenant.id,
                    e,
                    e.detail,
                )
                odoo_company_id = None

    # F.1.c will add skill-seeding here.
    return InternalAgentEnsureResponse(
        id=agent.id,
        created=True,
        template_id=template_id,
        skills_seeded=0,
        odoo_company_id=odoo_company_id,
    )


# ─── Self-learning Tier 1 (F.1.c-batch + #99) ───────────────────────
# Knowledge gap → owner teaches → knowledge.md → Rex remembers loop.


def _agent_workspace_path(agent_id: uuid.UUID):
    """Resolve the on-disk workspace dir for an agent."""
    from pathlib import Path
    from app.config import get_settings
    return Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "workspace"


def _read_workspace_text(path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


class KnowledgeGapOut(BaseModel):
    question: str
    asked_count: int
    last_asked_at: str
    representative_row: str


class KnowledgeGapsResponse(BaseModel):
    pending: list[KnowledgeGapOut]
    total_pending: int
    total_taught: int


@router.get(
    "/agents/{agent_id}/knowledge-gaps",
    response_model=KnowledgeGapsResponse,
)
async def list_knowledge_gaps(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """List unanswered knowledge gaps, deduplicated and ranked by ask
    frequency × recency. Skips rows already marked `taught`."""
    import re
    from app.models.agent import Agent

    a = (
        await db.execute(select(Agent).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")

    raw = _read_workspace_text(_agent_workspace_path(agent_id) / "knowledge-gaps.md")

    # Match a "· taught YYYY-MM-DD" or " - taught YYYY-MM-DD" suffix only —
    # NOT the literal "not yet taught" phrase that knowledge_gap_capture
    # writes into the original row to indicate pending state.
    taught_marker = re.compile(r"(?:·|-)\s*taught\s+\d{4}-\d{2}-\d{2}")

    pending_by_q: dict[str, dict] = {}
    taught_count = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        if taught_marker.search(line):
            taught_count += 1
            continue
        m = re.search(r'asked:\s*"([^"]+)"', line)
        if not m:
            continue
        question_raw = m.group(1).strip()
        norm = re.sub(r"[?!.,\s]+$", "", question_raw.lower().strip())
        ts_match = re.match(r"-\s*!?\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", line)
        ts = ts_match.group(1) if ts_match else ""
        bucket = pending_by_q.setdefault(
            norm,
            {
                "question": question_raw,
                "asked_count": 0,
                "last_asked_at": ts,
                "row": line,
            },
        )
        bucket["asked_count"] += 1
        if ts > bucket["last_asked_at"]:
            bucket["last_asked_at"] = ts
            bucket["question"] = question_raw

    ranked = sorted(
        pending_by_q.values(),
        key=lambda b: (b["asked_count"], b["last_asked_at"]),
        reverse=True,
    )
    return KnowledgeGapsResponse(
        pending=[
            KnowledgeGapOut(
                question=b["question"],
                asked_count=b["asked_count"],
                last_asked_at=b["last_asked_at"],
                representative_row=b["row"],
            )
            for b in ranked
        ],
        total_pending=sum(b["asked_count"] for b in ranked),
        total_taught=taught_count,
    )


class TeachRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    answer: str = Field(..., min_length=1, max_length=4000)
    topic: str | None = Field(default=None, max_length=80)
    teacher_name: str | None = Field(default=None, max_length=80)


class TeachResponse(BaseModel):
    topic: str
    knowledge_md_path: str
    gaps_marked_taught: int


@router.post(
    "/agents/{agent_id}/teach",
    response_model=TeachResponse,
)
async def teach_agent(
    agent_id: uuid.UUID,
    data: TeachRequest,
    db: AsyncSession = Depends(get_db),
):
    """Append a Q&A entry to workspace/knowledge.md and mark matching
    knowledge-gaps rows as taught. Idempotent: re-teaching the same
    topic replaces the existing section in place.

    Implementation lives in services.knowledge_teach so the WA
    owner-reply path (Tier 1.5a) can call the same code without an
    HTTP round-trip."""
    from app.models.agent import Agent
    from app.services.knowledge_teach import record_teach

    a = (
        await db.execute(select(Agent).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")

    result = record_teach(
        agent_id=agent_id,
        question=data.question,
        answer=data.answer,
        topic=data.topic,
        teacher_name=data.teacher_name,
    )
    return TeachResponse(
        topic=result.topic,
        knowledge_md_path=result.knowledge_md_path,
        gaps_marked_taught=result.gaps_marked_taught,
    )


class KnowledgeStatsResponse(BaseModel):
    total_taught: int
    pending_unique: int
    pending_total_asks: int
    learned_this_week: int
    top_pending: list[str]


@router.get(
    "/agents/{agent_id}/knowledge-stats",
    response_model=KnowledgeStatsResponse,
)
async def knowledge_stats(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Counters for the home-page 'Rex learned X things this week' widget."""
    import re
    from datetime import datetime, timezone as _tz, timedelta
    from app.models.agent import Agent

    a = (
        await db.execute(select(Agent).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")

    ws = _agent_workspace_path(agent_id)
    knowledge = _read_workspace_text(ws / "knowledge.md")
    gaps = _read_workspace_text(ws / "knowledge-gaps.md")

    total_taught = len(re.findall(r"^## ", knowledge, re.MULTILINE))

    week_ago = (datetime.now(_tz.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    learned_this_week = sum(
        1
        for m in re.finditer(r"taught (\d{4}-\d{2}-\d{2})", knowledge)
        if m.group(1) >= week_ago
    )

    taught_marker_re = re.compile(r"(?:·|-)\s*taught\s+\d{4}-\d{2}-\d{2}")
    pending_by_q: dict[str, int] = {}
    for line in gaps.splitlines():
        if not line.startswith("- ") or taught_marker_re.search(line):
            continue
        m = re.search(r'asked:\s*"([^"]+)"', line)
        if m:
            norm = re.sub(r"[?!.,\s]+$", "", m.group(1).lower().strip())
            pending_by_q[norm] = pending_by_q.get(norm, 0) + 1

    sorted_q = sorted(pending_by_q.items(), key=lambda kv: kv[1], reverse=True)
    top_pending = [q for q, _ in sorted_q[:5]]
    pending_total_asks = sum(pending_by_q.values())

    return KnowledgeStatsResponse(
        total_taught=total_taught,
        pending_unique=len(pending_by_q),
        pending_total_asks=pending_total_asks,
        learned_this_week=learned_this_week,
        top_pending=top_pending,
    )



# ── L4 S7 R38/R39 (2026-05-05): internal-secret DELETE endpoints ─────
# Soft-retire pattern (mirror S4 agents.retired_at).
#
# Auth: X-Internal-Secret (router-level dep at line 31). Per-callsite
# dep redundant; included for defensive clarity.
#
# Body: optional retired_by UUID (caller-supplied operator identity if
# orchestrating from a higher-level service like admin_crossstore.py).
#
# Idempotency: retiring an already-retired row → 200 with current state,
# NO update (existing retired_at preserved). Mirrors S4 agent retire 409
# semantics inverted: internal endpoints are friendlier to retry.
#
# 404 if not found. No body validation needed (UUID parsed via Path str
# coercion + try/except per S3 bug-2 path-param-as-str rule).

from datetime import datetime as _s7_dt, timezone as _s7_tz
import uuid as _s7_uuid

from app.models.agent import Agent as _S7Agent
from app.models.user import User as _S7User


class _RetireBody(BaseModel):
    retired_by: str | None = Field(
        default=None,
        description="UUID of the user/operator initiating the retire (audit trail). Optional.",
    )


def _parse_uuid_str(value: str, label: str) -> _s7_uuid.UUID:
    try:
        return _s7_uuid.UUID(value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"{label} must be a UUID")


@router.delete("/tenants/{tenant_id}", status_code=200)
async def delete_tenant_soft(
    tenant_id: str,
    body: _RetireBody | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft-retire a tenant by setting retired_at = NOW(). Idempotent."""
    if not tenant_id or len(tenant_id) > 100:
        raise HTTPException(status_code=400, detail="tenant_id must be 1-100 chars")
    tid = _parse_uuid_str(tenant_id, "tenant_id")

    result = await db.execute(select(Tenant).where(Tenant.id == tid))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if getattr(tenant, "retired_at", None) is not None:
        # Idempotent: already retired. Return current state without write.
        return {
            "id": str(tenant.id),
            "retired_at": tenant.retired_at.isoformat(),
            "retired_by": str(tenant.retired_by) if getattr(tenant, "retired_by", None) else None,
            "noop": True,
        }

    tenant.retired_at = _s7_dt.now(_s7_tz.utc)
    if body and body.retired_by:
        tenant.retired_by = _parse_uuid_str(body.retired_by, "retired_by")
    await db.commit()
    await db.refresh(tenant)
    return {
        "id": str(tenant.id),
        "retired_at": tenant.retired_at.isoformat(),
        "retired_by": str(tenant.retired_by) if getattr(tenant, "retired_by", None) else None,
        "noop": False,
    }


@router.delete("/agents/{agent_id}", status_code=200)
async def delete_agent_soft(
    agent_id: str,
    body: _RetireBody | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft-retire an agent (X-Internal-Secret variant of S4 admin retire).

    The S4 endpoint at /api/admin/cross-store/.../agents/{aid}/retire is
    Bearer + platform_admin (operator-console origin). This /internal/
    variant uses X-Internal-Secret (service-to-service origin: cross-store
    orchestrator, future cleanup jobs, BFF cascade workers). Same row write,
    different auth family per memory #28.
    """
    if not agent_id or len(agent_id) > 100:
        raise HTTPException(status_code=400, detail="agent_id must be 1-100 chars")
    aid = _parse_uuid_str(agent_id, "agent_id")

    result = await db.execute(select(_S7Agent).where(_S7Agent.id == aid))
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    if agent.retired_at is not None:
        return {
            "id": str(agent.id),
            "retired_at": agent.retired_at.isoformat(),
            "retired_by": str(agent.retired_by) if agent.retired_by else None,
            "noop": True,
        }

    agent.retired_at = _s7_dt.now(_s7_tz.utc)
    if body and body.retired_by:
        agent.retired_by = _parse_uuid_str(body.retired_by, "retired_by")
    await db.commit()
    await db.refresh(agent)
    return {
        "id": str(agent.id),
        "retired_at": agent.retired_at.isoformat(),
        "retired_by": str(agent.retired_by) if agent.retired_by else None,
        "noop": False,
    }


@router.delete("/users/{user_id}", status_code=200)
async def delete_user_soft(
    user_id: str,
    body: _RetireBody | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft-retire a user. Idempotent."""
    if not user_id or len(user_id) > 100:
        raise HTTPException(status_code=400, detail="user_id must be 1-100 chars")
    uid = _parse_uuid_str(user_id, "user_id")

    result = await db.execute(select(_S7User).where(_S7User.id == uid))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if getattr(user, "retired_at", None) is not None:
        return {
            "id": str(user.id),
            "retired_at": user.retired_at.isoformat(),
            "retired_by": str(user.retired_by) if getattr(user, "retired_by", None) else None,
            "noop": True,
        }

    user.retired_at = _s7_dt.now(_s7_tz.utc)
    if body and body.retired_by:
        user.retired_by = _parse_uuid_str(body.retired_by, "retired_by")
    await db.commit()
    await db.refresh(user)
    return {
        "id": str(user.id),
        "retired_at": user.retired_at.isoformat(),
        "retired_by": str(user.retired_by) if getattr(user, "retired_by", None) else None,
        "noop": False,
    }
