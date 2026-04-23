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


async def test_phase_b_chain(http, test_state, fake_creds, mock_meta, db_session):
    """Run B.1 -> B.2 -> B.3 as one dependent chain.

    Single test function so module-scoped fixtures stay on one event loop.
    Each phase's assertions must pass before the next phase runs — if B.1
    breaks, B.2 and B.3 don't execute (fail-fast).
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
