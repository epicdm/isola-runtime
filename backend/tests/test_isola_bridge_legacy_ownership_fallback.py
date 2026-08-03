# ISOLA GLUE — additive test (not upstream)
"""Regression coverage for the legacy synchronous bridge's run-owned reply
integration (`dec-clawith-run-scoped-assistant-reply-ownership-2026-08-03`).

Closes an adversarial-review (Codex) BLOCKING finding: once
`read_run_owned_reply` raises `RunOwnedReplyError` (an existing delivery
receipt failed ownership validation -- a positive corruption signal for
this run), the endpoint must fail closed all the way to the 502
`no_assistant_reply` response. It must never fall through to the
`waiting_reason`/`result_summary` defensive fallback, which would silently
substitute unrelated, unvalidated text for a reply the ownership check just
rejected.

Follows the monkeypatch-the-module pattern already used by
`test_isola_bridge_structured.py`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.api import isola_bridge
from app.main import app
from app.services.agent_runtime.run_owned_reply import RunOwnedReply

AGENT_ID = uuid.UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")
TENANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
MODEL_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
SESSION_ID = uuid.uuid4()
RUN_ID = uuid.uuid4()

SECRET = "test-isola-bridge-secret"


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch):
    monkeypatch.setenv("ISOLA_BRIDGE_SECRET", SECRET)


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)

    async def _build():
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _build


def _headers():
    return {"X-Isola-Secret": SECRET}


def _wire_happy_path(monkeypatch, *, final_status: str, waiting_reason, result_summary):
    fake_agent = SimpleNamespace(
        id=AGENT_ID, tenant_id=TENANT_ID, name="Test Agent",
        primary_model_id=MODEL_ID, fallback_model_id=None,
    )
    fake_model = SimpleNamespace(id=MODEL_ID)
    fake_user = SimpleNamespace(id=USER_ID)
    fake_session = SimpleNamespace(id=SESSION_ID)

    class _Begin:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeDb:
        def begin(self):
            return _Begin()

        async def get(self, model_cls, pk):
            if model_cls is isola_bridge.Agent:
                return fake_agent
            if model_cls is isola_bridge.LLMModel:
                return fake_model
            if model_cls is isola_bridge.User:
                return fake_user
            return None

        async def execute(self, *a, **k):
            return SimpleNamespace(first=lambda: None)

        def expire_all(self):
            return None

    class _FakeDbFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _FakeDb()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(isola_bridge, "async_session", _FakeDbFactory())

    async def fake_ensure_session(db, agent_id, user_id):
        return fake_session

    monkeypatch.setattr(isola_bridge, "ensure_primary_platform_session", fake_ensure_session)

    async def fake_enqueue(db, **kwargs):
        return SimpleNamespace(handle=SimpleNamespace(run_id=RUN_ID), message_id=uuid.uuid4())

    monkeypatch.setattr(isola_bridge, "enqueue_chat_runtime", fake_enqueue)

    class _Reader:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get_run_state(self, tenant_id, run_id):
            return SimpleNamespace(
                execution_status=final_status,
                waiting_reason=waiting_reason,
                result_summary=result_summary,
            )

    monkeypatch.setattr(isola_bridge, "open_run_state_reader", lambda db: _Reader())


@pytest.mark.asyncio
async def test_ownership_error_fails_closed_even_when_result_summary_present(monkeypatch, client):
    """The receipt exists but fails ownership validation: must return 502,
    never substitute result_summary text."""
    _wire_happy_path(
        monkeypatch,
        final_status="completed",
        waiting_reason=None,
        result_summary="a plausible-looking but untrusted summary",
    )

    async def fake_read_run_owned_reply(db, **kwargs):
        raise isola_bridge.RunOwnedReplyError("message_agent_mismatch", "doctored receipt")

    monkeypatch.setattr(isola_bridge, "read_run_owned_reply", fake_read_run_owned_reply)

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/message",
            json={"agent_id": str(AGENT_ID), "phone": "+18095551234", "text": "hi"},
            headers=_headers(),
        )

    assert response.status_code == 502
    assert response.json()["error"] == "no_assistant_reply"


@pytest.mark.asyncio
async def test_ownership_error_fails_closed_even_when_waiting_reason_present(monkeypatch, client):
    """Same as above for the waiting_reason fallback field specifically."""
    _wire_happy_path(
        monkeypatch,
        final_status="waiting_user",
        waiting_reason="please confirm your account number",
        result_summary=None,
    )

    async def fake_read_run_owned_reply(db, **kwargs):
        raise isola_bridge.RunOwnedReplyError("receipt_session_mismatch", "doctored receipt")

    monkeypatch.setattr(isola_bridge, "read_run_owned_reply", fake_read_run_owned_reply)

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/message",
            json={"agent_id": str(AGENT_ID), "phone": "+18095551234", "text": "hi"},
            headers=_headers(),
        )

    assert response.status_code == 502
    assert response.json()["error"] == "no_assistant_reply"


@pytest.mark.asyncio
async def test_no_receipt_yet_still_falls_back_to_result_summary(monkeypatch, client):
    """Contrast case this fix must NOT touch: when no receipt exists at all
    (read_run_owned_reply returns None, never raises), the existing
    waiting_reason/result_summary fallback must keep working exactly as
    before."""
    _wire_happy_path(
        monkeypatch,
        final_status="failed",
        waiting_reason=None,
        result_summary="Run failed but produced a summary",
    )

    async def fake_read_run_owned_reply(db, **kwargs):
        return None

    monkeypatch.setattr(isola_bridge, "read_run_owned_reply", fake_read_run_owned_reply)
    monkeypatch.setattr(isola_bridge, "_MESSAGE_GRACE_S", 0.0)
    monkeypatch.setattr(isola_bridge, "_POLL_INTERVAL_S", 0.01)

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/message",
            json={"agent_id": str(AGENT_ID), "phone": "+18095551234", "text": "hi"},
            headers=_headers(),
        )

    assert response.status_code == 200
    assert response.json()["reply"] == "Run failed but produced a summary"


@pytest.mark.asyncio
async def test_no_receipt_yet_still_falls_back_to_waiting_reason(monkeypatch, client):
    """Symmetric with `test_no_receipt_yet_still_falls_back_to_result_summary`
    for the `waiting_reason` field specifically (R1.1 Phase 1 hardening,
    `dec-clawith-r1-1-run-state-fallback-and-correlation-identity-fix-2026-
    08-03`): when no receipt exists at all, the exact-run `waiting_reason` is
    returned unchanged."""
    _wire_happy_path(
        monkeypatch,
        final_status="waiting_user",
        waiting_reason="please confirm your account number",
        result_summary=None,
    )

    async def fake_read_run_owned_reply(db, **kwargs):
        return None

    monkeypatch.setattr(isola_bridge, "read_run_owned_reply", fake_read_run_owned_reply)
    monkeypatch.setattr(isola_bridge, "_MESSAGE_GRACE_S", 0.0)
    monkeypatch.setattr(isola_bridge, "_POLL_INTERVAL_S", 0.01)

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/message",
            json={"agent_id": str(AGENT_ID), "phone": "+18095551234", "text": "hi"},
            headers=_headers(),
        )

    assert response.status_code == 200
    assert response.json()["reply"] == "please confirm your account number"


@pytest.mark.asyncio
async def test_happy_path_returns_the_run_owned_reply_content(monkeypatch, client):
    """Baseline: a valid run-owned reply is returned as-is."""
    _wire_happy_path(
        monkeypatch,
        final_status="completed",
        waiting_reason=None,
        result_summary=None,
    )

    async def fake_read_run_owned_reply(db, **kwargs):
        return RunOwnedReply(
            message_id=uuid.uuid4(),
            content="Yes, EPIC installs fibre in Roseau.",
            delivery_kind="terminal",
            lifecycle_status="completed",
        )

    monkeypatch.setattr(isola_bridge, "read_run_owned_reply", fake_read_run_owned_reply)

    async with await client() as ac:
        response = await ac.post(
            "/api/isola/bridge/message",
            json={"agent_id": str(AGENT_ID), "phone": "+18095551234", "text": "hi"},
            headers=_headers(),
        )

    assert response.status_code == 200
    assert response.json()["reply"] == "Yes, EPIC installs fibre in Roseau."
