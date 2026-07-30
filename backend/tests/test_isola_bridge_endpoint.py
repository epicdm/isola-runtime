"""Endpoint-level regression guard for the additive structured contract.

Closes the review caveat: proves at the REAL endpoint (bridge_message) that
- empty CLAWITH_STRUCTURED_CONTRACT_AGENT_IDS  -> byte-for-byte legacy 8-key
  envelope, NO "contract" key (the property 3742/bff-v2 depend on);
- an allowlisted agent -> "contract" key present, schema_version "1.0",
  with the legacy 8 keys still intact alongside it.

The DB/runtime seams are monkeypatched so the handler runs its actual
envelope-assembly + gating code; production code is unchanged.
"""
import json
import uuid
from types import SimpleNamespace

import pytest

import app.api.isola_bridge as ib
from app.api.isola_bridge import BridgeMessageIn, bridge_message

AGENT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "99999999-9999-9999-9999-999999999999"
TENANT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
RUN_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
SESSION_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
SECRET = "test-bridge-secret"
REPLY = "hello from the agent"

LEGACY_KEYS = {
    "reply", "run_id", "matched_session", "status",
    "correlation_id", "agent_id", "agent_name", "tenant_id",
}


class _Result:
    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []


class _FakeDB:
    async def get(self, model, ident):
        name = getattr(model, "__name__", str(model))
        if name == "Agent":
            return SimpleNamespace(
                id=uuid.UUID(AGENT_ID), name="Test Agent", tenant_id=TENANT_ID,
                primary_model_id=None, fallback_model_id=uuid.uuid4(),
            )
        if name == "LLMModel":
            return SimpleNamespace(id=ident)
        if name == "User":
            return SimpleNamespace(id=ident, tenant_id=TENANT_ID)
        return None  # ChatMessage / anything else

    async def execute(self, *a, **k):
        return _Result()

    def expire_all(self):
        pass

    def begin(self):
        return _AsyncCM(self)


class _AsyncCM:
    def __init__(self, val):
        self.val = val

    async def __aenter__(self):
        return self.val

    async def __aexit__(self, *a):
        return False


class _FakeReader:
    async def get_run_state(self, tenant_id, run_id):
        return SimpleNamespace(
            execution_status="completed", waiting_reason=None, result_summary=None,
        )


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(ib, "async_session", lambda: _AsyncCM(_FakeDB()))
    monkeypatch.setattr(ib, "ensure_primary_platform_session",
                        lambda db, aid, uid: _coro(SimpleNamespace(id=SESSION_ID)))
    monkeypatch.setattr(ib, "enqueue_chat_runtime",
                        lambda *a, **k: _coro(SimpleNamespace(
                            handle=SimpleNamespace(run_id=RUN_ID), message_id=uuid.uuid4())))
    monkeypatch.setattr(ib, "open_run_state_reader", lambda db: _AsyncCM(_FakeReader()))
    monkeypatch.setattr(ib, "_read_last_assistant",
                        lambda db, sid, after: _coro(REPLY))
    monkeypatch.setenv(ib._SECRET_ENV, SECRET)


async def _coro(value):
    return value


def _body():
    return BridgeMessageIn(
        agent_id=AGENT_ID, phone="15550001234", text="hi",
        correlation_id="cid-regression",
    )


async def _call():
    resp = await bridge_message(_body(), x_isola_secret=SECRET)
    return resp.status_code, json.loads(bytes(resp.body))


@pytest.mark.asyncio
async def test_empty_allowlist_returns_byte_for_byte_legacy_envelope(monkeypatch):
    monkeypatch.setenv(ib._CONTRACT_AGENTS_ENV, "")
    status, body = await _call()
    assert status == 200
    assert set(body.keys()) == LEGACY_KEYS
    assert "contract" not in body
    assert body["agent_id"] == AGENT_ID
    assert body["tenant_id"] == str(TENANT_ID)
    assert body["reply"] == REPLY
    assert body["run_id"] == str(RUN_ID)
    assert body["matched_session"] == str(SESSION_ID)


@pytest.mark.asyncio
async def test_other_agent_allowlisted_leaves_this_agent_legacy(monkeypatch):
    monkeypatch.setenv(ib._CONTRACT_AGENTS_ENV, OTHER_ID)
    status, body = await _call()
    assert status == 200
    assert set(body.keys()) == LEGACY_KEYS
    assert "contract" not in body


@pytest.mark.asyncio
async def test_allowlisted_agent_gets_contract_key_schema_1_0(monkeypatch):
    monkeypatch.setenv(ib._CONTRACT_AGENTS_ENV, AGENT_ID)
    status, body = await _call()
    assert status == 200
    assert "contract" in body
    # legacy keys still intact alongside the additive contract
    assert set(k for k in body if k != "contract") == LEGACY_KEYS
    c = body["contract"]
    assert c["schema_version"] == "1.0"
    assert c["agent_id"] == AGENT_ID
    assert c["tenant_id"] == str(TENANT_ID)
    assert c["response_text"] == REPLY
    assert c["run_id"] == str(RUN_ID)
