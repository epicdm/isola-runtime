"""#121 — Synthetic WA webhook e2e harness for the Tier 1.5a owner-ask loop.

Tests the FULL chain that today's unit tests can't reach:

  signed Meta webhook (POST /api/channel/whatsapp/{agent_id}/webhook)
    → background _process_whatsapp_message
    → _try_handle_owner_reply branch (the new code from #109)
    → OwnerAskInFlight row updated
    → record_teach() writes knowledge.md
    → mock Meta send_text_message twice (customer + owner ack)

This is the L3 layer of the wa-test-ladder (#124): full chain against a
real backend + real postgres + mocked Meta Graph. Each test exercises one
scenario:

  test_phase_owner_answer_path:
    seed an OwnerAskInFlight row, then send an "owner replied with answer"
    webhook. Assert the row flips to 'answered', knowledge.md gains a
    section, customer + owner both got Meta send_message calls.

  test_phase_owner_skip_path:
    seed a row, send an owner reply that says "skip". Assert the row
    flips to 'skipped', knowledge.md untouched, only one outbound (the
    skip ack to owner — no customer message).

  test_phase_marker_dispatcher_e2e:
    construct a ReplyContext, call parse_and_dispatch with a reply that
    contains '[ask_owner: ...]'. Assert the row was inserted + outbound
    to owner happened. This tests the OTHER half of the loop (customer
    side) without needing the LLM in the hot path.

Run from inside the backend container:
    docker exec isolaruntime-backend-1 python -m pytest \
        tests/test_owner_ask_e2e.py -v --no-header

Container env requirement: WHATSAPP_GRAPH_BASE_URL=http://localhost:8901/v21.0
so outbound Meta calls land on the in-process mock instead of Facebook.
docker-compose.override.yml already sets this for `backend` per Phase B.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets as _secrets
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Touch the model modules so SQLAlchemy registers them on Base.metadata
# (mirrors test_phase_b.py's import-side-effects pattern).
from app.models import agent as _agent  # noqa: F401, E402
from app.models import audit as _audit  # noqa: F401, E402
from app.models import chat_session as _chat_session  # noqa: F401, E402
from app.models import llm as _llm  # noqa: F401, E402
from app.models import owner_ask as _owner_ask  # noqa: F401, E402
from app.models import participant as _participant  # noqa: F401, E402


BACKEND = "http://localhost:8000"
MOCK_META_PORT = 8901


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
async def mock_meta():
    """Mock Meta Cloud API on localhost:8901. Records every outbound call.

    Backend's WHATSAPP_GRAPH_BASE_URL points here per docker-compose.override.yml.
    """
    records: list[dict] = []

    async def messages(request):
        phone_number_id = request.match_info["phone_number_id"]
        body = await request.json()
        records.append({
            "op": "send_message",
            "phone_number_id": phone_number_id,
            "body": body,
        })
        return web.json_response({
            "messaging_product": "whatsapp",
            "contacts": [{"input": body.get("to"), "wa_id": body.get("to")}],
            "messages": [{"id": f"wamid.mock-{len(records)}"}],
        })

    app = web.Application()
    app.router.add_post("/v21.0/{phone_number_id}/messages", messages)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", MOCK_META_PORT)
    await site.start()
    try:
        yield records
    finally:
        await runner.cleanup()


@pytest.fixture
async def http():
    async with httpx.AsyncClient(base_url=BACKEND, timeout=30.0, follow_redirects=True) as client:
        yield client


@pytest.fixture
async def db_session():
    """Direct SQLAlchemy session for read-back assertions."""
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://isolaruntime:isolaruntime@postgres:5432/isolaruntime",
    )
    engine = create_async_engine(dsn, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield Session
    finally:
        await engine.dispose()


@pytest.fixture
def fake_creds():
    return {
        "phone_number_id": "mock-phone-number-id",
        "waba_id": "mock-waba-id",
        "access_token": "mock-access-token",
        "verify_token": "mock-verify-token-2026",
        "app_secret": "mock-owner-ask-secret-2026",
    }


@pytest.fixture
async def configured_agent(http, fake_creds):
    """Register a fresh user, create an agent with an owner_phone set,
    and configure the WhatsApp channel. Returns enough state for the
    owner-ask tests to drive."""
    suffix = _secrets.token_hex(4)
    email = f"test-oae-{suffix}@isola.dev"
    password = "TestOwnerAskE2E-2026!"
    owner_phone = "17672958382"  # mirrors Eric's real escalation contact

    r = await http.post("/api/auth/register", json={
        "email": email,
        "username": f"test-oae-{suffix}",
        "password": password,
        "display_name": f"Owner-Ask E2E {suffix}",
        "tenant_name": f"OAE Tenant {suffix}",
        "tenant_slug": f"oae-tenant-{suffix}",
    })
    assert r.status_code in (200, 201), f"register: {r.status_code} {r.text}"
    data = r.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    user_id = data["user_id"]

    r = await http.post("/api/agents", headers=headers, json={
        "name": f"OAE Agent {suffix}",
        "role_description": "owner-ask e2e test agent",
    })
    assert r.status_code in (200, 201), f"agent create: {r.status_code} {r.text}"
    agent_id = r.json()["id"]

    # Set owner_phone via the internal ensure-agent endpoint so the
    # webhook's _try_handle_owner_reply has a phone to match against.
    internal_secret = os.environ.get("ISOLA_INTERNAL_SECRET", "")
    if internal_secret:
        # First, look up the runtime tenant_id we'd need. Easier path:
        # update the column directly via a session.
        pass

    # Channel config
    r = await http.post(
        f"/api/agents/{agent_id}/whatsapp-channel",
        headers=headers,
        json=fake_creds,
    )
    assert r.status_code == 201, f"channel config: {r.status_code} {r.text}"

    # Set owner_phone directly via SQL (no exposed PATCH endpoint for it
    # in apps/isola-runtime today).
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://isolaruntime:isolaruntime@postgres:5432/isolaruntime",
    )
    engine = create_async_engine(dsn, echo=False)
    async with engine.begin() as conn:
        from sqlalchemy import text as _sql
        await conn.execute(
            _sql("UPDATE agents SET owner_phone = :p WHERE id = :id"),
            {"p": owner_phone, "id": agent_id},
        )
    await engine.dispose()

    yield {
        "agent_id": agent_id,
        "user_id": user_id,
        "headers": headers,
        "owner_phone": owner_phone,
        "suffix": suffix,
    }


def _sign(body_bytes: bytes, app_secret: str) -> str:
    digest = hmac.new(app_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _wa_text_payload(*, phone_number_id: str, waba_id: str, from_phone: str, text: str, msg_id: str) -> bytes:
    """Build a Meta-shaped 'text' inbound webhook payload."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": waba_id,
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "17678180000",
                        "phone_number_id": phone_number_id,
                    },
                    "contacts": [{
                        "profile": {"name": "OAE Test"},
                        "wa_id": from_phone,
                    }],
                    "messages": [{
                        "from": from_phone,
                        "id": msg_id,
                        "timestamp": str(int(time.time())),
                        "type": "text",
                        "text": {"body": text},
                    }],
                },
                "field": "messages",
            }],
        }],
    }
    return json.dumps(payload).encode("utf-8")


async def _seed_pending_ask(
    db_session_factory,
    *,
    agent_id: str,
    customer_phone: str,
    owner_phone: str,
    question: str,
) -> uuid.UUID:
    """Insert an OwnerAskInFlight row directly so the owner-reply test
    has something to match against. Returns the inserted row id."""
    from app.models.owner_ask import OwnerAskInFlight, STATUS_AWAITING

    row_id = uuid.uuid4()
    async with db_session_factory() as db:
        row = OwnerAskInFlight(
            id=row_id,
            agent_id=uuid.UUID(agent_id),
            customer_phone=customer_phone,
            customer_name="OAE Customer",
            customer_conversation_id=None,
            question=question,
            owner_phone=owner_phone,
            status=STATUS_AWAITING,
        )
        db.add(row)
        await db.commit()
    return row_id


def _agent_workspace(agent_id: str) -> Path:
    from app.config import get_settings
    return Path(get_settings().AGENT_DATA_DIR) / agent_id / "workspace"


# ─── Phase chain: owner-ask e2e ────────────────────────────────────────


async def _phase_owner_answer(http, configured_agent, fake_creds, mock_meta, db_session):
    """Owner replies with an answer → /teach fires + customer relay."""
    agent_id = configured_agent["agent_id"]
    owner_phone = configured_agent["owner_phone"]
    customer_phone = "15558881111"
    question = "Do you have valet parking on weekends?"
    answer = "Yes - free valet evenings and weekends"

    row_id = await _seed_pending_ask(
        db_session,
        agent_id=agent_id,
        customer_phone=customer_phone,
        owner_phone=owner_phone,
        question=question,
    )

    body = _wa_text_payload(
        phone_number_id=fake_creds["phone_number_id"],
        waba_id=fake_creds["waba_id"],
        from_phone=owner_phone,
        text=answer,
        msg_id=f"wamid.oae-answer-{configured_agent['suffix']}",
    )
    sig = _sign(body, fake_creds["app_secret"])

    r = await http.post(
        f"/api/channel/whatsapp/{agent_id}/webhook",
        content=body,
        headers={"Content-Type": "application/json", "x-hub-signature-256": sig},
    )
    assert r.status_code == 200, f"webhook: {r.status_code} {r.text}"

    # Wait for the background task to: lookup pending row → call
    # record_teach → mark answered → send 2 outbound (customer + owner).
    deadline = time.monotonic() + 30.0
    saw_customer = saw_owner_ack = False
    while time.monotonic() < deadline:
        for rec in mock_meta:
            if rec.get("op") != "send_message":
                continue
            to = rec.get("body", {}).get("to")
            text = rec.get("body", {}).get("text", {}).get("body", "")
            if to == customer_phone and answer in text:
                saw_customer = True
            if to == owner_phone and "Taught" in text:
                saw_owner_ack = True
        if saw_customer and saw_owner_ack:
            break
        await asyncio.sleep(0.5)
    assert saw_customer, f"customer never got answer; records={mock_meta}"
    assert saw_owner_ack, f"owner never got teach-ack; records={mock_meta}"

    # DB: row flipped to answered + owner_reply persisted
    from app.models.owner_ask import OwnerAskInFlight, STATUS_ANSWERED
    async with db_session() as db:
        row = (
            await db.execute(select(OwnerAskInFlight).where(OwnerAskInFlight.id == row_id))
        ).scalar_one()
        assert row.status == STATUS_ANSWERED, f"status not flipped: {row.status}"
        assert row.owner_reply == answer
        assert row.answered_at is not None

    # File: knowledge.md gained the topic section
    km = _agent_workspace(agent_id) / "knowledge.md"
    assert km.exists(), f"knowledge.md not written at {km}"
    content = km.read_text(encoding="utf-8")
    assert answer in content, f"answer not in knowledge.md: {content[:300]!r}"


async def _phase_owner_skip(http, configured_agent, fake_creds, mock_meta, db_session):
    """Owner replies 'skip' → row marked skipped + ack only, no /teach."""
    agent_id = configured_agent["agent_id"]
    owner_phone = configured_agent["owner_phone"]
    customer_phone = "15559992222"
    question = "Do you do same-day catering for 50+ people?"

    row_id = await _seed_pending_ask(
        db_session,
        agent_id=agent_id,
        customer_phone=customer_phone,
        owner_phone=owner_phone,
        question=question,
    )

    # Snapshot knowledge.md mtime before
    km = _agent_workspace(agent_id) / "knowledge.md"
    pre_mtime = km.stat().st_mtime if km.exists() else None

    body = _wa_text_payload(
        phone_number_id=fake_creds["phone_number_id"],
        waba_id=fake_creds["waba_id"],
        from_phone=owner_phone,
        text="skip",
        msg_id=f"wamid.oae-skip-{configured_agent['suffix']}",
    )
    sig = _sign(body, fake_creds["app_secret"])

    # Snapshot mock_meta length so we can detect what was added by THIS phase
    pre_count = len(mock_meta)

    r = await http.post(
        f"/api/channel/whatsapp/{agent_id}/webhook",
        content=body,
        headers={"Content-Type": "application/json", "x-hub-signature-256": sig},
    )
    assert r.status_code == 200, f"skip webhook: {r.status_code} {r.text}"

    deadline = time.monotonic() + 20.0
    saw_skip_ack = False
    while time.monotonic() < deadline:
        for rec in mock_meta[pre_count:]:
            if rec.get("op") != "send_message":
                continue
            to = rec.get("body", {}).get("to")
            text = rec.get("body", {}).get("text", {}).get("body", "")
            if to == owner_phone and "Skipped" in text:
                saw_skip_ack = True
                break
        if saw_skip_ack:
            break
        await asyncio.sleep(0.5)
    assert saw_skip_ack, f"owner never got skip-ack; new records={mock_meta[pre_count:]}"

    # No customer outbound on skip path.
    customer_msgs = [
        r for r in mock_meta[pre_count:]
        if r.get("op") == "send_message"
        and r.get("body", {}).get("to") == customer_phone
    ]
    assert customer_msgs == [], f"unexpected customer outbound on skip: {customer_msgs}"

    # DB: status flipped to skipped, no owner_reply persisted
    from app.models.owner_ask import OwnerAskInFlight, STATUS_SKIPPED
    async with db_session() as db:
        row = (
            await db.execute(select(OwnerAskInFlight).where(OwnerAskInFlight.id == row_id))
        ).scalar_one()
        assert row.status == STATUS_SKIPPED, f"status not skipped: {row.status}"
        assert row.owner_reply is None

    # knowledge.md untouched (mtime unchanged or still missing)
    if pre_mtime is None:
        assert not km.exists() or km.stat().st_size == 0, "knowledge.md should not be created on skip"
    else:
        assert km.stat().st_mtime == pre_mtime, "knowledge.md was modified on skip"


async def _phase_ask_owner_marker_dispatch(http, configured_agent, fake_creds, mock_meta, db_session):
    """LLM-side: exercise the [ask_owner: ...] marker dispatcher directly.

    Bypasses the LLM (no model is configured in test) — calls
    parse_and_dispatch with a synthetic ReplyContext + reply text.
    Asserts an OwnerAskInFlight row was created + owner got the ping.
    """
    from app.services.reply_markers import ReplyContext, parse_and_dispatch

    agent_id = configured_agent["agent_id"]
    owner_phone = configured_agent["owner_phone"]
    customer_phone = "15557773333"
    question = "Do you accept Amex?"
    pre_count = len(mock_meta)

    ctx = ReplyContext(
        agent_id=uuid.UUID(agent_id),
        agent_name="OAE Agent",
        owner_phone=owner_phone,
        phone_number_id=fake_creds["phone_number_id"],
        access_token=fake_creds["access_token"],
        customer_phone=customer_phone,
        customer_name="Marie",
        conversation_id=None,
        paperclip_issue_id=None,
        last_inbound=question,
    )
    raw = f"Give me a moment, checking with Eric. [ask_owner: {question}]"
    cleaned = await parse_and_dispatch(raw, ctx)
    assert "ask_owner" not in cleaned, "marker not stripped"
    assert "Give me a moment" in cleaned, "user-facing prefix lost"

    # Assert an OwnerAskInFlight row was created for this agent.
    from app.models.owner_ask import OwnerAskInFlight, STATUS_AWAITING
    async with db_session() as db:
        rows = (
            await db.execute(
                select(OwnerAskInFlight).where(
                    OwnerAskInFlight.agent_id == uuid.UUID(agent_id),
                    OwnerAskInFlight.customer_phone == customer_phone,
                    OwnerAskInFlight.status == STATUS_AWAITING,
                )
            )
        ).scalars().all()
        assert len(rows) == 1, f"expected 1 awaiting row, got {len(rows)}"
        assert rows[0].question == question

    # Assert owner got a Meta send_message
    deadline = time.monotonic() + 5.0
    saw_owner_ping = False
    while time.monotonic() < deadline:
        for rec in mock_meta[pre_count:]:
            if (
                rec.get("op") == "send_message"
                and rec.get("body", {}).get("to") == owner_phone
                and question in rec.get("body", {}).get("text", {}).get("body", "")
            ):
                saw_owner_ping = True
                break
        if saw_owner_ping:
            break
        await asyncio.sleep(0.2)
    assert saw_owner_ping, f"owner ping not sent; new records={mock_meta[pre_count:]}"


# ─── Single chain test (one event-loop, fixtures stay alive) ─────────


@pytest.mark.asyncio
async def test_owner_ask_chain(http, configured_agent, fake_creds, mock_meta, db_session):
    """Run the three phases in sequence. Each builds on backend state from
    the previous (channel config persists; new pending rows don't collide
    because phases use distinct customer phones)."""
    await _phase_owner_answer(http, configured_agent, fake_creds, mock_meta, db_session)
    await _phase_owner_skip(http, configured_agent, fake_creds, mock_meta, db_session)
    await _phase_ask_owner_marker_dispatch(
        http, configured_agent, fake_creds, mock_meta, db_session,
    )
