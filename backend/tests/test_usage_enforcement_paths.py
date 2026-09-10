"""Real-path enforcement proofs for the usage meter.

The defect being repaired is NOT "the arithmetic is wrong". It is
"the predicate is never reached": check_agent_llm_quota() was imported at
websocket.py:355 and called nowhere, and heartbeat.py never imported
quota_guard at all while calling client.complete() in range(20).

So a passing arithmetic test proves nothing. These tests assert the only
thing that matters: **the provider is not called when there is no budget**,
on each of the three paths — human chat, the heartbeat wake, and a retry —
and they assert it by counting provider invocations.

Requires ISOLA_TEST_DATABASE_URL (a throwaway PostgreSQL at head).
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

TEST_DB = os.environ.get("ISOLA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="ISOLA_TEST_DATABASE_URL not set")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB

from sqlalchemy import text                                             # noqa: E402
from sqlalchemy.pool import NullPool                                    # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

Session = None


def _new_session_factory():
    return async_sessionmaker(create_async_engine(TEST_DB, poolclass=NullPool),
                              expire_on_commit=False)


class FakeResponse:
    def __init__(self):
        self.content = "hi"
        self.tool_calls = None
        self.reasoning_content = None
        self.usage = {"prompt_tokens": 10, "completion_tokens": 5}


class CountingClient:
    """Stands in for a real provider client. Counts every reach-through."""

    def __init__(self, fail_with=None):
        self.calls = 0
        self.fail_with = fail_with

    async def complete(self, *a, **kw):
        return self._reach()

    async def stream(self, *a, **kw):
        # the human-chat tool loop uses stream(), not complete()
        return self._reach()

    def _reach(self):
        self.calls += 1
        if self.fail_with:
            raise self.fail_with
        return FakeResponse()

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _loop_safe_db(monkeypatch):
    """app.database.async_session is bound to an engine created at import
    time in another event loop. Redirect every lazy lookup of it."""
    if TEST_DB:
        f = _new_session_factory()
        monkeypatch.setattr("app.database.async_session", f, raising=False)


@pytest.fixture
async def agentrow():
    global Session
    Session = _new_session_factory()
    tid, uid, aid, mid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with Session() as db:
        await db.execute(text(
            "INSERT INTO tenants (id,name,slug,im_provider,is_active,default_message_limit,"
            "default_message_period,default_max_agents,default_agent_ttl_hours,"
            "default_max_llm_calls_per_day,min_heartbeat_interval_minutes,timezone,sso_enabled,"
            "default_max_triggers,min_poll_interval_floor,max_webhook_rate_ceiling,"
            "a2a_async_enabled,runtime_mode) VALUES "
            "(:i,'T','t-'||:s,'web_only',true,50,'month',5,24,100,60,'UTC',false,10,5,60,false,'native')"
        ), {"i": str(tid), "s": str(tid)[:8]})
        await db.execute(text(
            "INSERT INTO users (id,display_name,role,is_active,quota_message_limit,"
            "quota_message_period,quota_messages_used,quota_max_agents,quota_agent_ttl_hours,tenant_id)"
            " VALUES (:i,'U','member',true,50,'month',0,5,24,:t)"), {"i": str(uid), "t": str(tid)})
        await db.execute(text(
            "INSERT INTO llm_models (id,provider,model,api_key_encrypted,label,enabled,"
            "supports_vision,tenant_id) VALUES (:i,'deepseek','deepseek-chat','x','m',true,false,:t)"
        ), {"i": str(mid), "t": str(tid)})
        await db.execute(text(
            "INSERT INTO agents (id,name,role_description,tone,creator_id,agent_type,"
            "escalation_keywords,status,autonomy_policy,tokens_used_today,tokens_used_month,"
            "tokens_used_total,context_window_size,max_tool_rounds,max_triggers,"
            "min_poll_interval_min,webhook_rate_limit,is_expired,llm_calls_today,"
            "heartbeat_enabled,heartbeat_interval_minutes,heartbeat_active_hours,tenant_id,"
            "max_llm_calls_per_day,primary_model_id,llm_calls_reset_at) VALUES "
            "(:i,'MeterAgent','r',3,:c,'native','[]','idle','{}',0,0,0,20,20,10,5,60,false,0,"
            "false,240,'',:t,2,:m,now())"
        ), {"i": str(aid), "c": str(uid), "t": str(tid), "m": str(mid)})
        await db.commit()
    yield {"agent_id": aid, "user_id": uid, "tenant_id": tid, "model_id": mid}
    async with Session() as db:
        for t_ in ("llm_call_telemetry", "agent_activity_logs", "daily_token_usage",
                   "chat_messages", "chat_sessions"):
            try:
                await db.execute(text(f"DELETE FROM {t_} WHERE agent_id=:a"), {"a": str(aid)})
            except Exception:
                await db.rollback()
        await db.execute(text("DELETE FROM agents WHERE id=:a"), {"a": str(aid)})
        await db.execute(text("DELETE FROM llm_models WHERE id=:m"), {"m": str(mid)})
        await db.execute(text("DELETE FROM users WHERE id=:u"), {"u": str(uid)})
        await db.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": str(tid)})
        await db.commit()


async def _set_used(aid, used):
    async with Session() as db:
        await db.execute(text(
            "UPDATE agents SET llm_calls_today=:u, llm_calls_reset_at=now() WHERE id=:a"),
            {"u": used, "a": str(aid)})
        await db.commit()


async def _used(aid):
    async with Session() as db:
        return (await db.execute(text("SELECT llm_calls_today FROM agents WHERE id=:a"),
                                 {"a": str(aid)})).scalar_one()


# ═════════════ PATH 1 — HUMAN CHAT (websocket -> call_llm) ═════════════

async def test_human_chat_reaches_enforcement_before_the_provider(agentrow, monkeypatch):
    """app/services/llm/caller.py::call_llm is the chokepoint the websocket
    handler funnels into. Enforcement is wired at client creation, so the
    reservation happens before the first complete()."""
    import app.services.llm.caller as caller
    from sqlalchemy import select
    from app.models.llm import LLMModel

    client = CountingClient()
    monkeypatch.setattr(caller, "create_llm_client", lambda **kw: client)

    async with Session() as db:
        model = (await db.execute(select(LLMModel).where(
            LLMModel.id == agentrow["model_id"]))).scalar_one()

    # cap is 2. Below the cap the provider IS reached.
    await _set_used(agentrow["agent_id"], 0)
    out = await caller.call_llm(model, [{"role": "user", "content": "hi"}], "A", "r",
                                agent_id=agentrow["agent_id"], user_id=agentrow["user_id"])
    assert client.calls == 1, out
    assert await _used(agentrow["agent_id"]) == 1

    # AT the cap the provider is NOT reached.
    await _set_used(agentrow["agent_id"], 2)
    before = client.calls
    out = await caller.call_llm(model, [{"role": "user", "content": "hi"}], "A", "r",
                                agent_id=agentrow["agent_id"], user_id=agentrow["user_id"])
    assert client.calls == before, "provider was called with no budget"
    assert "daily llm_calls limit" in out, out
    # refusal did not leak the counter
    assert await _used(agentrow["agent_id"]) == 2


# ═════════════ PATH 2 — RETRY / FAILOVER ═════════════

async def test_retry_attempts_each_reach_enforcement(agentrow, monkeypatch):
    """call_llm_with_failover retries the fallback model by calling call_llm a
    second time, which creates a second metered client. Each attempt must
    reserve; the budget must not be bypassed by the retry."""
    import app.services.llm.caller as caller
    from app.services.llm.utils import LLMError
    from sqlalchemy import select
    from app.models.llm import LLMModel

    clients = []

    def _mk(**kw):
        c = CountingClient(fail_with=LLMError("503 upstream unavailable"))
        clients.append(c)
        return c

    monkeypatch.setattr(caller, "create_llm_client", _mk)

    async with Session() as db:
        model = (await db.execute(select(LLMModel).where(
            LLMModel.id == agentrow["model_id"]))).scalar_one()

    # cap 2, used 0: primary attempt reserves 1, failover attempt reserves 1.
    await _set_used(agentrow["agent_id"], 0)
    await caller.call_llm_with_failover(model, model, [{"role": "user", "content": "x"}],
                                        "A", "r", agent_id=agentrow["agent_id"],
                                        user_id=agentrow["user_id"])
    assert await _used(agentrow["agent_id"]) == 2, "a retry escaped the budget"
    total = sum(c.calls for c in clients)
    assert total == 2, f"expected 2 provider attempts, saw {total}"

    # now at the cap: a further attempt reaches enforcement, not the provider
    n_before = sum(c.calls for c in clients)
    await caller.call_llm_with_failover(model, model, [{"role": "user", "content": "x"}],
                                        "A", "r", agent_id=agentrow["agent_id"],
                                        user_id=agentrow["user_id"])
    assert sum(c.calls for c in clients) == n_before, "retry called the provider with no budget"
    assert await _used(agentrow["agent_id"]) == 2


# ═════════════ PATH 3 — HEARTBEAT (the volume path) ═════════════

async def test_heartbeat_reaches_enforcement_before_the_provider(agentrow, monkeypatch):
    """_execute_heartbeat() is the path that never imported quota_guard at all.
    278 agents x up to 20 rounds. With no budget, zero provider calls."""
    import app.services.heartbeat as hb
    import app.services.llm as llm_pkg

    client = CountingClient()
    monkeypatch.setattr(llm_pkg, "create_llm_client", lambda **kw: client)
    monkeypatch.setattr(hb, "async_session", Session, raising=False)
    monkeypatch.setattr("app.database.async_session", Session, raising=False)

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(hb, "get_agent_tools_for_llm", _noop, raising=False)

    # AT the cap: the 20-round loop must make zero provider calls.
    await _set_used(agentrow["agent_id"], 2)
    await hb._execute_heartbeat(agentrow["agent_id"])
    assert client.calls == 0, (
        f"heartbeat made {client.calls} provider call(s) with no budget "
        "-- this is the exact bypass being repaired"
    )
    assert await _used(agentrow["agent_id"]) == 2

    # WITH budget: the loop runs, and every round consumes exactly one unit.
    await _set_used(agentrow["agent_id"], 0)
    await hb._execute_heartbeat(agentrow["agent_id"])
    assert client.calls >= 1, "heartbeat made no provider call even with budget"
    used = await _used(agentrow["agent_id"])
    assert used == client.calls, f"counter {used} != provider calls {client.calls}"
    assert used <= 2, f"heartbeat exceeded its cap of 2 ({used})"


# ═════════════ POSTGRES CONCURRENCY (real row locking) ═════════════

async def test_concurrent_reservations_never_over_grant_on_postgres(agentrow):
    """SQLite serialises writers; it cannot establish production locking.
    This runs on real PostgreSQL with independent connections."""
    from app.services.usage_meter import UsageLimitExceeded, reserve_llm_call

    CAP, CONTENDERS = 5, 40
    async with Session() as db:
        await db.execute(text(
            "UPDATE agents SET max_llm_calls_per_day=:c, llm_calls_today=0,"
            " llm_calls_reset_at=now() WHERE id=:a"),
            {"c": CAP, "a": str(agentrow["agent_id"])})
        await db.commit()

    granted, refused = 0, 0

    async def one():
        async with Session() as db:          # its own connection
            try:
                await reserve_llm_call(agentrow["agent_id"], db)
                return True
            except UsageLimitExceeded:
                return False

    results = await asyncio.gather(*[one() for _ in range(CONTENDERS)])
    granted = sum(1 for r in results if r)
    refused = len(results) - granted

    assert granted == CAP, f"over/under-granted: {granted} of {CAP}"
    assert refused == CONTENDERS - CAP
    assert await _used(agentrow["agent_id"]) == CAP, "counter drifted from grants"


async def test_postgres_rollover_is_atomic_under_contention(agentrow):
    """All contenders arrive on a stale reset stamp at once. Exactly one
    rollover may occur; the cap must still bind afterwards."""
    from app.services.usage_meter import UsageLimitExceeded, reserve_llm_call

    CAP = 3
    async with Session() as db:
        await db.execute(text(
            "UPDATE agents SET max_llm_calls_per_day=:c, llm_calls_today=99,"
            " llm_calls_reset_at=now() - interval '2 days' WHERE id=:a"),
            {"c": CAP, "a": str(agentrow["agent_id"])})
        await db.commit()

    async def one():
        async with Session() as db:
            try:
                await reserve_llm_call(agentrow["agent_id"], db)
                return True
            except UsageLimitExceeded:
                return False

    results = await asyncio.gather(*[one() for _ in range(20)])
    assert sum(1 for r in results if r) == CAP, results.count(True)
    assert await _used(agentrow["agent_id"]) == CAP


async def test_heartbeat_writes_cost_telemetry(agentrow, monkeypatch):
    """llm_call_telemetry last took a row on 2026-07-09 because nothing wrote
    to it. Prove the restored writer actually lands rows on Postgres."""
    import app.services.heartbeat as hb
    import app.services.llm as llm_pkg

    client = CountingClient()
    monkeypatch.setattr(llm_pkg, "create_llm_client", lambda **kw: client)
    monkeypatch.setattr(hb, "async_session", Session, raising=False)

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(hb, "get_agent_tools_for_llm", _noop, raising=False)

    await _set_used(agentrow["agent_id"], 0)
    await hb._execute_heartbeat(agentrow["agent_id"])

    async with Session() as db:
        rows = (await db.execute(text(
            "SELECT count(*), sum(input_tokens), sum(output_tokens), count(cost_cents)"
            "  FROM llm_call_telemetry WHERE agent_id=:a"), {"a": str(agentrow["agent_id"])})).first()
    assert rows[0] == client.calls, f"telemetry rows {rows[0]} != provider calls {client.calls}"
    assert rows[1] and rows[1] > 0, "input tokens not recorded"
    assert rows[3] == rows[0], "cost_cents missing on a priced model"
