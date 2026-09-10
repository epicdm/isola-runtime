"""POST /api/internal/paperclip-run -- Paperclip http-adapter receiver.

Sits beside /internal/dispatch, behind the SAME Depends(_verify_bff_shared_secret)
bearer boundary, and is reached from paperclip-paperclip-1 over isola-net.

THE REQUEST BODY IS NOT A CHOICE. It is dictated by Paperclip's http adapter,
server/src/adapters/http/execute.ts:13:

    const body = { ...payloadTemplate, agentId: agent.id, runId, context };

so agentId / runId / context are always present and always win over anything
in payloadTemplate, and everything else in the body is whatever the operator
put in payloadTemplate. This model mirrors that exactly, and ignores unknown
keys rather than 422-ing on them -- a 422 would be indistinguishable, to the
adapter, from any other failure.

THE RESPONSE BODY IS NEVER READ. execute.ts:29-38 checks res.ok, throws on
non-2xx with only the status number, and returns a fixed result object. It
never parses the body. So the body here exists for humans, for Lumen and for
the log -- never as a control channel -- and the STATUS CODE is the only
signal the adapter receives. That is why a timed-out run answers 504 and a
failed run answers 502: those are the only way the adapter learns anything
other than "fine".

SECRET CONTAINMENT. This route returns an explicit, closed set of fields. It
never echoes `context` back, never returns anything it fetched from
Paperclip's agents API (which hands out an Ed25519 devicePrivateKeyPem and a
bearer token in plaintext), and never logs the Authorization header. The one
Paperclip response it reads is the issue record, and it reads two fields off
it.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.internal_dispatch import _verify_bff_shared_secret
from app.database import get_db
from app.models.agent import Agent as AgentModel
from app.models.paperclip_run import PaperclipRun
from app.services import paperclip_client
from app.services.paperclip_client import PaperclipUnreachable
from app.services.run_bounds import (
    INT_001_ALLOWED_TOOLS,
    STATE_ACCEPTED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_TIMED_OUT,
    AllowlistError,
    BoundsExceeded,
    RunBounds,
    RunFenced,
    active_bounds,
    install as install_bounds,
    resolve_allowlist,
)

# Same shape as internal_dispatch: a router with no router-level deps, mounted
# under settings.API_PREFIX in main.py, so this lands at
# /api/internal/paperclip-run.
router = APIRouter(prefix="/internal", tags=["internal-paperclip-run"])


# ---------------------------------------------------------------------------
# Authoritative identifiers. Env-overridable so staging can differ, but with
# INT-001's real values as the default, and NOTHING is accepted that does not
# match.
# ---------------------------------------------------------------------------
def _allowed_company_id() -> str:
    return os.environ.get(
        "PAPERCLIP_RUN_COMPANY_ID", "48f327a1-244e-4b58-ae1e-8222b6472794"
    ).strip()


def _allowed_agent_id() -> str:
    return os.environ.get(
        "PAPERCLIP_RUN_AGENT_ID", "e1a4a504-17a2-4020-9303-7057e77ae0c9"
    ).strip()


def _duration_budget_s() -> float:
    try:
        return float(os.environ.get("PAPERCLIP_RUN_DURATION_BUDGET_S", "110"))
    except ValueError:
        return 110.0


def _service_principal_user_id() -> uuid.UUID | None:
    """The scoped service identity this run executes as.

    Deliberately has NO fallback. If it is unset or unparseable the run is
    refused. The tempting fallback -- pass the creator's id, as
    heartbeat.py:362 and agent_tools.py:4762 do -- would make the COO execute
    as its owner, which is the exact failure mode this route exists to avoid.
    """
    raw = os.environ.get("PAPERCLIP_RUN_SERVICE_PRINCIPAL_USER_ID", "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Wire contract
# ---------------------------------------------------------------------------
class PaperclipRunRequest(BaseModel):
    """Mirrors execute.ts's body. Unknown payloadTemplate keys are ignored."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    agent_id: str = Field(alias="agentId")
    run_id: str = Field(alias="runId")
    context: dict | None = None

    # Supplied via adapterConfig.payloadTemplate.
    company_id: str | None = Field(default=None, alias="companyId")
    issue_id: str | None = Field(default=None, alias="issueId")


class PaperclipRunResponse(BaseModel):
    """Closed set of fields. Nothing here is derived from a secret."""

    run_id: str
    agent_id: str
    state: str
    duplicate: bool = False
    provider_calls: int = 0
    denied_tools: list[str] = Field(default_factory=list)
    comment_posted: bool = False
    comment_id: str | None = None
    principal: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _redact(exc: BaseException, limit: int = 200) -> str:
    """A type name and a truncated message. Never a header, never a payload."""
    return f"{type(exc).__name__}: {str(exc)[:limit]}"


async def _finalize(
    db: AsyncSession,
    run_id: str,
    *,
    state: str,
    provider_calls: int = 0,
    denied_tools: list[str] | None = None,
    error: str | None = None,
) -> None:
    await db.execute(
        sql_update(PaperclipRun)
        .where(PaperclipRun.run_id == run_id)
        .values(
            state=state,
            provider_calls=provider_calls,
            denied_tools=",".join(sorted(set(denied_tools or []))) or None,
            error=error,
            finished_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def _claim_comment_slot(db: AsyncSession, run_id: str) -> bool:
    """Atomically claim the single comment slot for this run.

    A conditional UPDATE, not a read-then-write: two concurrent finishers
    cannot both observe comment_posted=False and both post.
    """
    result = await db.execute(
        sql_update(PaperclipRun)
        .where(PaperclipRun.run_id == run_id, PaperclipRun.comment_posted.is_(False))
        .values(comment_posted=True)
        .returning(PaperclipRun.id)
    )
    claimed = result.first() is not None
    await db.commit()
    return claimed


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
@router.post("/paperclip-run", response_model=PaperclipRunResponse)
async def paperclip_run(
    body: PaperclipRunRequest,
    _auth: None = Depends(_verify_bff_shared_secret),
    db: AsyncSession = Depends(get_db),
) -> PaperclipRunResponse:
    install_bounds()

    company_id = (body.company_id or "").strip()
    agent_id = body.agent_id.strip()
    run_id = body.run_id.strip()

    # -- 1. Identifier validation, default-deny ------------------------------
    if not run_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="missing_run_id")
    if agent_id != _allowed_agent_id():
        logger.warning(f"[paperclip-run] refused: agent {agent_id} not permitted")
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="agent_not_permitted")
    if company_id != _allowed_company_id():
        logger.warning("[paperclip-run] refused: company not permitted")
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="company_not_permitted")
    if not body.issue_id:
        # The permitted work is scoped to one issue. No issue, no run.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="missing_issue_id")

    # -- 2. Scoped service principal, or nothing -----------------------------
    principal_id = _service_principal_user_id()
    if principal_id is None:
        logger.error("[paperclip-run] refused: service principal unset")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="receiver_misconfigured: PAPERCLIP_RUN_SERVICE_PRINCIPAL_USER_ID unset",
        )

    # -- 3. Authoritative record checks --------------------------------------
    # The issue must exist and belong to the permitted company. Two fields are
    # read off the response; the response itself is never propagated.
    try:
        issue = await paperclip_client._request_json("GET", f"/api/issues/{body.issue_id}")
    except PaperclipUnreachable as e:
        logger.error(f"[paperclip-run] issue lookup failed: {_redact(e)}")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="paperclip_unreachable"
        )
    if str(issue.get("companyId") or "") != company_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="issue_not_in_company")

    # The runtime-side agent this Paperclip agent is bridged to. Written by
    # /internal/dispatch; if it has never dispatched there is nothing to run.
    agent_row = (
        await db.execute(
            select(AgentModel).where(AgentModel.paperclip_agent_id == agent_id)
        )
    ).scalar_one_or_none()
    if agent_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="agent_not_bridged")

    # -- 4. Idempotent claim -------------------------------------------------
    # ON CONFLICT DO NOTHING. Under concurrent duplicate delivery exactly one
    # caller inserts; everyone else falls through to the read below and
    # executes nothing.
    claim = await db.execute(
        pg_insert(PaperclipRun)
        .values(
            id=uuid.uuid4(),
            run_id=run_id,
            paperclip_agent_id=agent_id,
            paperclip_company_id=company_id,
            issue_id=body.issue_id,
            agent_id=agent_row.id,
            state=STATE_ACCEPTED,
            principal_user_id=principal_id,
            principal_label="isola.service.int001",
        )
        .on_conflict_do_nothing(index_elements=["run_id"])
        .returning(PaperclipRun.id)
    )
    won = claim.first() is not None
    await db.commit()

    if not won:
        existing = (
            await db.execute(select(PaperclipRun).where(PaperclipRun.run_id == run_id))
        ).scalar_one()
        logger.info(f"[paperclip-run] duplicate delivery run={run_id} state={existing.state}")
        return PaperclipRunResponse(
            run_id=run_id,
            agent_id=agent_id,
            state=existing.state,
            duplicate=True,
            provider_calls=existing.provider_calls,
            denied_tools=(existing.denied_tools or "").split(",") if existing.denied_tools else [],
            comment_posted=existing.comment_posted,
            comment_id=existing.comment_id,
            principal=existing.principal_label,
            detail="already_delivered",
        )

    # -- 5. Bounds ------------------------------------------------------------
    try:
        allowed = resolve_allowlist(INT_001_ALLOWED_TOOLS)
    except AllowlistError as e:
        # Fail closed. An allowlist that cannot be resolved against the real
        # registry is the F1 failure: it looks strict and permits everything.
        await _finalize(db, run_id, state=STATE_FAILED, error=_redact(e))
        logger.error(f"[paperclip-run] allowlist unresolvable: {e}")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="allowlist_unresolvable"
        )

    bounds = RunBounds(
        run_id=run_id,
        allowed_tools=allowed,
        max_provider_calls=3,
        max_input_tokens=12_000,
        max_output_tokens=2_000,
        duration_budget_s=_duration_budget_s(),
    )
    token = active_bounds.set(bounds)

    await db.execute(
        sql_update(PaperclipRun).where(PaperclipRun.run_id == run_id).values(state=STATE_RUNNING)
    )
    await db.commit()

    # -- 6. Execute -----------------------------------------------------------
    from app.api.channel_common import _call_agent_llm

    task_text = str((body.context or {}).get("instruction") or issue.get("title") or "").strip()
    if not task_text:
        active_bounds.reset(token)
        await _finalize(db, run_id, state=STATE_FAILED, error="no_instruction")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="no_instruction")

    try:
        soul = await paperclip_client.fetch_paperclip_soul(agent_id)
    except PaperclipUnreachable as e:
        active_bounds.reset(token)
        await _finalize(db, run_id, state=STATE_FAILED, error=_redact(e))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="paperclip_unreachable"
        )

    draft: str | None = None
    outcome = STATE_FAILED
    detail: str | None = None
    http_status = status.HTTP_502_BAD_GATEWAY

    try:
        draft = await asyncio.wait_for(
            _call_agent_llm(
                db=db,
                agent_id=agent_row.id,
                user_text=task_text,
                history=[],
                user_id=principal_id,
                session_id=f"paperclip-run:{run_id}",
                role_description_override=(soul or "") or None,
            ),
            timeout=bounds.duration_budget_s,
        )
        outcome = STATE_COMPLETED
        http_status = status.HTTP_200_OK
    except asyncio.TimeoutError:
        # Close the fence BEFORE answering. Whatever task is still unwinding
        # cannot issue another provider call: admit_provider_call raises
        # RunFenced on every subsequent attempt, and the acceptance test
        # asserts the provider call count does not move after this point.
        bounds.close_fence("receiver_timeout")
        outcome = STATE_TIMED_OUT
        detail = "timed_out"
        http_status = status.HTTP_504_GATEWAY_TIMEOUT
        logger.warning(f"[paperclip-run] run={run_id} timed out; fence closed")
    except (BoundsExceeded, RunFenced) as e:
        bounds.close_fence("bounds_exceeded")
        outcome = STATE_FAILED
        detail = "bounds_exceeded"
        logger.warning(f"[paperclip-run] run={run_id} {_redact(e)}")
    except Exception as e:  # noqa: BLE001 - any provider/tool failure
        bounds.close_fence("error")
        outcome = STATE_FAILED
        detail = "run_error"
        logger.error(f"[paperclip-run] run={run_id} failed: {_redact(e)}")
    finally:
        active_bounds.reset(token)

    # -- 7. Exactly one comment, and only on a completed run -----------------
    comment_id: str | None = None
    if outcome == STATE_COMPLETED and draft:
        if await _claim_comment_slot(db, run_id):
            first_line = f"run={run_id} agent={agent_id}"
            try:
                created = await paperclip_client._request_json(
                    "POST",
                    f"/api/issues/{body.issue_id}/comments",
                    {"body": f"{first_line}\n\n{draft}"},
                )
                comment_id = str(created.get("id") or "") or None
                await db.execute(
                    sql_update(PaperclipRun)
                    .where(PaperclipRun.run_id == run_id)
                    .values(comment_id=comment_id)
                )
                await db.commit()
            except PaperclipUnreachable as e:
                # The slot stays claimed: a retry must not double-post.
                outcome = STATE_FAILED
                detail = "comment_write_failed"
                logger.error(f"[paperclip-run] comment write failed: {_redact(e)}")

    await _finalize(
        db,
        run_id,
        state=outcome,
        provider_calls=bounds.provider_calls,
        denied_tools=bounds.denied_tools,
        error=detail,
    )

    payload = PaperclipRunResponse(
        run_id=run_id,
        agent_id=agent_id,
        state=outcome,
        duplicate=False,
        provider_calls=bounds.provider_calls,
        denied_tools=sorted(set(bounds.denied_tools)),
        comment_posted=comment_id is not None,
        comment_id=comment_id,
        principal="isola.service.int001",
        detail=detail,
    )

    if outcome != STATE_COMPLETED:
        # The adapter only reads the status code (execute.ts:29). A non-2xx is
        # the only way it learns this run did not succeed.
        raise HTTPException(status_code=http_status, detail=payload.model_dump())
    return payload
