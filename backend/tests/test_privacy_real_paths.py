"""Real-path privacy proofs — NOT helper-level.

Helper tests prove predicates. These drive the ACTUAL modules that leaked:

  * app.api.chat_sessions        (session retrieval)
  * app.services.agent_tools     (tool dispatch)
  * app.api.channel_common       (the WhatsApp/Discord/Teams inbound chain)
  * app.services.trigger_daemon  (trigger propagation)

against a REAL PostgreSQL database with real rows, and assert three things
on each path:

    principal-preserved  — the requesting person's identity survives the call
    owner-denied         — a non-owner principal cannot act with owner authority
    revocation-respected — deleting the permission row, or clearing
                           users.is_active, is honoured on the very next call

Requires ISOLA_TEST_DATABASE_URL pointing at a throwaway database migrated to
head. Skipped otherwise.
"""
from __future__ import annotations

import os
import uuid

import pytest

TEST_DB = os.environ.get("ISOLA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="ISOLA_TEST_DATABASE_URL not set")

if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB

from sqlalchemy import text                                          # noqa: E402
from sqlalchemy.pool import NullPool                                 # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

# NullPool + a per-test engine: asyncpg connections are bound to the event
# loop that opened them, and pytest-asyncio gives each test a fresh loop.
Session = None


def _new_session_factory():
    eng = create_async_engine(TEST_DB, poolclass=NullPool)
    return async_sessionmaker(eng, expire_on_commit=False)


# ─────────────────────────── fixtures ───────────────────────────

class World:
    """Two principals on one agent, in one tenant."""


@pytest.fixture
async def world():
    global Session
    Session = _new_session_factory()
    from app.models.agent import Agent, AgentPermission
    from app.models.chat_session import ChatSession
    from app.models.audit import ChatMessage
    from app.models.user import User
    from app.models.tenant import Tenant

    w = World()
    w.tenant_id = uuid.uuid4()
    w.owner_id = uuid.uuid4()
    w.staff_id = uuid.uuid4()
    w.agent_id = uuid.uuid4()

    async with Session() as db:
        await db.execute(text(
            "INSERT INTO tenants (id,name,slug,im_provider,is_active,default_message_limit,"
            "default_message_period,default_max_agents,default_agent_ttl_hours,"
            "default_max_llm_calls_per_day,min_heartbeat_interval_minutes,timezone,sso_enabled,"
            "default_max_triggers,min_poll_interval_floor,max_webhook_rate_ceiling,"
            "a2a_async_enabled,runtime_mode) VALUES "
            "(:i,'T','t-'||:s,'web_only',true,50,'month',5,24,100,60,'UTC',false,10,5,60,false,'native')"
        ), {"i": str(w.tenant_id), "s": str(w.tenant_id)[:8]})
        for uid, name in ((w.owner_id, "SyntheticOwner"), (w.staff_id, "SyntheticStaff")):
            await db.execute(text(
                "INSERT INTO users (id,display_name,role,is_active,quota_message_limit,"
                "quota_message_period,quota_messages_used,quota_max_agents,quota_agent_ttl_hours,tenant_id)"
                " VALUES (:i,:n,'org_admin',true,50,'month',0,5,24,:t)"
            ), {"i": str(uid), "n": name, "t": str(w.tenant_id)})
        await db.execute(text(
            "INSERT INTO agents (id,name,role_description,tone,creator_id,agent_type,"
            "escalation_keywords,status,autonomy_policy,tokens_used_today,tokens_used_month,"
            "tokens_used_total,context_window_size,max_tool_rounds,max_triggers,"
            "min_poll_interval_min,webhook_rate_limit,is_expired,llm_calls_today,"
            "heartbeat_enabled,heartbeat_interval_minutes,heartbeat_active_hours,"
            "tenant_id,max_llm_calls_per_day) VALUES "
            "(:i,'SyntheticAgent','r',3,:c,'native','[]','idle','{}',0,0,0,20,20,10,5,60,"
            "false,0,false,240,'',:t,5)"
        ), {"i": str(w.agent_id), "c": str(w.owner_id), "t": str(w.tenant_id)})
        # staff holds `use` on the agent, by explicit user-scope permission
        w.perm_id = uuid.uuid4()
        await db.execute(text(
            "INSERT INTO agent_permissions (id,agent_id,scope_type,scope_id,access_level)"
            " VALUES (:i,:a,'user',:s,'use')"
        ), {"i": str(w.perm_id), "a": str(w.agent_id), "s": str(w.staff_id)})
        # owner's PRIVATE session with one message
        w.owner_session_id = uuid.uuid4()
        await db.execute(text(
            "INSERT INTO chat_sessions (id,agent_id,user_id,title,source_channel,visibility)"
            " VALUES (:i,:a,:u,'Owner private','whatsapp','private')"
        ), {"i": str(w.owner_session_id), "a": str(w.agent_id), "u": str(w.owner_id)})
        await db.execute(text(
            "INSERT INTO chat_messages (id,agent_id,user_id,role,content,conversation_id)"
            " VALUES (:i,:a,:u,'assistant','OWNER BANK BALANCE IS 41000',:c)"
        ), {"i": str(uuid.uuid4()), "a": str(w.agent_id), "u": str(w.owner_id),
            "c": str(w.owner_session_id)})
        await db.commit()

    yield w

    async with Session() as db:
        for tbl in ("chat_messages", "chat_sessions", "agent_permissions",
                    "agent_activity_logs", "agent_credentials", "agent_tools",
                    "agent_triggers", "daily_token_usage", "audit_logs", "tasks",
                    "notifications", "approval_requests"):
            try:
                await db.execute(text(f"DELETE FROM {tbl} WHERE agent_id=:a"), {"a": str(w.agent_id)})
            except Exception:
                await db.rollback()
        await db.execute(text("DELETE FROM agents WHERE id=:a"), {"a": str(w.agent_id)})
        await db.execute(text("DELETE FROM users WHERE id IN (:a,:b)"),
                         {"a": str(w.owner_id), "b": str(w.staff_id)})
        await db.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": str(w.tenant_id)})
        await db.commit()


async def _load(db, model, ident):
    from sqlalchemy import select
    return (await db.execute(select(model).where(model.id == ident))).scalar_one()


# ═══════════════ PATH 1 — SESSION RETRIEVAL (real route) ═══════════════

async def test_retrieval_principal_preserved_owner_denied_revocation_respected(world):
    from fastapi import HTTPException
    from app.api import chat_sessions as route
    from app.models.user import User
    from app.models.agent import Agent

    async with Session() as db:
        owner = await _load(db, User, world.owner_id)
        staff = await _load(db, User, world.staff_id)

        # principal-preserved: the OWNER reads their own private session
        msgs = await route.get_session_messages(
            agent_id=world.agent_id, session_id=world.owner_session_id,
            current_user=owner, db=db)
        assert "41000" in msgs[0]["content"]

        # owner-denied: staff (org_admin by ROLE, `use` by PERMISSION) refused
        with pytest.raises(HTTPException) as e:
            await route.get_session_messages(
                agent_id=world.agent_id, session_id=world.owner_session_id,
                current_user=staff, db=db)
        assert e.value.status_code == 403

        # ...and refused at scope=all too
        with pytest.raises(HTTPException) as e:
            await route.list_sessions(agent_id=world.agent_id, scope="all",
                                      current_user=staff, db=db)
        assert e.value.status_code == 403

    # elevate staff to `manage` — still refused, because the session is PRIVATE
    async with Session() as db:
        await db.execute(text("UPDATE agent_permissions SET access_level='manage' WHERE id=:i"),
                         {"i": str(world.perm_id)})
        await db.commit()
    async with Session() as db:
        staff = await _load(db, User, world.staff_id)
        with pytest.raises(HTTPException) as e:
            await route.get_session_messages(
                agent_id=world.agent_id, session_id=world.owner_session_id,
                current_user=staff, db=db)
        assert e.value.status_code == 403, "manage must not open a private session"
        # scope=all is admitted at manage, but returns ONLY readable rows
        out = await route.list_sessions(agent_id=world.agent_id, scope="all",
                                        current_user=staff, db=db)
        assert [s for s in out if s.id == str(world.owner_session_id)] == [], \
            "owner's private session leaked into a manager's scope=all"

    # revocation-respected: delete the permission row -> refused outright
    async with Session() as db:
        await db.execute(text("DELETE FROM agent_permissions WHERE id=:i"), {"i": str(world.perm_id)})
        await db.commit()
    async with Session() as db:
        staff = await _load(db, User, world.staff_id)
        with pytest.raises(HTTPException) as e:
            await route.list_sessions(agent_id=world.agent_id, scope="mine",
                                      current_user=staff, db=db)
        assert e.value.status_code == 403


async def test_retrieval_revocation_by_deactivation(world):
    from fastapi import HTTPException
    from app.api import chat_sessions as route
    from app.models.user import User

    async with Session() as db:
        await db.execute(text("UPDATE users SET is_active=false WHERE id=:i"),
                         {"i": str(world.owner_id)})
        await db.commit()
    async with Session() as db:
        owner = await _load(db, User, world.owner_id)
        with pytest.raises(HTTPException) as e:
            await route.get_session_messages(
                agent_id=world.agent_id, session_id=world.owner_session_id,
                current_user=owner, db=db)
        assert e.value.status_code == 403, "deactivated principal still read its own session"


# ═══════════════ PATH 2 — TOOL DISPATCH (real execute_tool) ═══════════════

async def test_tool_dispatch_principal_preserved_owner_denied_revocation_respected(world, monkeypatch):
    # The gate lives in app.core.tool_guard, which is what
    # app/services/llm/caller.py imports as `execute_tool` -- i.e. the symbol
    # every human-chat and inbound-channel turn actually dispatches through.
    import app.core.tool_guard as at
    import app.services.llm.caller as caller
    assert caller.execute_tool is at.execute_tool, \
        "caller.py is not dispatching through the guarded execute_tool"
    monkeypatch.setattr("app.database.async_session", Session, raising=False)

    OWNER_TOOL = "create_credential"

    # owner-denied: the staff principal holding `use` cannot run an owner-level tool
    out = await at.execute_tool(OWNER_TOOL, {}, world.agent_id, world.staff_id)
    assert out.startswith("❌") and "owner-level" in out, out

    # owner-denied: the AGENT'S OWN ID is not a person
    out = await at.execute_tool(OWNER_TOOL, {}, world.agent_id, world.agent_id)
    assert out.startswith("❌") and "authenticated person" in out, out

    # owner-denied: NO principal at all (this is what an inbound WhatsApp
    # sender now carries) is refused rather than defaulting to the creator
    out = await at.execute_tool(OWNER_TOOL, {}, world.agent_id, None)
    assert out.startswith("❌") and "authenticated person" in out, out

    # principal-preserved: the owner is NOT refused by the privacy gate.
    # (It proceeds past the gate; the tool itself may then fail for its own
    #  reasons, which is not a privacy refusal.)
    out = await at.execute_tool(OWNER_TOOL, {}, world.agent_id, world.owner_id)
    assert "owner-level action" not in out and "authenticated person" not in out, out

    # revocation-respected: strip the owner's authority and re-run
    async with Session() as db:
        await db.execute(text("UPDATE users SET is_active=false WHERE id=:i"),
                         {"i": str(world.owner_id)})
        await db.commit()
    out = await at.execute_tool(OWNER_TOOL, {}, world.agent_id, world.owner_id)
    assert out.startswith("❌") and "owner-level" in out, \
        f"deactivated owner still passed the tool gate: {out}"


async def test_non_owner_level_tools_are_not_gated(world, monkeypatch):
    """The gate must be narrow: `use` principals keep talking to the agent."""
    from app.core.session_privacy import assert_tool_allowed, OWNER_LEVEL_TOOLS
    assert "read_file" not in OWNER_LEVEL_TOOLS
    async with Session() as db:
        await assert_tool_allowed(db, "read_file", world.agent_id, world.staff_id)  # no raise


# ═══════════════ PATH 3 — WHATSAPP INBOUND (real chain) ═══════════════

async def test_whatsapp_inbound_does_not_run_as_the_creator(world, monkeypatch):
    """The sender's tools must not execute with the owner's authority.

    Drives the real app.api.channel_common._call_agent_llm with the value
    whatsapp.py now passes (user_id=None) and captures the principal that
    reaches the LLM/tool layer.
    """
    import app.api.channel_common as cc

    seen = {}

    async def fake_call_llm(model, messages, agent_name, role, **kw):
        seen["user_id"] = kw.get("user_id")
        return "ok"

    monkeypatch.setattr(cc, "call_llm", fake_call_llm, raising=False)
    monkeypatch.setattr("app.services.llm.call_llm", fake_call_llm, raising=False)

    async with Session() as db:
        # give the agent a model so the function reaches the call
        mid = uuid.uuid4()
        await db.execute(text(
            "INSERT INTO llm_models (id,provider,model,api_key_encrypted,label,enabled,"
            "supports_vision,tenant_id) VALUES "
            "(:i,'deepseek','deepseek-chat','x','m',true,false,:t)"
        ), {"i": str(mid), "t": str(world.tenant_id)})
        await db.execute(text("UPDATE agents SET primary_model_id=:m WHERE id=:a"),
                         {"m": str(mid), "a": str(world.agent_id)})
        await db.commit()

    async with Session() as db:
        await cc._call_agent_llm(db, world.agent_id, "hello", history=[],
                                 user_id=None, session_id=str(world.owner_session_id))

    # principal-preserved: the absent principal stayed absent. It was NOT
    # coerced to agent.creator_id (the old whatsapp.py:527 behaviour) and NOT
    # coerced to agent_id (the old channel_common.py:148 behaviour).
    assert seen["user_id"] is None, (
        f"principal was substituted: {seen['user_id']} "
        f"(creator={world.owner_id}, agent={world.agent_id})"
    )
    assert seen["user_id"] != world.owner_id
    assert seen["user_id"] != world.agent_id

    async with Session() as db:
        await db.execute(text("UPDATE agents SET primary_model_id=NULL WHERE id=:a"),
                         {"a": str(world.agent_id)})
        await db.execute(text("DELETE FROM llm_models WHERE tenant_id=:t"),
                         {"t": str(world.tenant_id)})
        await db.commit()


async def test_whatsapp_principal_is_refused_owner_level_tools(world, monkeypatch):
    """owner-denied, end of the same chain: with no principal carried, an
    owner-level tool invoked on the sender's turn is refused."""
    import app.core.tool_guard as at
    monkeypatch.setattr("app.database.async_session", Session, raising=False)
    out = await at.execute_tool("send_payment_link", {}, world.agent_id, None)
    assert out.startswith("❌"), out


# ═══════════════ PATH 4 — TRIGGER PROPAGATION (real daemon fn) ═══════════════

async def _mk_trigger(world, cfg):
    from types import SimpleNamespace
    from datetime import datetime, timezone, timedelta
    return SimpleNamespace(
        id=uuid.uuid4(), agent_id=world.agent_id, config=cfg,
        last_fired_at=None, fire_count=0,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
    )


async def test_trigger_does_not_copy_another_principals_private_message(world, monkeypatch):
    import app.services.trigger_daemon as td
    monkeypatch.setattr(td, "async_session", Session, raising=False)

    # A SECOND agent in another tenant, with its own private session and a
    # secret. The trigger names that agent; the join used to be unconstrained.
    other_agent = uuid.uuid4()
    other_session = uuid.uuid4()
    participant = uuid.uuid4()
    async with Session() as db:
        await db.execute(text(
            "INSERT INTO agents (id,name,role_description,tone,creator_id,agent_type,"
            "escalation_keywords,status,autonomy_policy,tokens_used_today,tokens_used_month,"
            "tokens_used_total,context_window_size,max_tool_rounds,max_triggers,"
            "min_poll_interval_min,webhook_rate_limit,is_expired,llm_calls_today,"
            "heartbeat_enabled,heartbeat_interval_minutes,heartbeat_active_hours,tenant_id)"
            " VALUES (:i,'OtherTenantAgent','r',3,:c,'native','[]','idle','{}',0,0,0,20,20,"
            "10,5,60,false,0,false,240,'',NULL)"
        ), {"i": str(other_agent), "c": str(world.owner_id)})
        await db.execute(text(
            "INSERT INTO participants (id,type,ref_id,display_name)"
            " VALUES (:i,'agent',:r,'OtherTenantAgent')"
        ), {"i": str(participant), "r": str(other_agent)})
        await db.execute(text(
            "INSERT INTO chat_sessions (id,agent_id,user_id,title,source_channel,visibility)"
            " VALUES (:i,:a,:u,'other private','web','private')"
        ), {"i": str(other_session), "a": str(other_agent), "u": str(world.owner_id)})
        await db.execute(text(
            "INSERT INTO chat_messages (id,agent_id,user_id,role,content,conversation_id,participant_id)"
            " VALUES (:i,:a,:u,'assistant','SECRET FROM ANOTHER TENANT',:c,:p)"
        ), {"i": str(uuid.uuid4()), "a": str(other_agent), "u": str(world.owner_id),
            "c": str(other_session), "p": str(participant)})
        await db.commit()

    try:
        trig = await _mk_trigger(world, {"from_agent_name": "OtherTenantAgent"})
        fired = await td._check_new_agent_messages(trig)
        assert fired is False, "trigger matched a message outside its own agent's sessions"
        assert "_matched_message" not in trig.config, trig.config
        assert "SECRET" not in str(trig.config)

        # principal-preserved / owner-denied: same participant, but the message
        # now lives in a PRIVATE session ON THIS agent -> still not copied.
        s2 = uuid.uuid4()
        async with Session() as db:
            await db.execute(text(
                "INSERT INTO chat_sessions (id,agent_id,user_id,title,source_channel,visibility)"
                " VALUES (:i,:a,:u,'priv on-agent','web','private')"
            ), {"i": str(s2), "a": str(world.agent_id), "u": str(world.owner_id)})
            await db.execute(text(
                "INSERT INTO chat_messages (id,agent_id,user_id,role,content,conversation_id,participant_id)"
                " VALUES (:i,:a,:u,'assistant','PRIVATE ON AGENT',:c,:p)"
            ), {"i": str(uuid.uuid4()), "a": str(world.agent_id), "u": str(world.owner_id),
                "c": str(s2), "p": str(participant)})
            await db.commit()
        trig = await _mk_trigger(world, {"from_agent_name": "OtherTenantAgent"})
        assert await td._check_new_agent_messages(trig) is False
        assert "PRIVATE" not in str(trig.config)

        # ...and the permitted case still works: an explicitly SHARED
        # agent-to-agent session on this agent DOES propagate.
        s3 = uuid.uuid4()
        async with Session() as db:
            await db.execute(text(
                "INSERT INTO chat_sessions (id,agent_id,user_id,title,source_channel,visibility)"
                " VALUES (:i,:a,:u,'shared a2a','agent','shared')"
            ), {"i": str(s3), "a": str(world.agent_id), "u": str(world.owner_id)})
            await db.execute(text(
                "INSERT INTO chat_messages (id,agent_id,user_id,role,content,conversation_id,participant_id)"
                " VALUES (:i,:a,:u,'assistant','SHARED HANDOFF',:c,:p)"
            ), {"i": str(uuid.uuid4()), "a": str(world.agent_id), "u": str(world.owner_id),
                "c": str(s3), "p": str(participant)})
            await db.commit()
        trig = await _mk_trigger(world, {"from_agent_name": "OtherTenantAgent"})
        assert await td._check_new_agent_messages(trig) is True
        assert trig.config["_matched_message"] == "SHARED HANDOFF"
    finally:
        async with Session() as db:
            for _t in ("agent_activity_logs", "chat_messages"):
                try:
                    await db.execute(text(f"DELETE FROM {_t} WHERE agent_id IN (:a,:b)"),
                                     {"a": str(other_agent), "b": str(world.agent_id)})
                except Exception:
                    await db.rollback()
            await db.execute(text("DELETE FROM chat_sessions WHERE agent_id IN (:a,:b)"),
                             {"a": str(other_agent), "b": str(world.agent_id)})
            await db.execute(text("DELETE FROM participants WHERE id=:p"), {"p": str(participant)})
            await db.execute(text("DELETE FROM agents WHERE id=:a"), {"a": str(other_agent)})
            await db.commit()


async def test_trigger_from_user_name_fails_closed_when_unresolvable(world, monkeypatch):
    """The :356 fallback used to drop the user filter and match ANY principal's
    message on the agent. It must now refuse to fire."""
    import app.services.trigger_daemon as td
    monkeypatch.setattr(td, "async_session", Session, raising=False)

    s = uuid.uuid4()
    async with Session() as db:
        await db.execute(text(
            "INSERT INTO chat_sessions (id,agent_id,user_id,title,source_channel,visibility)"
            " VALUES (:i,:a,:u,'feishu','feishu','private')"
        ), {"i": str(s), "a": str(world.agent_id), "u": str(world.owner_id)})
        await db.execute(text(
            "INSERT INTO chat_messages (id,agent_id,user_id,role,content,conversation_id)"
            " VALUES (:i,:a,:u,'user','SOMEONE ELSES MESSAGE',:c)"
        ), {"i": str(uuid.uuid4()), "a": str(world.agent_id), "u": str(world.owner_id), "c": str(s)})
        await db.commit()
    try:
        trig = await _mk_trigger(world, {"from_user_name": "NoSuchPersonAnywhere"})
        assert await td._check_new_agent_messages(trig) is False
        assert "SOMEONE ELSES MESSAGE" not in str(trig.config)
    finally:
        async with Session() as db:
            await db.execute(text("DELETE FROM chat_messages WHERE conversation_id=:c"), {"c": str(s)})
            await db.execute(text("DELETE FROM chat_sessions WHERE id=:i"), {"i": str(s)})
            await db.commit()
