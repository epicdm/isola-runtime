"""Unit tests for registration_service org member binding."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.org import OrgMember
from app.services.registration_service import registration_service


class DummyResult:
    def __init__(self, scalar_value=None):
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        return self._scalar_value


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.added = []
        self.flushed = False

    async def execute(self, _statement, _params=None):
        if not self.responses:
            raise AssertionError("unexpected execute() call")
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True


def _make_user(*, tenant_id=None, email="user@example.com", display_name="Web User"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        email=email,
        primary_mobile=None,
        display_name=display_name,
    )


@pytest.mark.asyncio
async def test_bind_org_member_creates_web_member_when_missing():
    """Users created from web flows should get a web-backed OrgMember shell."""
    user = _make_user()
    provider = SimpleNamespace(id=uuid.uuid4())
    db = RecordingDB(
        responses=[
            DummyResult(None),  # existing member by user_id
            DummyResult(None),  # email match
        ]
    )

    with patch.object(
        registration_service,
        "ensure_identity_provider",
        new=AsyncMock(return_value=provider),
    ), patch(
        "app.services.okr_agent_hook.hook_new_org_member",
        new=AsyncMock(),
    ) as mock_hook:
        await registration_service.bind_org_member(db, user)

    created_members = [item for item in db.added if isinstance(item, OrgMember)]
    assert len(created_members) == 1
    member = created_members[0]
    assert member.provider_id == provider.id
    assert member.user_id == user.id
    assert member.name == "Web User"
    assert member.title == "Web User"
    mock_hook.assert_awaited_once()


@pytest.mark.asyncio
async def test_bind_org_member_upgrades_providerless_member_to_web():
    """Existing provider-less platform members should be categorized as web users."""
    user = _make_user(display_name="Alice Web")
    provider = SimpleNamespace(id=uuid.uuid4())
    member = OrgMember(
        name="Old Name",
        email=user.email,
        tenant_id=user.tenant_id,
        user_id=user.id,
        provider_id=None,
        title="",
        status="active",
    )
    db = RecordingDB(
        responses=[
            DummyResult(member),  # existing member by user_id
        ]
    )

    with patch.object(
        registration_service,
        "ensure_identity_provider",
        new=AsyncMock(return_value=provider),
    ), patch(
        "app.services.okr_agent_hook.hook_new_org_member",
        new=AsyncMock(),
    ) as mock_hook:
        await registration_service.bind_org_member(db, user)

    assert member.provider_id == provider.id
    assert member.name == "Alice Web"
    assert member.title == "Web User"
    mock_hook.assert_awaited_once()
