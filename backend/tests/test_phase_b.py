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


# ─── Fixtures (shared state chain) ───────────────────────────────────


BACKEND = "http://localhost:8000"
MOCK_META_PORT = 8901


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


async def test_phase_b_chain(http, test_state, fake_creds, mock_meta, db_session):
    """Run B.1 -> B.2 -> B.3 -> B.4 -> B.5 as one dependent chain.

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
