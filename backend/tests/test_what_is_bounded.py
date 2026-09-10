"""What the meter actually bounds — stated as an executable claim.

A call limit is not a spending limit. These tests pin down which of the
three quantities (calls / tokens / money) the repair constrains, so nobody
reads "enforced usage limits" as "spend is capped".
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

TEST_DB = os.environ.get("ISOLA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="ISOLA_TEST_DATABASE_URL not set")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB

from sqlalchemy import text                                             # noqa: E402
from sqlalchemy.pool import NullPool                                    # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402


def _sf():
    return async_sessionmaker(create_async_engine(TEST_DB, poolclass=NullPool),
                              expire_on_commit=False)


@pytest.fixture
async def ctx():
    S = _sf()
    tid, uid, aid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with S() as db:
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
            "INSERT INTO agents (id,name,role_description,tone,creator_id,agent_type,"
            "escalation_keywords,status,autonomy_policy,tokens_used_today,tokens_used_month,"
            "tokens_used_total,context_window_size,max_tool_rounds,max_triggers,"
            "min_poll_interval_min,webhook_rate_limit,is_expired,llm_calls_today,"
            "heartbeat_enabled,heartbeat_interval_minutes,heartbeat_active_hours,tenant_id,"
            "max_llm_calls_per_day,llm_calls_reset_at) VALUES "
            "(:i,'B','r',3,:c,'native','[]','idle','{}',0,0,0,20,20,10,5,60,false,0,false,240,"
            "'',:t,3,now())"), {"i": str(aid), "c": str(uid), "t": str(tid)})
        await db.commit()
    yield {"S": S, "agent_id": aid, "tenant_id": tid, "user_id": uid}
    async with S() as db:
        await db.execute(text("DELETE FROM llm_call_telemetry WHERE agent_id=:a"), {"a": str(aid)})
        await db.execute(text("DELETE FROM agents WHERE id=:a"), {"a": str(aid)})
        await db.execute(text("DELETE FROM users WHERE id=:u"), {"u": str(uid)})
        await db.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": str(tid)})
        await db.commit()


async def test_the_bound_is_CALLS_per_agent_per_utc_day(ctx):
    from app.services.usage_meter import UsageLimitExceeded, reserve_llm_call
    S = ctx["S"]
    for _ in range(3):
        async with S() as db:
            await reserve_llm_call(ctx["agent_id"], db)
    async with S() as db:
        with pytest.raises(UsageLimitExceeded) as e:
            await reserve_llm_call(ctx["agent_id"], db)
    assert e.value.limit_kind == "llm_calls", e.value.limit_kind
    assert "llm_calls limit (3/3)" in e.value.message


async def test_TOKENS_are_metered_but_NOT_bounded(ctx):
    """Each admitted call may carry an arbitrary number of tokens. Three
    calls of 1,000 tokens and three calls of 1,000,000 tokens are identical
    to this mechanism."""
    from app.services.usage_meter import record_llm_call, reserve_llm_call, spend_since
    S = ctx["S"]
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with S() as db:
        await reserve_llm_call(ctx["agent_id"], db)
    async with S() as db:
        await record_llm_call(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                              provider="deepseek", model="deepseek-chat",
                              input_tokens=5_000_000, output_tokens=2_000_000)
    async with S() as db:
        # the enormous call consumed exactly one unit of budget
        used = (await db.execute(text("SELECT llm_calls_today FROM agents WHERE id=:a"),
                                 {"a": str(ctx["agent_id"])})).scalar_one()
    assert used == 1, "a 7M-token call cost the same single unit as a 10-token one"

    async with S() as db:
        rep = await spend_since(db, since, ctx["agent_id"])
    assert rep["input_tokens"] == 5_000_000
    # tokens are RECORDED. Nothing refuses a call for being large.


async def test_MONEY_is_reportable_but_NOT_bounded_and_DB_prices_are_absent(ctx):
    """Cost becomes computable only via the module's fallback price book,
    because llm_models.cost_per_1k_* is NULL estate-wide. Nothing enforces a
    money ceiling anywhere."""
    from app.services.usage_meter import (FALLBACK_PRICE_BOOK, compute_cost_cents,
                                          record_llm_call, spend_since)
    S = ctx["S"]
    since = datetime.now(timezone.utc) - timedelta(minutes=1)

    # priced model -> a figure
    c = compute_cost_cents(1000, 1000, "deepseek", "deepseek-chat", None, None)
    assert c is not None and c > 0

    # UNPRICED model -> None, never 0. "unknown" must not read as "free".
    assert compute_cost_cents(1000, 1000, "some-new-provider", "some-new-model",
                              None, None) is None

    async with S() as db:
        await record_llm_call(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                              provider="some-new-provider", model="some-new-model",
                              input_tokens=1000, output_tokens=1000)
    async with S() as db:
        rep = await spend_since(db, since, ctx["agent_id"])
    assert rep["unpriced_calls"] >= 1, \
        "an unpriced call must be visible as unpriced, not silently costed at zero"

    # There is no money ceiling column anywhere on agents.
    async with S() as db:
        cols = [r[0] for r in (await db.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='agents'"
        ))).all()]
    assert not [c for c in cols if "cost" in c or "spend" in c or "budget" in c], \
        f"unexpected money column appeared: {cols}"


async def test_no_estate_wide_or_tenant_wide_ceiling_exists(ctx):
    """The cap is PER AGENT. 444 agents at 100 calls/day is 44,400 calls/day
    with nothing above it to say stop."""
    S = ctx["S"]
    async with S() as db:
        cols = [r[0] for r in (await db.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='tenants'"
        ))).all()]
    # tenants carry a DEFAULT for new agents, not a ceiling on the tenant.
    assert "default_max_llm_calls_per_day" in cols
    assert not [c for c in cols if c.startswith("max_llm_calls")], \
        "a tenant-level ceiling exists; update the claim"
