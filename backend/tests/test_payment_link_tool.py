"""CHUNK C — create_payment_link tool unit tests.

Probes (per PM dispatch):
  (a) autonomy-gate refusal path: access_payment_collection fails closed to
      L3 even when an agent's stored autonomy_policy has no entry for it yet
      (a pre-existing agent row can never pick up a new Python-side model
      default retroactively -- see _DEFAULT_L3_ACTIONS in autonomy_service.py).
  (b) mint round-trip against a stubbed BFF: _create_payment_link posts the
      documented PR #16 payload shape and surfaces a friendly success message.
  (c) non-EMA agent doesn't get the tool: _PAYMENT_LINK_ALLOWED_AGENT_IDS
      scoping refuses honestly for any agentId not on the allowlist.

Plus: the tenant-binding guard (item 3 of the dispatch) and the
collection-not-live guard (item 3, second half), tested at the same
odoo_context-check-then-return-string granularity the sibling create_lead/
log_interaction/request_booking guards already use -- none of those have
deeper execute_tool()-level integration coverage either (no DB/workspace
mocking precedent exists anywhere in this repo for the odoo-gated tool
family), so this suite doesn't invent one just for this tool.

Run from the runtime backend dir:
    pytest backend/tests/test_payment_link_tool.py -v
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EMA_AGENT_ID = "8166ea11-8db0-4f26-879a-e2067be0a018"


def _fake_agent(autonomy_policy: dict):
    return type(
        "FakeAgent",
        (),
        {
            "id": uuid.uuid4(),
            "name": "EMA",
            "autonomy_policy": autonomy_policy,
            "creator_id": uuid.uuid4(),
        },
    )()


# ─── probe (a): autonomy-gate refusal path ────────────────────────────────


@pytest.mark.asyncio
async def test_access_payment_collection_defaults_to_l3_when_unset():
    """A pre-existing agent whose autonomy_policy dict has no
    access_payment_collection key must be blocked pending approval -- NOT
    silently auto-executed at the plain L2 default other keys get."""
    from app.services.autonomy_service import AutonomyService

    agent = _fake_agent({})  # key absent -- exactly the gap this guards against
    db = AsyncMock()

    with patch("app.services.notification_service.send_notification", new=AsyncMock()):
        result = await AutonomyService().check_and_enforce(
            db, agent, "access_payment_collection", {"tool": "create_payment_link"}
        )

    assert result["allowed"] is False
    assert result["level"] == "L3"


@pytest.mark.asyncio
async def test_access_payment_collection_respects_explicit_policy_override():
    """If ops explicitly sets L1/L2 for this agent, that choice is honored --
    the fail-closed default only fires when the key is truly absent."""
    from app.services.autonomy_service import AutonomyService

    agent = _fake_agent({"access_payment_collection": "L1"})
    db = AsyncMock()

    result = await AutonomyService().check_and_enforce(
        db, agent, "access_payment_collection", {"tool": "create_payment_link"}
    )

    assert result["allowed"] is True
    assert result["level"] == "L1"


@pytest.mark.asyncio
async def test_other_action_types_still_default_to_l2():
    """Regression guard: the new fail-closed default is scoped to payment
    actions only -- it must not change existing L2-default behavior for the
    other 8 autonomy keys (e.g. access_business_system_write)."""
    from app.services.autonomy_service import AutonomyService

    agent = _fake_agent({})
    db = AsyncMock()

    with patch("app.services.notification_service.send_notification", new=AsyncMock()):
        result = await AutonomyService().check_and_enforce(
            db, agent, "access_business_system_write", {"tool": "create_lead"}
        )

    assert result["allowed"] is True
    assert result["level"] == "L2"


# ─── probe (b): mint round-trip against a stubbed BFF ─────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse, captured: dict):
        self._response = response
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json, headers):
        self._captured["url"] = url
        self._captured["json"] = json
        self._captured["headers"] = headers
        return self._response


@pytest.mark.asyncio
async def test_create_payment_link_mint_round_trip(monkeypatch):
    from app.services import agent_tools

    monkeypatch.setenv("PAYMENT_COLLECTION_LIVE", "true")
    monkeypatch.setenv("BFF_API_BASE_URL", "https://bff.epic.dm")
    monkeypatch.setenv("BFF_INTERNAL_SECRET", "test-secret")

    captured: dict = {}
    response = _FakeResponse(200, {
        "ok": True,
        "token": "tok",
        "url": "https://isola.epic.dm/pay/link/tok",
        "expiresAt": "2026-07-02T00:00:00.000Z",
    })

    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(response, captured)):
        result = await agent_tools._create_payment_link(
            uuid.UUID(EMA_AGENT_ID),
            "tenant-abc",
            {"amount": 25.0, "currency": "usd", "description": "Invoice #4"},
        )

    assert "https://isola.epic.dm/pay/link/tok" in result
    assert captured["url"] == "https://bff.epic.dm/api/internal/payment-link"
    # PR #16 contract: amount, currency, description, tenantId, agentId, createdBy
    assert captured["json"] == {
        "amount": 25.0,
        "currency": "USD",  # normalized upper-case, matches BFF's VALID_CURRENCIES set
        "description": "Invoice #4",
        "tenantId": "tenant-abc",
        "agentId": EMA_AGENT_ID,
        "createdBy": f"ema-agent:{EMA_AGENT_ID}",
    }
    assert captured["headers"]["x-internal-secret"] == "test-secret"


@pytest.mark.asyncio
async def test_create_payment_link_surfaces_bff_error_without_faking_a_link(monkeypatch):
    from app.services import agent_tools

    monkeypatch.setenv("PAYMENT_COLLECTION_LIVE", "true")
    captured: dict = {}
    response = _FakeResponse(400, {"error": "amount must be a number between 5.00 and 500.00"})

    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(response, captured)):
        result = await agent_tools._create_payment_link(
            uuid.UUID(EMA_AGENT_ID), "tenant-abc", {"amount": 25.0, "currency": "USD"},
        )

    assert "http" not in result
    assert "amount must be" in result


@pytest.mark.asyncio
async def test_create_payment_link_refuses_when_collection_not_live(monkeypatch):
    """Item 3 of the dispatch: refuse honestly, never fake a link, when
    collection isn't live -- checked here without even reaching the BFF."""
    from app.services import agent_tools

    monkeypatch.delenv("PAYMENT_COLLECTION_LIVE", raising=False)  # unset -> default false

    with patch("httpx.AsyncClient") as mock_client:
        result = await agent_tools._create_payment_link(
            uuid.UUID(EMA_AGENT_ID), "tenant-abc", {"amount": 25.0, "currency": "USD"},
        )
        mock_client.assert_not_called()

    assert "isn't live" in result
    assert "http" not in result


@pytest.mark.asyncio
async def test_create_payment_link_rejects_out_of_range_amount(monkeypatch):
    from app.services import agent_tools

    monkeypatch.setenv("PAYMENT_COLLECTION_LIVE", "true")

    with patch("httpx.AsyncClient") as mock_client:
        result = await agent_tools._create_payment_link(
            uuid.UUID(EMA_AGENT_ID), "tenant-abc", {"amount": 4.99, "currency": "USD"},
        )
        mock_client.assert_not_called()

    assert "5.00 and 500.00" in result


# ─── probe (c): non-EMA agent doesn't get the tool ────────────────────────


@pytest.mark.asyncio
async def test_create_payment_link_refuses_non_ema_agent(monkeypatch):
    from app.services import agent_tools

    monkeypatch.setenv("PAYMENT_COLLECTION_LIVE", "true")
    other_agent_id = uuid.uuid4()
    assert str(other_agent_id) != EMA_AGENT_ID

    with patch("httpx.AsyncClient") as mock_client:
        result = await agent_tools._create_payment_link(
            other_agent_id, "tenant-abc", {"amount": 25.0, "currency": "USD"},
        )
        mock_client.assert_not_called()

    assert "isn't enabled" in result


def test_ema_agent_is_the_default_allowlist_entry():
    from app.services.agent_tools import _PAYMENT_LINK_ALLOWED_AGENT_IDS

    assert _PAYMENT_LINK_ALLOWED_AGENT_IDS == frozenset({EMA_AGENT_ID})


def test_allowlist_is_configurable_via_env(monkeypatch):
    """Ops can widen/replace the allowlist without a code change once a
    second agent is approved for payment links."""
    monkeypatch.setenv("PAYMENT_LINK_ALLOWED_AGENT_IDS", "aaaa,bbbb")
    import importlib
    from app.services import agent_tools as mod

    importlib.reload(mod)
    try:
        assert mod._PAYMENT_LINK_ALLOWED_AGENT_IDS == frozenset({"aaaa", "bbbb"})
    finally:
        monkeypatch.delenv("PAYMENT_LINK_ALLOWED_AGENT_IDS", raising=False)
        importlib.reload(mod)


# ─── tenant-binding guard (dispatch item 3) ───────────────────────────────


def test_tool_autonomy_map_uses_dedicated_payment_key():
    """Money gets its own gate -- must not reuse access_business_system_write."""
    from app.services.agent_tools import _TOOL_AUTONOMY_MAP

    assert _TOOL_AUTONOMY_MAP["create_payment_link"] == "access_payment_collection"
    assert _TOOL_AUTONOMY_MAP["create_payment_link"] != _TOOL_AUTONOMY_MAP["create_lead"]


def test_access_payment_collection_is_enforced_wave1():
    from app.api.admin_crossstore import _AUTONOMY_ENFORCED_WAVE1, _AUTONOMY_KEY_WHITELIST

    assert "access_payment_collection" in _AUTONOMY_KEY_WHITELIST
    assert "access_payment_collection" in _AUTONOMY_ENFORCED_WAVE1
