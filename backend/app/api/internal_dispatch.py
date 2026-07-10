"""POST /api/internal/dispatch -- BFF->Clawith dispatch endpoint.

ADR-0070 actions #2, #3, #5 / Day 4b.

This endpoint is the BFF-facing dispatch entry point. BFF posts here
after Meta webhook validation; Clawith fetches SOUL.md + desiredSkills
from Paperclip per request, runs the agent's LLM, returns the draft.

Architecturally separate from app/api/internal.py because that router
has a router-level Depends(require_internal_secret) that applies to all
its routes. Day-4b's design decision #1 calls for a DISTINCT trust
boundary -- BFF uses Authorization: Bearer with its own shared secret
(BFF_CLAWITH_SHARED_SECRET), not the apps/isola X-Internal-Secret.

PKT-04 / decision d35 (2026-07-05): the staging BFF (bff-v2-staging)
presents a second, staging-only value, BFF_CLAWITH_SHARED_SECRET_STAGING.
Both secrets are valid Bearer tokens for this same endpoint -- see
app/core/bff_clawith_auth.py for the matching logic. Production behavior
is unchanged when the staging secret is left unset.

Failure mode (Eric's Day-4b decision #4): FAIL-CLOSED on Paperclip
unreachability. Returns HTTP 503; BFF dead-letters the inbound message
upstream. The /channel/whatsapp/{id}/webhook route is unaffected (and
remains fire-and-forget on Paperclip per paperclip_mirror.py).

Skills are surfaced in the response as informational metadata only --
not yet consumed by the LLM. Step 4 wires per-skill tool definitions +
the read_skill_md body-loader as one coherent change.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import update as _sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.channel_common import _call_agent_llm
from app.core.bff_clawith_auth import (
    any_bff_clawith_secret_configured,
    resolve_bff_clawith_secret,
)
from app.database import get_db
from app.models.agent import Agent as _Agent
from app.services import paperclip_client
from app.services.paperclip_client import PaperclipUnreachable


# Separate router from app/api/internal.py -- no router-level deps.
# Endpoints land at /api/internal/dispatch via main.py's API_PREFIX merge.
router = APIRouter(prefix="/internal", tags=["internal-dispatch"])


class InternalDispatchRequest(BaseModel):
    agent_id: uuid.UUID
    paperclip_agent_id: str
    paperclip_company_id: str
    user_text: str
    user_id: str | None = None
    session_id: str = ""
    history: list[dict] = Field(default_factory=list)
    # DEF-006/007: caller identity + per-tenant odoo (BFF-resolved)
    caller_phone: str | None = None
    owner_phone: str | None = None
    odoo_url: str | None = None
    odoo_db: str | None = None
    odoo_login: str | None = None
    odoo_password: str | None = None
    # Test/Preview console (owner-facing sandbox chat). When True, the same
    # dispatch + LLM + tool-calling path runs as a live request, but any tool
    # tagged in agent_tools.SANDBOX_GATED_TOOLS is short-circuited instead of
    # performing its real side effect (Odoo write, message send, etc). See
    # agent_tools.dispatch_sandbox_mode / SANDBOX_GATED_TOOLS.
    sandbox: bool = False


class InternalDispatchResponse(BaseModel):
    draft: str
    soul_codepoints: int = 0
    skills_count: int = 0
    # Informational only in Day-4b Step 3 -- not yet consumed by the LLM.
    # Step 4 wires per-skill tool definitions + the read_skill_md body-loader.
    skills: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    # Echoes body.sandbox so callers (test-chat UI) can confirm the server
    # actually ran this turn in sandbox mode, not just that the request asked for it.
    sandbox: bool = False
    # S5: True when a tool this turn could not confidently answer from grounded
    # Odoo data (see agent_tools.needs_handoff_signal). BFF reads this and pings
    # the human owner via the existing pingOperatorOnEscalation path.
    needs_handoff: bool = False


async def _verify_bff_shared_secret(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    """FastAPI dep: 401 unless Authorization: Bearer matches the prod or
    staging BFF-Clawith shared secret (see app/core/bff_clawith_auth.py)."""
    if not any_bff_clawith_secret_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="dispatch_misconfigured: BFF_CLAWITH_SHARED_SECRET unset",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing_bearer_token",
        )
    presented = authorization[7:]
    if resolve_bff_clawith_secret(presented) is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bad_bearer_token",
        )


@router.post("/dispatch", response_model=InternalDispatchResponse)
async def internal_dispatch(
    body: InternalDispatchRequest,
    _auth: None = Depends(_verify_bff_shared_secret),
    db: AsyncSession = Depends(get_db),
) -> InternalDispatchResponse:
    """BFF->Clawith dispatch entry point.

    Per ADR-0070 action #3 (temporary integration mode). Pulls SOUL +
    desiredSkills from Paperclip, invokes _call_agent_llm with SOUL as
    role_description override. Day 4b Step 3 does NOT persist soul to
    the local agents.soul column -- pull-on-every-invocation = always
    fresh; the schema column (committed in migration f16) is reserved
    for future fallback-cache use.
    """
    # 1. SOUL fetch (fail-closed)
    try:
        soul = await paperclip_client.fetch_paperclip_soul(body.paperclip_agent_id)
    except PaperclipUnreachable as e:
        logger.error(f"[dispatch] paperclip_unreachable on SOUL fetch: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"paperclip_unreachable: {e}",
        )

    # 2. Skills fetch (fail-closed)
    try:
        skill_refs = await paperclip_client.fetch_paperclip_desired_skills(
            body.paperclip_agent_id, body.paperclip_company_id,
        )
    except PaperclipUnreachable as e:
        logger.error(f"[dispatch] paperclip_unreachable on skills fetch: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"paperclip_unreachable: {e}",
        )

    soul_str = soul or ""
    logger.info(
        f"[dispatch] resolved SOUL={len(soul_str)} codepoints, "
        f"skills={len(skill_refs)} agent={body.agent_id}"
    )

    # Day 4b Step 4: write paperclip bridge IDs to local Agent record.
    # Idempotent (overwrites with same values on repeat dispatches);
    # required so the read_skill_md tool handler can resolve Paperclip
    # context. IDs are post-provisioning-immutable -- no stale-cache risk.
    await db.execute(
        _sql_update(_Agent)
        .where(_Agent.id == body.agent_id)
        .values(
            paperclip_agent_id=body.paperclip_agent_id,
            paperclip_company_id=body.paperclip_company_id,
        )
    )
    await db.commit()

    # 3. Build effective role: SOUL.md + Available-skills section so the
    #    LLM knows what skills exist + that read_skill_md is the loader.
    #    (Day 4b Step 4 wiring -- corresponds to read_skill_md in AGENT_TOOLS.)
    if skill_refs:
        skill_lines = "\n".join(
            "- " + s["slug"] + ": " + s["description"] for s in skill_refs
        )
        skill_section = (
            "\n\n## Available skills\n\n"
            f"{skill_lines}\n\n"
            "When a customer\'s request matches one of these skills, call "
            "`read_skill_md(skill_name=\"<slug>\")` to get the full "
            "instructions, then follow them carefully."
        )
        effective_role = (soul_str or "") + skill_section
    else:
        effective_role = soul_str or ""

    # DEF-006/007: bind per-tenant odoo + caller identity before tools run.
    from app.services.odoo_service import odoo_context
    from app.services.agent_tools import caller_context
    if body.odoo_db and body.odoo_password:
        _ourl = body.odoo_url or "http://odoo-epic-app:8069"
        if "127.0.0.1" in _ourl or "localhost" in _ourl:
            _ourl = "http://odoo-epic-app:8069"  # container-net translation
        odoo_context.set({"url": _ourl, "db": body.odoo_db, "login": body.odoo_login or "admin", "password": body.odoo_password})
    if body.caller_phone:
        from app.services.caller_identity import resolve_caller
        class _A: owner_phone = body.owner_phone
        _cc = await resolve_caller(_A(), body.caller_phone)
        caller_context.set(_cc)
        _role = _cc.get("role")
        # DEF-006/007 + AgriLink owner-identity fix:
        # A business-data agent is one with per-tenant Odoo configured
        # (body.odoo_db + body.odoo_password present). ONLY such agents may
        # divert the OWNER into business-owner mode (balances/debtors/reports).
        # For customer-facing agents with NO business data (e.g. Isola AgriLink),
        # the owner must KEEP the agent's soul/identity -- the agent stays itself
        # and simply acknowledges this is its owner/admin, never flipping into a
        # generic business-owner/executive assistant that ignores the soul.
        # This mirrors the BFF-side fix (resolveOdooConnection().isConfigured;
        # commit 0cb58c8f) so both dispatch paths classify owners identically.
        _is_business_data_agent = bool(body.odoo_db and body.odoo_password)
        if _role == "owner" and _is_business_data_agent:
            effective_role += "\n\n## CALLER CONTEXT (system-verified)\nYou are speaking with the BUSINESS OWNER (verified by phone). Give direct business answers: balances, debtors, summaries, reports. Use your business data tools freely. Never give them the customer sales pitch."
        elif _role == "owner":
            # Customer-facing / non-business-data agent: stay in character.
            effective_role += "\n\n## CALLER CONTEXT (system-verified)\nYou are speaking with your OWNER / ADMIN (verified by phone). Stay fully in your own identity and persona -- do NOT switch into a generic business-owner or executive assistant. You have no business-data/finance tools to offer. Greet them warmly as yourself, and if they ask you to do something operational you cannot do, say so plainly in your own voice. You may speak a little more candidly than with the public, but you remain exactly who you are."
        elif _role == "customer":
            effective_role += "\n\n## CALLER CONTEXT (system-verified)\nYou are speaking with a VERIFIED EXISTING CUSTOMER. You may look up and discuss THEIR OWN account and invoices (use get_customer_balance - no arguments needed). Never discuss other customers or internal business data."
        else:
            effective_role += "\n\n## CALLER CONTEXT (system-verified)\nThis caller is an UNIDENTIFIED member of the public - treat as a potential customer (lead). Help with products, prices, booking. NEVER reveal business internals, financials, or any customer information."
    # 4. Run inference. role_description_override carries SOUL + skill list.
    #    Test/Preview console: set the sandbox contextvar BEFORE the LLM runs
    #    so any tool call the LLM makes during this turn sees it (agent_tools
    #    reads it in execute_tool, same contextvar-per-request pattern already
    #    used by odoo_context/caller_context elsewhere in this file's history).
    from app.services.agent_tools import dispatch_sandbox_mode, needs_handoff_signal
    dispatch_sandbox_mode.set(bool(body.sandbox))
    needs_handoff_signal.set(False)
    try:
        draft = await _call_agent_llm(
            db=db,
            agent_id=body.agent_id,
            user_text=body.user_text,
            history=body.history,
            user_id=body.user_id,
            session_id=body.session_id,
            role_description_override=effective_role if effective_role else None,
        )
    except Exception as e:
        logger.error(f"[dispatch] LLM call failed: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"llm_error: {type(e).__name__}: {str(e)[:200]}",
        )

    return InternalDispatchResponse(
        draft=draft,
        soul_codepoints=len(soul_str),
        skills_count=len(skill_refs),
        skills=skill_refs,
        sandbox=bool(body.sandbox),
        needs_handoff=bool(needs_handoff_signal.get()),
    )
