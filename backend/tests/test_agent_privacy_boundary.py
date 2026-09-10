"""Privacy boundary for a shared agent: synthetic owner vs synthetic staff.

These tests exist because a shared table was mistaken for a shared context and
vice versa. They demonstrate the boundary empirically, with two synthetic
identities and one synthetic agent — never a real staff record, never a real
phone number.

Scenario throughout:

    agent   "COO"        created by OWNER
    OWNER   synthetic    the person whose WhatsApp conversation is private
    STAFF   synthetic    org_admin, granted company-scope 'use' on the agent

Four properties:
    1. owner-only history is not visible to staff
    2. a staff principal is refused an owner-level action
    3. retrieval is scoped by principal, not by agent
    4. revocation is honoured on the next request
"""

import uuid
from types import SimpleNamespace

import pytest

from app.core import session_privacy as sp

TENANT = uuid.uuid4()
AGENT_ID = uuid.uuid4()
OWNER_ID = uuid.uuid4()
STAFF_ID = uuid.uuid4()


def _agent():
    return SimpleNamespace(id=AGENT_ID, name="COO", creator_id=OWNER_ID, tenant_id=TENANT)


def _owner():
    return SimpleNamespace(id=OWNER_ID, role="org_admin", tenant_id=TENANT, is_active=True)


def _staff():
    # org_admin on purpose: 297 of this estate's users hold that role, and the
    # pre-repair code let any of them read every session on any agent.
    return SimpleNamespace(id=STAFF_ID, role="org_admin", tenant_id=TENANT, is_active=True)


def _session(user_id, visibility=sp.VISIBILITY_PRIVATE, channel="whatsapp"):
    return SimpleNamespace(
        id=uuid.uuid4(), agent_id=AGENT_ID, user_id=user_id,
        source_channel=channel, visibility=visibility, is_group=False,
        title="Private thread",
    )


class FakeResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None


class FakeDB:
    """Answers each select() from a per-entity table. No database, no writes."""

    def __init__(self, permissions=(), agents=(), users=()):
        self.tables = {
            "agent_permissions": list(permissions),
            "agents": list(agents),
            "users": list(users),
        }
        self.reads = 0

    async def execute(self, statement):
        self.reads += 1
        name = statement.column_descriptions[0]["entity"].__tablename__
        return FakeResult(self.tables.get(name, []))


def _perm(scope_type, access_level, scope_id=None):
    return SimpleNamespace(
        agent_id=AGENT_ID, scope_type=scope_type,
        scope_id=scope_id, access_level=access_level,
    )


# ── 1. owner-only history is not visible to staff ────────────────────────────

@pytest.mark.asyncio
async def test_owner_private_session_is_invisible_to_staff():
    db = FakeDB(permissions=[_perm("company", "use")])
    staff_level = await sp.resolve_access_level(db, _staff(), _agent())
    assert staff_level == sp.USE

    owner_thread = _session(OWNER_ID)
    assert sp.can_read_session(_owner(), owner_thread, sp.MANAGE) is True
    assert sp.can_read_session(_staff(), owner_thread, staff_level) is False


@pytest.mark.asyncio
async def test_manage_access_still_does_not_open_a_private_session():
    """The sharp edge: 'manage' is a configuration right, not a reading right."""
    db = FakeDB(permissions=[_perm("company", "manage")])
    staff_level = await sp.resolve_access_level(db, _staff(), _agent())
    assert staff_level == sp.MANAGE
    assert sp.can_read_session(_staff(), _session(OWNER_ID), staff_level) is False
    # ... but an explicitly shared session is readable with manage, and only then.
    shared = _session(OWNER_ID, visibility=sp.VISIBILITY_SHARED)
    assert sp.can_read_session(_staff(), shared, sp.MANAGE) is True
    assert sp.can_read_session(_staff(), shared, sp.USE) is False


# ── 2. a staff principal is refused an owner-level action ────────────────────

@pytest.mark.asyncio
async def test_staff_principal_refused_owner_level_tool():
    db = FakeDB(
        permissions=[_perm("company", "use")],
        agents=[_agent()],
        users=[_staff()],
    )
    with pytest.raises(sp.PrincipalDenied) as exc:
        await sp.assert_tool_allowed(db, "send_payment_link", AGENT_ID, STAFF_ID)
    assert "owner-level action" in exc.value.detail

    # A non-owner-level tool is untouched by this gate.
    await sp.assert_tool_allowed(db, "list_files", AGENT_ID, STAFF_ID)


@pytest.mark.asyncio
async def test_agent_id_is_not_an_acceptable_principal():
    """execute_tool(..., user_id=user_id or agent_id) used to smuggle this in."""
    db = FakeDB(agents=[_agent()], users=[])
    with pytest.raises(sp.PrincipalDenied):
        await sp.assert_tool_allowed(db, "send_payment_link", AGENT_ID, AGENT_ID)
    with pytest.raises(sp.PrincipalDenied):
        await sp.assert_tool_allowed(db, "send_payment_link", AGENT_ID, None)


@pytest.mark.asyncio
async def test_owner_principal_is_allowed_the_same_action():
    db = FakeDB(permissions=[], agents=[_agent()], users=[_owner()])
    await sp.assert_tool_allowed(db, "send_payment_link", AGENT_ID, OWNER_ID)


# ── 3. retrieval is scoped by principal, not by agent ────────────────────────

@pytest.mark.asyncio
async def test_listing_is_scoped_to_the_principal_not_the_agent():
    db = FakeDB(permissions=[_perm("company", "use")])
    staff_level = await sp.resolve_access_level(db, _staff(), _agent())
    all_sessions_on_agent = [
        _session(OWNER_ID),
        _session(OWNER_ID, visibility=sp.VISIBILITY_SHARED),
        _session(STAFF_ID),
    ]
    visible = sp.filter_readable(_staff(), all_sessions_on_agent, staff_level)
    assert [s.user_id for s in visible] == [STAFF_ID]

    owner_visible = sp.filter_readable(_owner(), all_sessions_on_agent, sp.MANAGE)
    assert [s.user_id for s in owner_visible] == [OWNER_ID, OWNER_ID]


def test_readable_clause_matches_the_predicate():
    clause_use = str(sp.readable_sessions_clause(_staff(), sp.USE))
    assert "chat_sessions.user_id" in clause_use
    assert "visibility" not in clause_use
    clause_manage = str(sp.readable_sessions_clause(_staff(), sp.MANAGE))
    assert "visibility" in clause_manage
    denied = str(sp.readable_sessions_clause(_staff(), None))
    assert "IS NULL" in denied.upper()


# ── 4. revocation is honoured on the next request ────────────────────────────

@pytest.mark.asyncio
async def test_revoking_the_permission_row_denies_the_next_request():
    db = FakeDB(permissions=[_perm("user", "manage", scope_id=STAFF_ID)],
                agents=[_agent()], users=[_staff()])
    assert await sp.resolve_access_level(db, _staff(), _agent()) == sp.MANAGE

    db.tables["agent_permissions"] = []          # access withdrawn
    assert await sp.resolve_access_level(db, _staff(), _agent()) is None
    with pytest.raises(sp.PrincipalDenied):
        await sp.assert_tool_allowed(db, "send_payment_link", AGENT_ID, STAFF_ID)
    assert sp.filter_readable(_staff(), [_session(STAFF_ID)], None) == []


@pytest.mark.asyncio
async def test_deactivating_the_identity_denies_the_next_request():
    staff = _staff()
    db = FakeDB(permissions=[_perm("company", "manage")], agents=[_agent()], users=[staff])
    assert await sp.resolve_access_level(db, staff, _agent()) == sp.MANAGE
    staff.is_active = False
    assert await sp.resolve_access_level(db, staff, _agent()) is None


@pytest.mark.asyncio
async def test_resolution_is_a_live_read_every_time():
    """No caching: an in-flight turn cannot outrun a revocation."""
    db = FakeDB(permissions=[_perm("company", "use")])
    await sp.resolve_access_level(db, _staff(), _agent())
    await sp.resolve_access_level(db, _staff(), _agent())
    assert db.reads == 2


@pytest.mark.asyncio
async def test_cross_tenant_is_refused_regardless_of_permission_rows():
    outsider = SimpleNamespace(id=uuid.uuid4(), role="org_admin",
                               tenant_id=uuid.uuid4(), is_active=True)
    db = FakeDB(permissions=[_perm("company", "manage")])
    assert await sp.resolve_access_level(db, outsider, _agent()) is None
