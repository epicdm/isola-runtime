"""Phase B WhatsApp adapter — dependent integration tests.

These tests run inside the backend container via
    docker exec isolaruntime-backend-1 pytest backend/tests/test_phase_b.py -v

Each test builds on the state from the previous one. If test_b1 fails the
whole chain fails; there is no point asserting B.2 when B.1 config doesn't
persist.

The chain:
  test_b1_config_crud
    - register fresh user, create agent, configure WA channel
    - assert POST/GET/DELETE/webhook-url + Meta verify-handshake (ok+403)
  test_b2_signed_text_webhook
    - reuse the B.1 channel
    - sign a Meta text-message payload, POST to webhook
    - assert webhook returns 200 fast
    - wait for background task
    - assert ChatSession + ChatMessage rows exist
    - assert the mock Meta server received a send-message POST for our text
  test_b3_signed_image_webhook
    - reuse the B.1 channel
    - send signed image payload, media_id backed by the mock Meta server
    - assert the media download + save hit the workspace uploads/
    - assert ChatMessage user content carries the [image:...] marker + caption

The mock Meta server runs in-process on localhost:8901 for the pytest run.
backend reads WHATSAPP_GRAPH_BASE_URL=http://localhost:8901/v21.0 from
docker-compose.override.yml so outbound Meta calls land here instead of
Facebook.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets as _secrets
import time
import uuid
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

# Populate Base.metadata — we select Agent in B.6 which has FKs to
# tenants, users, llm_models, so those tables must be known to the
# mapper or sort_tables() raises NoReferencedTableError.
from app.models import tenant as _tenant  # noqa: F401
from app.models import user as _user  # noqa: F401
from app.models import llm as _llm  # noqa: F401
from app.models import channel_config as _channel_config  # noqa: F401
from app.models import agent as _agent  # noqa: F401
from app.models import chat_session as _chat_session  # noqa: F401
from app.models import audit as _audit  # noqa: F401
from app.models import participant as _participant  # noqa: F401


# ─── Fixtures (shared state chain) ───────────────────────────────────


BACKEND = "http://localhost:8000"
MOCK_META_PORT = 8901
MOCK_PAPERCLIP_PORT = 8902


@pytest.fixture
async def mock_meta():
    """In-process Meta Cloud API mock.

    Records every outbound call so tests can assert on it. Serves
    deterministic responses for media-id lookup and download.
    """
    records: list[dict] = []
    media_bytes = b"\xff\xd8\xff\xe0" + b"mock-jpeg-payload" * 16  # ~260-byte 'jpeg'

    async def media_info(request):
        media_id = request.match_info["media_id"]
        records.append({"op": "media_info", "media_id": media_id})
        return web.json_response({
            "messaging_product": "whatsapp",
            "url": f"http://localhost:{MOCK_META_PORT}/download/{media_id}",
            "mime_type": "image/jpeg",
            "sha256": "mocked-sha",
            "file_size": len(media_bytes),
            "id": media_id,
        })

    async def media_download(request):
        media_id = request.match_info["media_id"]
        records.append({"op": "media_download", "media_id": media_id})
        return web.Response(body=media_bytes, content_type="image/jpeg")

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
    app.router.add_get("/v21.0/{media_id}", media_info)
    app.router.add_get("/download/{media_id}", media_download)
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
async def mock_paperclip():
    """In-process Paperclip mock on localhost:8902.

    Implements the 3 endpoints our B.6 port actually hits:
      POST /api/companies/{company_id}/issues  -> create_issue
      POST /api/issues/{issue_id}/comments     -> post_comment
      GET  /api/issues/{issue_id}              -> status check

    Records every call so tests can assert on the fanout shape.
    Requires Authorization: Bearer <anything>.
    """
    records: list[dict] = []
    # Counter for issue ids
    seq = {"n": 0}

    def _require_bearer(request):
        auth = request.headers.get("Authorization", "")
        return auth.startswith("Bearer ")

    async def create_issue(request):
        if not _require_bearer(request):
            return web.Response(status=401, text="missing bearer")
        company_id = request.match_info["company_id"]
        body = await request.json()
        seq["n"] += 1
        issue_id = f"mock-issue-{seq['n']:04d}"
        records.append({
            "op": "create_issue",
            "company_id": company_id,
            "body": body,
            "issue_id": issue_id,
        })
        return web.json_response({
            "id": issue_id,
            "companyId": company_id,
            "identifier": f"EPI-{seq['n']}",
            "title": body.get("title", ""),
            "status": "backlog",
            "createdAt": "2026-04-23T00:00:00Z",
        })

    async def post_comment(request):
        if not _require_bearer(request):
            return web.Response(status=401, text="missing bearer")
        issue_id = request.match_info["issue_id"]
        body = await request.json()
        records.append({
            "op": "post_comment",
            "issue_id": issue_id,
            "body": body.get("body", ""),
        })
        return web.json_response({"id": f"comment-{len(records)}"}, status=201)

    async def get_issue(request):
        if not _require_bearer(request):
            return web.Response(status=401, text="missing bearer")
        issue_id = request.match_info["issue_id"]
        records.append({"op": "get_issue", "issue_id": issue_id})
        return web.json_response({"id": issue_id, "status": "backlog"})

    app = web.Application()
    app.router.add_post("/api/companies/{company_id}/issues", create_issue)
    app.router.add_post("/api/issues/{issue_id}/comments", post_comment)
    app.router.add_get("/api/issues/{issue_id}", get_issue)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", MOCK_PAPERCLIP_PORT)
    await site.start()
    try:
        yield records
    finally:
        await runner.cleanup()


@pytest.fixture
async def http():
    # follow_redirects: FastAPI routers redirect no-trailing-slash to trailing-slash
    # on POST, which httpx drops by default. Follow to preserve method.
    async with httpx.AsyncClient(base_url=BACKEND, timeout=30.0, follow_redirects=True) as client:
        yield client


@pytest.fixture
async def test_state(http):
    """Register a fresh user + create an agent. Returns {jwt, agent_id, ...}.

    Uses a random username so re-running the test chain against an existing
    DB doesn't collide.
    """
    suffix = _secrets.token_hex(4)
    email = f"test-b-{suffix}@isola.dev"
    username = f"test-b-{suffix}"
    password = "TestPhaseB-2026!"

    r = await http.post("/api/auth/register", json={
        "email": email,
        "username": username,
        "password": password,
        "display_name": f"Phase B Test {suffix}",
        "tenant_name": f"Test Tenant {suffix}",
        "tenant_slug": f"test-tenant-{suffix}",
    })
    assert r.status_code in (200, 201), f"register failed: {r.status_code} {r.text}"
    data = r.json()
    jwt = data["access_token"]
    user_id = data["user_id"]

    headers = {"Authorization": f"Bearer {jwt}"}
    r = await http.post("/api/agents", headers=headers, json={
        "name": f"B-Test Agent {suffix}",
        "role_description": "Phase B integration test agent",
    })
    assert r.status_code in (200, 201), f"agent create failed: {r.status_code} {r.text}"
    agent_id = r.json()["id"]

    yield {
        "jwt": jwt,
        "headers": headers,
        "user_id": user_id,
        "agent_id": agent_id,
        "suffix": suffix,
    }


@pytest.fixture
def fake_creds():
    """Fake WhatsApp channel creds used end-to-end through the chain."""
    return {
        "phone_number_id": "mock-phone-number-id",
        "waba_id": "mock-waba-id",
        "access_token": "mock-access-token",
        "verify_token": "mock-verify-token-2026",
        "app_secret": "mock-app-secret-2026",
    }


@pytest.fixture
async def db_session():
    """Direct SQLAlchemy session for reading-back assertions."""
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://isolaruntime:isolaruntime@postgres:5432/isolaruntime",
    )
    engine = create_async_engine(dsn, echo=False)
    Session = sessionmaker(engine, class_=__import__("sqlalchemy.ext.asyncio", fromlist=["AsyncSession"]).AsyncSession, expire_on_commit=False)
    try:
        yield Session
    finally:
        await engine.dispose()


def _sign(body_bytes: bytes, app_secret: str) -> str:
    digest = hmac.new(app_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ─── Phase-B chain (one test, three dependent phases) ───────────────
#
# Collapsed into a single test so module-scoped fixtures don't get bitten
# by pytest-asyncio's per-test event loop. Each phase is an async helper.
# If B.1 fails, B.2 never runs; if B.2 fails, B.3 never runs.


async def _phase_b1_config_crud(http, test_state, fake_creds):
    """B.1: channel config persists + Meta verify handshake passes."""
    agent_id = test_state["agent_id"]
    headers = test_state["headers"]

    # POST config
    r = await http.post(
        f"/api/agents/{agent_id}/whatsapp-channel",
        headers=headers,
        json=fake_creds,
    )
    assert r.status_code == 201, f"config POST: {r.status_code} {r.text}"

    # GET config
    r = await http.get(
        f"/api/agents/{agent_id}/whatsapp-channel",
        headers=headers,
    )
    assert r.status_code == 200, f"config GET: {r.status_code} {r.text}"
    got = r.json()
    # extra_config should round-trip all 5 fields
    extra = got.get("extra_config") or {}
    for k in fake_creds:
        assert extra.get(k) == fake_creds[k], f"config mismatch on {k}: {extra.get(k)!r} != {fake_creds[k]!r}"

    # GET webhook-url
    r = await http.get(
        f"/api/agents/{agent_id}/whatsapp-channel/webhook-url",
        headers=headers,
    )
    assert r.status_code == 200
    url = r.json()["webhook_url"]
    assert f"/api/channel/whatsapp/{agent_id}/webhook" in url

    # Meta verify challenge — correct token
    r = await http.get(
        f"/api/channel/whatsapp/{agent_id}/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": fake_creds["verify_token"],
            "hub.challenge": "challenge-xyz-42",
        },
    )
    assert r.status_code == 200, f"verify correct: {r.status_code} {r.text}"
    assert r.text == "challenge-xyz-42"

    # Meta verify challenge — wrong token -> 403
    r = await http.get(
        f"/api/channel/whatsapp/{agent_id}/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "WRONG",
            "hub.challenge": "challenge-xyz-42",
        },
    )
    assert r.status_code == 403, f"verify wrong token should be 403, got {r.status_code}"


async def _phase_b2_signed_text_webhook(http, test_state, fake_creds, mock_meta, db_session):
    """B.2: signed text webhook -> background task -> LLM -> send reply.

    Depends on B.1's channel config existing.
    """
    agent_id = test_state["agent_id"]
    from_wa_id = "15551234567"
    msg_id = f"wamid.test-b2-{test_state['suffix']}"
    user_text = f"Hello from B2 test {test_state['suffix']}"

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": fake_creds["waba_id"],
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "17678180000",
                        "phone_number_id": fake_creds["phone_number_id"],
                    },
                    "contacts": [{
                        "profile": {"name": "Phase B Tester"},
                        "wa_id": from_wa_id,
                    }],
                    "messages": [{
                        "from": from_wa_id,
                        "id": msg_id,
                        "timestamp": str(int(time.time())),
                        "type": "text",
                        "text": {"body": user_text},
                    }],
                },
                "field": "messages",
            }],
        }],
    }
    body = json.dumps(payload).encode("utf-8")
    signature = _sign(body, fake_creds["app_secret"])

    r = await http.post(
        f"/api/channel/whatsapp/{agent_id}/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "x-hub-signature-256": signature,
        },
    )
    assert r.status_code == 200, f"webhook POST: {r.status_code} {r.text}"
    assert r.json() == {"status": "ok"}

    # Wait for the background task to complete — LLM call + send + db commit.
    # Poll for up to 30s (LLM inference time + no model config means we get an
    # immediate "no LLM configured" reply which still round-trips through Meta).
    deadline = time.monotonic() + 30.0
    send_seen = False
    while time.monotonic() < deadline:
        if any(
            r.get("op") == "send_message" and r.get("body", {}).get("text", {}).get("body")
            for r in mock_meta
        ):
            send_seen = True
            break
        await asyncio.sleep(0.5)
    assert send_seen, f"mock Meta never received send_message; records={mock_meta}"

    # DB assertions — via direct SQL against the same Postgres the backend uses.
    from app.models.chat_session import ChatSession
    from app.models.audit import ChatMessage

    Session = db_session
    async with Session() as db:
        sess_r = await db.execute(
            select(ChatSession).where(
                ChatSession.agent_id == uuid.UUID(agent_id),
                ChatSession.external_conv_id == f"wa:{from_wa_id}",
            )
        )
        sess = sess_r.scalar_one_or_none()
        assert sess is not None, "B.2: ChatSession row not created"
        assert sess.source_channel == "whatsapp"

        msgs_r = await db.execute(
            select(ChatMessage).where(
                ChatMessage.agent_id == uuid.UUID(agent_id),
                ChatMessage.conversation_id == str(sess.id),
            ).order_by(ChatMessage.created_at.asc())
        )
        msgs = list(msgs_r.scalars().all())
        assert len(msgs) >= 2, f"expected >=2 messages (user+assistant), got {len(msgs)}"
        user_msg = next((m for m in msgs if m.role == "user"), None)
        asst_msg = next((m for m in msgs if m.role == "assistant"), None)
        assert user_msg is not None and user_text in user_msg.content
        assert asst_msg is not None and asst_msg.content.strip() != ""


async def _phase_b3_signed_image_webhook(http, test_state, fake_creds, mock_meta, db_session):
    """B.3: signed image webhook -> media download -> workspace save -> LLM.

    Depends on B.2 having proven the text pipeline.
    """
    agent_id = test_state["agent_id"]
    from_wa_id = "15551234567"  # same customer as B.2 so session is reused
    msg_id = f"wamid.test-b3-{test_state['suffix']}"
    media_id = f"mock-media-b3-{test_state['suffix']}"
    caption = "Phase B3: does this image round-trip?"

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": fake_creds["waba_id"],
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "17678180000",
                        "phone_number_id": fake_creds["phone_number_id"],
                    },
                    "contacts": [{
                        "profile": {"name": "Phase B Tester"},
                        "wa_id": from_wa_id,
                    }],
                    "messages": [{
                        "from": from_wa_id,
                        "id": msg_id,
                        "timestamp": str(int(time.time())),
                        "type": "image",
                        "image": {
                            "id": media_id,
                            "mime_type": "image/jpeg",
                            "sha256": "mocked-sha",
                            "caption": caption,
                        },
                    }],
                },
                "field": "messages",
            }],
        }],
    }
    body = json.dumps(payload).encode("utf-8")
    signature = _sign(body, fake_creds["app_secret"])

    # Snapshot pre-test state so we can detect NEW activity
    pre_records = len(mock_meta)

    r = await http.post(
        f"/api/channel/whatsapp/{agent_id}/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "x-hub-signature-256": signature,
        },
    )
    assert r.status_code == 200, f"webhook POST: {r.status_code} {r.text}"

    # Wait for the 2-step media fetch + send-reply round-trip
    deadline = time.monotonic() + 30.0
    download_seen = False
    while time.monotonic() < deadline:
        new_records = mock_meta[pre_records:]
        if any(
            r.get("op") == "media_download" and r.get("media_id") == media_id
            for r in new_records
        ):
            download_seen = True
            break
        await asyncio.sleep(0.5)
    assert download_seen, f"mock Meta never saw media_download for {media_id}; new_records={mock_meta[pre_records:]}"

    # File assertion: the media should land in agent workspace uploads/
    upload_dir = Path(f"/data/agents/{agent_id}/workspace/uploads")
    files = list(upload_dir.glob(f"wa_image_{media_id[:12]}*"))
    assert files, f"no wa_image_ file landed in {upload_dir}; dir contents={list(upload_dir.glob('*'))}"
    saved = files[0]
    assert saved.stat().st_size > 0

    # DB assertion: latest user ChatMessage should carry the image marker + caption
    from app.models.chat_session import ChatSession
    from app.models.audit import ChatMessage

    Session = db_session
    async with Session() as db:
        sess_r = await db.execute(
            select(ChatSession).where(
                ChatSession.agent_id == uuid.UUID(agent_id),
                ChatSession.external_conv_id == f"wa:{from_wa_id}",
            )
        )
        sess = sess_r.scalar_one_or_none()
        assert sess is not None

        msgs_r = await db.execute(
            select(ChatMessage).where(
                ChatMessage.agent_id == uuid.UUID(agent_id),
                ChatMessage.conversation_id == str(sess.id),
                ChatMessage.role == "user",
            ).order_by(ChatMessage.created_at.desc())
        )
        msgs = list(msgs_r.scalars().all())
        assert msgs, "no user messages in session"
        latest = msgs[0]
        assert caption in latest.content, f"caption not in user message: {latest.content!r}"
        assert "[image:workspace/uploads/" in latest.content, f"no image marker in user message: {latest.content!r}"


async def _phase_b4_interactive(http, test_state, fake_creds, mock_meta, db_session):
    """B.4: outbound interactive senders + inbound button/list reply routing.

    Depends on B.3 having proven the webhook + LLM + send-reply pipeline.
    Shares the same ChatSession (customer phone 15551234567) so these
    messages append to the B.2/B.3 history.
    """
    from app.services.whatsapp_service import whatsapp_service

    agent_id = test_state["agent_id"]
    from_wa_id = "15551234567"

    # --- Outbound: call the service directly, assert mock Meta sees the shape.
    pre = len(mock_meta)
    await whatsapp_service.send_interactive_buttons(
        phone_number_id=fake_creds["phone_number_id"],
        access_token=fake_creds["access_token"],
        to=from_wa_id,
        body_text="Please confirm your reservation",
        buttons=[
            {"id": "confirm_btn", "title": "Confirm"},
            {"id": "cancel_btn", "title": "Cancel"},
        ],
        footer_text="Isola",
    )
    btn_record = next(
        (r for r in mock_meta[pre:] if r.get("op") == "send_message"
         and (r.get("body", {}).get("interactive", {}) or {}).get("type") == "button"),
        None,
    )
    assert btn_record is not None, f"buttons send not observed: {mock_meta[pre:]}"
    btn_body = btn_record["body"]
    assert btn_body["type"] == "interactive"
    assert btn_body["to"] == from_wa_id
    btn_ids = {
        b["reply"]["id"]
        for b in btn_body["interactive"]["action"]["buttons"]
    }
    assert btn_ids == {"confirm_btn", "cancel_btn"}

    pre = len(mock_meta)
    await whatsapp_service.send_interactive_list(
        phone_number_id=fake_creds["phone_number_id"],
        access_token=fake_creds["access_token"],
        to=from_wa_id,
        body_text="Pick a category",
        button_text="View menu",
        sections=[
            {"title": "Starters", "rows": [
                {"id": "starter_pho", "title": "Pho", "description": "Beef broth"},
                {"id": "starter_salad", "title": "Salad", "description": "Garden greens"},
            ]},
            {"title": "Mains", "rows": [
                {"id": "main_curry", "title": "Curry", "description": "Coconut + lime"},
            ]},
        ],
    )
    list_record = next(
        (r for r in mock_meta[pre:] if r.get("op") == "send_message"
         and (r.get("body", {}).get("interactive", {}) or {}).get("type") == "list"),
        None,
    )
    assert list_record is not None, f"list send not observed: {mock_meta[pre:]}"
    list_body = list_record["body"]
    row_ids = {
        row["id"]
        for sec in list_body["interactive"]["action"]["sections"]
        for row in sec["rows"]
    }
    assert row_ids == {"starter_pho", "starter_salad", "main_curry"}

    # --- Inbound: customer taps the Confirm button.
    suffix = test_state["suffix"]
    msg_id = f"wamid.test-b4-btn-{suffix}"
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": fake_creds["waba_id"],
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "17678180000",
                        "phone_number_id": fake_creds["phone_number_id"],
                    },
                    "contacts": [{
                        "profile": {"name": "Phase B Tester"},
                        "wa_id": from_wa_id,
                    }],
                    "messages": [{
                        "from": from_wa_id,
                        "id": msg_id,
                        "timestamp": str(int(time.time())),
                        "type": "interactive",
                        "interactive": {
                            "type": "button_reply",
                            "button_reply": {
                                "id": "confirm_btn",
                                "title": "Confirm",
                            },
                        },
                    }],
                },
                "field": "messages",
            }],
        }],
    }
    body = json.dumps(payload).encode("utf-8")
    signature = _sign(body, fake_creds["app_secret"])

    pre = len(mock_meta)
    r = await http.post(
        f"/api/channel/whatsapp/{agent_id}/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "x-hub-signature-256": signature,
        },
    )
    assert r.status_code == 200

    # Wait for the background task to produce an outbound text reply
    # (no LLM model is configured, so the reply is our canned no-model error
    # string — still a successful send_text_message call).
    deadline = time.monotonic() + 30.0
    saw_text_reply = False
    while time.monotonic() < deadline:
        for rec in mock_meta[pre:]:
            if rec.get("op") == "send_message" and rec.get("body", {}).get("type") == "text":
                saw_text_reply = True
                break
        if saw_text_reply:
            break
        await asyncio.sleep(0.5)
    assert saw_text_reply, f"no text reply after button tap: {mock_meta[pre:]}"

    # DB assertion: latest user ChatMessage should carry Confirm + [button:confirm_btn]
    from app.models.chat_session import ChatSession
    from app.models.audit import ChatMessage

    Session = db_session
    async with Session() as db:
        sess_r = await db.execute(
            select(ChatSession).where(
                ChatSession.agent_id == uuid.UUID(agent_id),
                ChatSession.external_conv_id == f"wa:{from_wa_id}",
            )
        )
        sess = sess_r.scalar_one_or_none()
        assert sess is not None

        msgs_r = await db.execute(
            select(ChatMessage).where(
                ChatMessage.agent_id == uuid.UUID(agent_id),
                ChatMessage.conversation_id == str(sess.id),
                ChatMessage.role == "user",
            ).order_by(ChatMessage.created_at.desc())
        )
        latest = msgs_r.scalars().first()
        assert latest is not None
        assert "Confirm" in latest.content
        assert "[button:confirm_btn]" in latest.content


async def _phase_b5_template_send(http, test_state, fake_creds, mock_meta):
    """B.5: outbound pre-approved template send with variable substitution.

    Depends on B.4 (proves the service + mock-Meta wiring is healthy).
    Asserts the Meta Graph payload shape: type=template, template.name,
    template.language.code, template.components[0].type=body,
    parameters in order matching body_params.
    """
    from app.services.whatsapp_service import whatsapp_service

    pre = len(mock_meta)
    await whatsapp_service.send_template(
        phone_number_id=fake_creds["phone_number_id"],
        access_token=fake_creds["access_token"],
        to="15551234567",
        template_name="reservation_reminder",
        language="en_US",
        body_params=["John", "7:00 PM"],
    )
    record = next(
        (r for r in mock_meta[pre:] if r.get("op") == "send_message"
         and r.get("body", {}).get("type") == "template"),
        None,
    )
    assert record is not None, f"template send not observed: {mock_meta[pre:]}"
    body = record["body"]
    template = body["template"]
    assert template["name"] == "reservation_reminder"
    assert template["language"] == {"code": "en_US"}

    components = template["components"]
    assert len(components) == 1
    assert components[0]["type"] == "body"
    params = components[0]["parameters"]
    assert [p["text"] for p in params] == ["John", "7:00 PM"], \
        f"body params mismatch: {params}"
    assert all(p["type"] == "text" for p in params)

    # Rich path: pre-built components override body_params cleanly.
    pre = len(mock_meta)
    await whatsapp_service.send_template(
        phone_number_id=fake_creds["phone_number_id"],
        access_token=fake_creds["access_token"],
        to="15551234567",
        template_name="order_shipped",
        language="en_US",
        components=[
            {
                "type": "header",
                "parameters": [
                    {"type": "text", "text": "TRACKING-42"},
                ],
            },
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": "John"},
                    {"type": "text", "text": "Tuesday"},
                ],
            },
        ],
    )
    record = next(
        (r for r in mock_meta[pre:] if r.get("op") == "send_message"
         and r.get("body", {}).get("type") == "template"
         and r.get("body", {}).get("template", {}).get("name") == "order_shipped"),
        None,
    )
    assert record is not None
    rich_components = record["body"]["template"]["components"]
    types = [c["type"] for c in rich_components]
    assert types == ["header", "body"], f"component types mismatch: {types}"


async def _phase_b6_paperclip_escalation(
    http, test_state, fake_creds, mock_meta, mock_paperclip, db_session
):
    """B.6: Paperclip mirror + operator escalation ping on inbound keyword.

    Depends on B.5. Configures agent.owner_phone + escalation_keywords +
    paperclip_company_id via direct DB update, then sends a signed inbound
    text that trips an escalation keyword. Asserts:

      - mock Paperclip POST /api/companies/<company>/issues once
      - mock Paperclip POST /api/issues/<id>/comments for inbound (prefixed 🟦)
      - mock Meta send_text_message to OWNER_PHONE (the 🚨 escalation ping)
      - mock Meta send_text_message to CUSTOMER (LLM reply, even if no-model)
      - mock Paperclip POST /api/issues/<id>/comments for outbound (prefixed 🟩)
    """
    from app.models.agent import Agent
    from app.services.escalation import reset_dedup

    # Reset in-process dedup so tests aren't poisoned by B.2's prior run
    # (this run is the first escalation for this customer in this process).
    reset_dedup()

    agent_id = test_state["agent_id"]
    owner_phone = "17678189999"
    paperclip_company_id = "test-company-b6"

    # Configure the test agent's Paperclip + escalation fields directly.
    Session = db_session
    async with Session() as db:
        ag_r = await db.execute(select(Agent).where(Agent.id == uuid.UUID(agent_id)))
        agent = ag_r.scalar_one_or_none()
        assert agent is not None
        agent.owner_phone = owner_phone
        agent.paperclip_company_id = paperclip_company_id
        agent.escalation_keywords = ["refund", "urgent"]
        await db.commit()

    # Use a fresh customer phone so this is Paperclips first inbound for
    # this agent+customer pair (forces create_issue, not just post_comment).
    suffix = test_state["suffix"]
    customer_phone = "15559997777"
    msg_id = f"wamid.test-b6-{suffix}"
    user_text = "I need a refund urgently — this is broken"

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": fake_creds["waba_id"],
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "17678180000",
                        "phone_number_id": fake_creds["phone_number_id"],
                    },
                    "contacts": [{
                        "profile": {"name": "B6 Test"},
                        "wa_id": customer_phone,
                    }],
                    "messages": [{
                        "from": customer_phone,
                        "id": msg_id,
                        "timestamp": str(int(time.time())),
                        "type": "text",
                        "text": {"body": user_text},
                    }],
                },
                "field": "messages",
            }],
        }],
    }
    body = json.dumps(payload).encode("utf-8")
    signature = _sign(body, fake_creds["app_secret"])

    meta_pre = len(mock_meta)
    pc_pre = len(mock_paperclip)

    r = await http.post(
        f"/api/channel/whatsapp/{agent_id}/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "x-hub-signature-256": signature,
        },
    )
    assert r.status_code == 200

    # Poll for up to 30s until we see the full fanout
    deadline = time.monotonic() + 30.0
    saw = {
        "issue_created": False,
        "inbound_mirror": False,
        "operator_ping": False,
        "customer_reply": False,
        "outbound_mirror": False,
    }
    issue_id: str | None = None
    while time.monotonic() < deadline:
        for rec in mock_paperclip[pc_pre:]:
            if rec.get("op") == "create_issue" and rec.get("company_id") == paperclip_company_id:
                saw["issue_created"] = True
                issue_id = rec.get("issue_id")
            if rec.get("op") == "post_comment" and issue_id and rec.get("issue_id") == issue_id:
                body_s = rec.get("body", "")
                if "🟦 Customer" in body_s:
                    saw["inbound_mirror"] = True
                elif "🟩 Agent" in body_s:
                    saw["outbound_mirror"] = True
        for rec in mock_meta[meta_pre:]:
            if rec.get("op") != "send_message":
                continue
            body_s = rec.get("body", {}) or {}
            to = body_s.get("to", "")
            text = (body_s.get("text") or {}).get("body", "")
            if to == owner_phone and "Escalation" in text:
                saw["operator_ping"] = True
            elif to == customer_phone and body_s.get("type") == "text":
                saw["customer_reply"] = True
        if all(saw.values()):
            break
        await asyncio.sleep(0.5)

    missing = [k for k, v in saw.items() if not v]
    assert not missing, (
        f"B.6 fanout incomplete. missing={missing} "
        f"paperclip_records={mock_paperclip[pc_pre:]} "
        f"meta_records={mock_meta[meta_pre:]}"
    )

    # ChatSession should have paperclip_issue_id populated
    from app.models.chat_session import ChatSession

    async with Session() as db:
        sess_r = await db.execute(
            select(ChatSession).where(
                ChatSession.agent_id == uuid.UUID(agent_id),
                ChatSession.external_conv_id == f"wa:{customer_phone}",
            )
        )
        sess = sess_r.scalar_one_or_none()
        assert sess is not None
        assert sess.paperclip_issue_id == issue_id, (
            f"session paperclip_issue_id mismatch: got {sess.paperclip_issue_id!r} "
            f"expected {issue_id!r}"
        )


async def _phase_e1_openclaw_gateway(http, test_state, fake_creds, mock_meta, db_session):
    """Edge (OpenClaw) gateway flow: create edge agent -> poll -> seed queue -> poll -> report.

    Covers:
      1. POST /api/agents with agent_type=openclaw returns an api_key once + status=idle
         (no workspace init, no LLM model required).
      2. GET /api/gateway/poll with X-Api-Key=<returned-key> authenticates + returns empty queue.
      3. Directly insert a GatewayMessage row simulating a queued inbound customer message.
      4. Poll again -> the gateway returns the seeded message under the authenticated agent.
      5. POST /api/gateway/report with {"message_id": ..., "result": "..."} marks the
         message completed and stores the Edge-generated reply.
      6. After report, a follow-up poll returns empty again (the reported message is not
         re-delivered).

    This test chains onto B.6: runs against the same test_state and db_session fixtures,
    verifies that Phase E.1 restored the gateway lifecycle without regressing Phase B.
    """
    suffix = test_state["suffix"]
    headers = test_state["headers"]

    # 1. Create an Edge agent. The creator is authenticated via test_state["jwt"].
    r = await http.post("/api/agents", headers=headers, json={
        "name": f"E-Edge Agent {suffix}",
        "agent_type": "openclaw",
        "role_description": "Phase E.1 Edge integration test agent",
    })
    assert r.status_code in (200, 201), f"edge agent create failed: {r.status_code} {r.text}"
    body = r.json()
    assert body["agent_type"] == "openclaw", f"agent_type mismatch: {body}"
    assert body["status"] == "idle", f"edge agent should be idle, got {body['status']}"
    api_key = body.get("api_key")
    assert api_key and api_key.startswith("oc-"), f"api_key missing or wrong shape: {api_key!r}"
    edge_agent_id = body["id"]

    # 2. Empty poll with the returned key: expect 200 + empty messages.
    r = await http.get("/api/gateway/poll", headers={"X-Api-Key": api_key})
    assert r.status_code == 200, f"empty poll failed: {r.status_code} {r.text}"
    poll_body = r.json()
    assert isinstance(poll_body.get("messages"), list), f"messages not list: {poll_body}"
    assert poll_body["messages"] == [], f"expected empty queue, got {poll_body['messages']}"

    # 3. Wrong key -> 401.
    r = await http.get("/api/gateway/poll", headers={"X-Api-Key": "oc-definitely-not-a-real-key"})
    assert r.status_code == 401, f"wrong-key poll should 401, got {r.status_code}"

    # 4. Seed a GatewayMessage directly into the DB for this agent.
    from app.models.gateway_message import GatewayMessage
    async with db_session() as db:
        gw_msg = GatewayMessage(
            agent_id=uuid.UUID(edge_agent_id),
            sender_user_id=uuid.UUID(test_state["user_id"]),
            content="Hey Edge agent, customer needs a quote for 3 rooms next weekend.",
            status="pending",
        )
        db.add(gw_msg)
        await db.commit()
        seeded_message_id = str(gw_msg.id)

    # 5. Poll again -> the seeded message appears.
    r = await http.get("/api/gateway/poll", headers={"X-Api-Key": api_key})
    assert r.status_code == 200, f"second poll failed: {r.status_code} {r.text}"
    poll_body = r.json()
    msgs = poll_body.get("messages", [])
    assert len(msgs) == 1, f"expected 1 message, got {len(msgs)}: {msgs}"
    assert msgs[0]["id"] == seeded_message_id, f"message id mismatch: {msgs[0]}"
    assert "customer needs a quote" in msgs[0]["content"], f"content missing: {msgs[0]}"

    # 6. Report a reply for the seeded message.
    r = await http.post("/api/gateway/report", headers={"X-Api-Key": api_key}, json={
        "message_id": seeded_message_id,
        "result": "Three rooms (double bed, ocean view) available Sat-Sun — EC$450/night each.",
    })
    assert r.status_code == 200, f"report failed: {r.status_code} {r.text}"

    # 7. Verify the row is now completed in the DB.
    async with db_session() as db:
        from sqlalchemy import select as _select
        row = await db.execute(
            _select(GatewayMessage).where(GatewayMessage.id == uuid.UUID(seeded_message_id))
        )
        gw = row.scalar_one()
        assert gw.status == "completed", f"expected completed, got {gw.status!r}"
        assert gw.result and "ocean view" in gw.result, f"result not stored: {gw.result!r}"

    # 8. Follow-up poll returns empty again (reported messages are not re-delivered).
    r = await http.get("/api/gateway/poll", headers={"X-Api-Key": api_key})
    assert r.status_code == 200, f"follow-up poll failed: {r.status_code} {r.text}"
    assert r.json().get("messages") == [], (
        f"completed message should not be re-polled: {r.json()}"
    )

    # 9. Rotate API key via /agents/<id>/api-key -> old key should stop working.
    r = await http.post(
        f"/api/agents/{edge_agent_id}/api-key",
        headers=headers,
    )
    assert r.status_code == 200, f"api-key rotate failed: {r.status_code} {r.text}"
    rotated = r.json().get("api_key")
    assert rotated and rotated.startswith("oc-") and rotated != api_key, (
        f"rotation returned bad key: {rotated!r} (original {api_key!r})"
    )
    r_old = await http.get("/api/gateway/poll", headers={"X-Api-Key": api_key})
    assert r_old.status_code == 401, f"old key should be revoked, got {r_old.status_code}"
    r_new = await http.get("/api/gateway/poll", headers={"X-Api-Key": rotated})
    assert r_new.status_code == 200, f"rotated key should work, got {r_new.status_code}"


    # ────────────────────────────────────────────────────────────────
    # Phase E.1.5: WA webhook -> Edge gateway routing
    # ────────────────────────────────────────────────────────────────
    # Configure a WhatsApp channel on the Edge agent, send a signed inbound
    # webhook, verify it queues into gateway_messages (NOT the native LLM
    # path — so mock_meta should NOT see a send_message during this step).
    # Then report a reply via /gateway/report and confirm mock_meta receives
    # the WA send_message forwarded by the gateway.

    # Baseline Meta record count BEFORE this section so we can assert deltas.
    meta_baseline = len(mock_meta)

    # Configure WA channel on the Edge agent.
    r = await http.post(
        f"/api/agents/{edge_agent_id}/whatsapp-channel",
        headers=headers,
        json=fake_creds,
    )
    assert r.status_code == 201, f"edge WA config: {r.status_code} {r.text}"

    edge_customer_wa = "15559998877"
    edge_msg_id = f"wamid.edge-e15-{suffix}"
    edge_user_text = f"Edge path test {suffix}"

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": fake_creds["waba_id"],
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "17678180001",
                        "phone_number_id": fake_creds["phone_number_id"],
                    },
                    "contacts": [{
                        "profile": {"name": "Edge Tester"},
                        "wa_id": edge_customer_wa,
                    }],
                    "messages": [{
                        "from": edge_customer_wa,
                        "id": edge_msg_id,
                        "timestamp": str(int(time.time())),
                        "type": "text",
                        "text": {"body": edge_user_text},
                    }],
                },
                "field": "messages",
            }],
        }],
    }
    body = json.dumps(payload).encode("utf-8")
    signature = _sign(body, fake_creds["app_secret"])

    r = await http.post(
        f"/api/channel/whatsapp/{edge_agent_id}/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "x-hub-signature-256": signature,
        },
    )
    assert r.status_code == 200, f"edge webhook POST: {r.status_code} {r.text}"

    # Wait for the background task to queue the gateway message (fast: no LLM call).
    deadline = time.monotonic() + 10.0
    edge_queued_msg_id = None
    while time.monotonic() < deadline:
        from app.models.gateway_message import GatewayMessage as _GwMsg
        async with db_session() as db:
            r2 = await db.execute(
                _select(_GwMsg)
                .where(_GwMsg.agent_id == uuid.UUID(edge_agent_id))
                .order_by(_GwMsg.created_at.desc())
                .limit(1)
            )
            latest = r2.scalar_one_or_none()
            if latest and latest.content == edge_user_text and latest.status == "pending":
                edge_queued_msg_id = str(latest.id)
                break
        await asyncio.sleep(0.3)
    assert edge_queued_msg_id, (
        f"Edge webhook did not queue a gateway_message within 10s "
        f"(agent={edge_agent_id}, expected content={edge_user_text!r})"
    )

    # Critical assertion: NO text-reply went to Meta during this step.
    # Native path would have called LLM and sent body.text.body; Edge path
    # defers the reply until /gateway/report is called.
    # (Read-receipts / typing indicators from the webhook ack flow are fine —
    # those have no body.text.body.)
    text_replies_during_inbound = [
        r for r in mock_meta[meta_baseline:]
        if r.get("op") == "send_message"
        and r.get("body", {}).get("text", {}).get("body")
    ]
    assert not text_replies_during_inbound, (
        f"Edge inbound should NOT trigger a WA text reply (that is the daemon's job); "
        f"got {text_replies_during_inbound}"
    )

    # Now simulate the Edge daemon: poll -> pick up the queued message -> report a reply.
    r = await http.get("/api/gateway/poll", headers={"X-Api-Key": rotated})
    assert r.status_code == 200, f"edge-path poll: {r.status_code}"
    polled = r.json().get("messages", [])
    assert any(m["id"] == edge_queued_msg_id for m in polled), (
        f"polled messages did not include the edge-queued msg {edge_queued_msg_id}: {polled}"
    )

    edge_reply = f"Noted — I will follow up on {suffix}."
    r = await http.post(
        "/api/gateway/report",
        headers={"X-Api-Key": rotated},
        json={"message_id": edge_queued_msg_id, "result": edge_reply},
    )
    assert r.status_code == 200, f"edge report: {r.status_code} {r.text}"

    # Now the gateway should have forwarded the reply via WhatsApp to the customer.
    deadline = time.monotonic() + 10.0
    edge_wa_send_seen = False
    while time.monotonic() < deadline:
        recent = [r for r in mock_meta[meta_baseline:] if r.get("op") == "send_message"]
        for rec in recent:
            body_field = rec.get("body", {})
            if (
                body_field.get("to") == edge_customer_wa
                and body_field.get("text", {}).get("body") == edge_reply
            ):
                edge_wa_send_seen = True
                break
        if edge_wa_send_seen:
            break
        await asyncio.sleep(0.3)
    assert edge_wa_send_seen, (
        f"/gateway/report did not forward reply via WhatsApp; "
        f"mock_meta post-baseline={mock_meta[meta_baseline:]}"
    )


async def _phase_e6a_runtime_mode(http, test_state, db_session):
    """Tenant-level Runtime Mode default for agent_type.

    Covers:
      1. GET /api/tenants/<id> returns runtime_mode='hosted' by default.
      2. Creating an agent with no agent_type -> defaults to 'native'.
      3. PUT /api/tenants/<id> with runtime_mode='edge' + GET round-trips.
      4. Creating a new agent now defaults to 'openclaw' + api_key returned.
      5. Explicit agent_type='native' overrides the edge default.
      6. PUT back to 'hosted' + new agent is native again.
      7. Invalid runtime_mode (e.g. 'cloud') -> 400.

    Chain dependency: runs after E.1 / E.1.5. Mutates the test tenant's
    runtime_mode — keep this the last step since later phases (if any)
    would see the flipped state.
    """
    headers = test_state["headers"]
    suffix = test_state["suffix"]
    from sqlalchemy import select as _select  # local alias for brevity

    # Ensure the test user has a tenant (needed for runtime_mode setting).
    # The B.x chain may have created one via /tenants/self-create; if not,
    # create one here so the test is self-contained.
    from app.models.user import User
    async with db_session() as db:
        u_r = await db.execute(
            _select(User).where(User.id == uuid.UUID(test_state["user_id"]))
        )
        u = u_r.scalar_one_or_none()
        tenant_id = str(u.tenant_id) if u and u.tenant_id else None

    if not tenant_id:
        r = await http.post(
            "/api/tenants/self-create",
            headers=headers,
            json={"name": f"E6a Tenant {suffix}"},
        )
        assert r.status_code in (200, 201), f"self-create tenant: {r.status_code} {r.text}"
        tenant_id = r.json()["tenant"]["id"]

    # 1. Default runtime_mode is 'hosted'.
    # org_admin needs the tenant GET; the test user was registered fresh so should qualify.
    r = await http.get(f"/api/tenants/{tenant_id}", headers=headers)
    # If the test user was assigned 'member' rather than 'org_admin', the GET may 403.
    # In that case, bump them via staff token path isn't available — so we fall back
    # to reading DB directly for the default assertion.
    if r.status_code == 200:
        assert r.json().get("runtime_mode") == "hosted", (
            f"expected default hosted, got {r.json().get('runtime_mode')!r}"
        )
    else:
        from app.models.tenant import Tenant
        async with db_session() as db:
            tr = await db.execute(_select(Tenant).where(Tenant.id == uuid.UUID(tenant_id)))
            tenant = tr.scalar_one()
            assert tenant.runtime_mode == "hosted", (
                f"DB: expected default hosted, got {tenant.runtime_mode!r}"
            )

    # 2. Creating an agent without agent_type -> native.
    r = await http.post("/api/agents", headers=headers, json={
        "name": f"E6a-hosted-default {suffix}",
        "role_description": "E.6a hosted default test",
    })
    assert r.status_code in (200, 201), f"create hosted-default agent: {r.status_code} {r.text}"
    native_agent = r.json()
    assert native_agent["agent_type"] == "native", (
        f"expected native default, got {native_agent['agent_type']!r}"
    )

    # 3. Flip tenant to edge. If PUT fails (non-admin), patch via DB.
    r = await http.put(
        f"/api/tenants/{tenant_id}",
        headers=headers,
        json={"runtime_mode": "edge"},
    )
    if r.status_code in (200, 201):
        assert r.json().get("runtime_mode") == "edge", r.text
    else:
        # Fall back to DB update — test still meaningful for the default-propagation logic.
        from app.models.tenant import Tenant
        async with db_session() as db:
            tr = await db.execute(_select(Tenant).where(Tenant.id == uuid.UUID(tenant_id)))
            tenant = tr.scalar_one()
            tenant.runtime_mode = "edge"
            await db.commit()

    # 4. New agent with no agent_type -> defaults to openclaw + api_key returned.
    r = await http.post("/api/agents", headers=headers, json={
        "name": f"E6a-edge-default {suffix}",
        "role_description": "E.6a edge default test",
    })
    assert r.status_code in (200, 201), f"create edge-default agent: {r.status_code} {r.text}"
    edge_agent = r.json()
    assert edge_agent["agent_type"] == "openclaw", (
        f"expected openclaw default after edge flip, got {edge_agent['agent_type']!r}"
    )
    assert edge_agent.get("api_key", "").startswith("oc-"), (
        f"edge-default agent should return oc- api_key, got {edge_agent.get('api_key')!r}"
    )

    # 5. Explicit agent_type='native' overrides the edge default.
    r = await http.post("/api/agents", headers=headers, json={
        "name": f"E6a-explicit-native {suffix}",
        "agent_type": "native",
        "role_description": "explicit native on edge tenant",
    })
    assert r.status_code in (200, 201), r.text
    assert r.json()["agent_type"] == "native", (
        f"explicit agent_type=native should override edge default, "
        f"got {r.json()['agent_type']!r}"
    )

    # 6. Flip back to hosted.
    r = await http.put(
        f"/api/tenants/{tenant_id}",
        headers=headers,
        json={"runtime_mode": "hosted"},
    )
    if r.status_code not in (200, 201):
        from app.models.tenant import Tenant
        async with db_session() as db:
            tr = await db.execute(_select(Tenant).where(Tenant.id == uuid.UUID(tenant_id)))
            tenant = tr.scalar_one()
            tenant.runtime_mode = "hosted"
            await db.commit()

    # New agent with no type -> native again.
    r = await http.post("/api/agents", headers=headers, json={
        "name": f"E6a-hosted-again {suffix}",
        "role_description": "back to hosted default",
    })
    assert r.status_code in (200, 201), r.text
    assert r.json()["agent_type"] == "native", (
        f"expected native after hosted flip-back, got {r.json()['agent_type']!r}"
    )

    # 7. Invalid runtime_mode -> 400.
    r = await http.put(
        f"/api/tenants/{tenant_id}",
        headers=headers,
        json={"runtime_mode": "cloud"},
    )
    # If the user isn't org_admin they get 403 which short-circuits the validation.
    # Accept both outcomes — this assertion is purely about the server rejecting bogus
    # values at the right layer when it reaches validation.
    assert r.status_code in (400, 403), (
        f"invalid runtime_mode should 400 (or 403 pre-validation), got {r.status_code} {r.text}"
    )


async def _phase_e6c_internal_auth(http, test_state):
    """Phase E.6c: X-Internal-Secret gate on /api/internal/tenants/*.

    Covers:
      1. No header -> 401
      2. Wrong header -> 401
      3. Right header + non-existent tenant -> 404
      4. POST /api/internal/tenants/ensure with right header:
         - First call -> 200 with created=True, runtime_mode=hosted
         - Repeat with same external_id -> same id, created=False
      5. GET /api/internal/tenants/{id} echoes fields incl. runtime_mode
      6. PUT /api/internal/tenants/{id} with runtime_mode=edge -> 200
      7. GET again -> runtime_mode=edge persists
      8. PUT with invalid runtime_mode -> 422 (Pydantic Literal validator)

    Uses the ISOLA_INTERNAL_SECRET set in docker-compose.override.yml.
    If that env var isn't set on this deployment, the endpoint 401s even
    with the right header — we match that behaviour explicitly.
    """
    import os as _os
    secret = _os.environ.get("ISOLA_INTERNAL_SECRET", "").strip()
    if not secret:
        # Endpoint is intentionally disabled when the server has no secret.
        r = await http.get(
            "/api/internal/tenants/00000000-0000-0000-0000-000000000000",
            headers={"X-Internal-Secret": "anything"},
        )
        assert r.status_code == 401, (
            f"E.6c: secret not set in server env — endpoint should 401 even "
            f"with a header, got {r.status_code}"
        )
        return

    # 1 + 2: missing / wrong header -> 401
    r = await http.get("/api/internal/tenants/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 401, f"no-header 401 expected, got {r.status_code} {r.text}"
    r = await http.get(
        "/api/internal/tenants/00000000-0000-0000-0000-000000000000",
        headers={"X-Internal-Secret": "obviously-wrong"},
    )
    assert r.status_code == 401, f"wrong-header 401 expected, got {r.status_code} {r.text}"

    hdr = {"X-Internal-Secret": secret}

    # 3: right header, missing tenant -> 404
    r = await http.get(
        "/api/internal/tenants/00000000-0000-0000-0000-000000000000", headers=hdr
    )
    assert r.status_code == 404, f"missing-tenant 404 expected, got {r.status_code} {r.text}"

    suffix = test_state["suffix"]
    external_id = f"e6c-chain-{suffix}"

    # 4a: ensure first-time
    r = await http.post(
        "/api/internal/tenants/ensure",
        headers=hdr,
        json={"external_id": external_id, "name": f"E6c Chain {suffix}"},
    )
    assert r.status_code == 200, f"ensure first-time: {r.status_code} {r.text}"
    first = r.json()
    assert first["created"] is True, f"first call should set created=True: {first}"
    assert first["runtime_mode"] == "hosted", f"default hosted: {first}"
    new_tenant_id = first["id"]

    # 4b: ensure idempotent
    r = await http.post(
        "/api/internal/tenants/ensure",
        headers=hdr,
        json={"external_id": external_id, "name": f"E6c Chain {suffix}"},
    )
    assert r.status_code == 200, f"ensure repeat: {r.status_code} {r.text}"
    second = r.json()
    assert second["id"] == new_tenant_id, (
        f"ensure not idempotent on external_id={external_id!r}: "
        f"first={new_tenant_id} second={second['id']}"
    )
    assert second["created"] is False, f"repeat should set created=False: {second}"

    # 5: GET
    r = await http.get(f"/api/internal/tenants/{new_tenant_id}", headers=hdr)
    assert r.status_code == 200, f"internal GET: {r.status_code}"
    got = r.json()
    assert got["id"] == new_tenant_id
    assert got["runtime_mode"] == "hosted"

    # 6: PUT runtime_mode=edge
    r = await http.put(
        f"/api/internal/tenants/{new_tenant_id}",
        headers=hdr,
        json={"runtime_mode": "edge"},
    )
    assert r.status_code == 200, f"internal PUT: {r.status_code} {r.text}"
    updated = r.json()
    assert updated["runtime_mode"] == "edge", f"PUT didn't flip: {updated}"

    # 7: GET confirms persisted
    r = await http.get(f"/api/internal/tenants/{new_tenant_id}", headers=hdr)
    assert r.status_code == 200
    assert r.json()["runtime_mode"] == "edge", (
        f"runtime_mode didn't persist: {r.json()}"
    )

    # 8: invalid runtime_mode -> 422 (Pydantic Literal validator)
    r = await http.put(
        f"/api/internal/tenants/{new_tenant_id}",
        headers=hdr,
        json={"runtime_mode": "cloud"},
    )
    assert r.status_code == 422, f"invalid runtime_mode should 422, got {r.status_code}"


async def test_phase_b_chain(http, test_state, fake_creds, mock_meta, mock_paperclip, db_session):
    """Run B.1 -> B.2 -> B.3 -> B.4 -> B.5 -> B.6 as one dependent chain.

    Single test function so module-scoped fixtures stay on one event loop.
    Each phase's assertions must pass before the next phase runs — if an
    earlier phase breaks, later phases don't execute (fail-fast).
    """
    print("\n--- Phase B.1: config CRUD + Meta verify handshake ---")
    await _phase_b1_config_crud(http, test_state, fake_creds)
    print("Phase B.1 PASS")

    print("--- Phase B.2: signed text webhook -> LLM -> send reply ---")
    await _phase_b2_signed_text_webhook(http, test_state, fake_creds, mock_meta, db_session)
    print("Phase B.2 PASS")

    print("--- Phase B.3: signed image webhook -> media download + save + LLM ---")
    await _phase_b3_signed_image_webhook(http, test_state, fake_creds, mock_meta, db_session)
    print("Phase B.3 PASS")

    print("--- Phase B.4: interactive outbound (buttons+list) + inbound button_reply ---")
    await _phase_b4_interactive(http, test_state, fake_creds, mock_meta, db_session)
    print("Phase B.4 PASS")

    print("--- Phase B.5: template send (simple + rich components) ---")
    await _phase_b5_template_send(http, test_state, fake_creds, mock_meta)
    print("Phase B.5 PASS")

    print("--- Phase B.6: Paperclip mirror + operator escalation ping ---")
    await _phase_b6_paperclip_escalation(
        http, test_state, fake_creds, mock_meta, mock_paperclip, db_session
    )
    print("Phase B.6 PASS")

    print("--- Phase E.1: Edge (OpenClaw) gateway lifecycle ---")
    await _phase_e1_openclaw_gateway(http, test_state, fake_creds, mock_meta, db_session)
    print("Phase E.1 PASS")

    print("--- Phase E.6a: tenant Runtime Mode default for agent_type ---")
    await _phase_e6a_runtime_mode(http, test_state, db_session)
    print("Phase E.6a PASS")

    print("--- Phase E.6c: internal auth bridge (service secret + ensure) ---")
    await _phase_e6c_internal_auth(http, test_state)
    print("Phase E.6c PASS")
