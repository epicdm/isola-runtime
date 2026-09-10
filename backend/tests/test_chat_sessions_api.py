import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import chat_sessions as chat_sessions_api


class DummyResult:
    def __init__(self, values=None, scalar_value=None):
        self._values = list(values or [])
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        if self._values:
            return self._values[0]
        return self._scalar_value

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def scalar(self):
        if self._scalar_value is not None:
            return self._scalar_value
        return self._values[0] if self._values else None


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.statements = []
        self.added = []
        self.committed = False
        self.refreshed = []

    async def execute(self, _statement, _params=None):
        self.statements.append(_statement)
        if not self.responses:
            raise AssertionError("unexpected execute() call")
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def refresh(self, value):
        self.refreshed.append(value)


@pytest.mark.asyncio
async def test_org_admin_role_alone_does_not_grant_scope_all(monkeypatch):
    """REWRITTEN. This test previously asserted the leak and pinned it in place.

    `org_admin` is held by 297 of 330 principals on this estate. Role is not
    permission. scope=all is now a `manage` right on THIS agent, resolved live
    from agent_permissions; a principal resolved to `use` is refused outright.
    """
    viewer_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid.uuid4())
    current_user = SimpleNamespace(id=viewer_id, role="org_admin", is_active=True)

    db = RecordingDB(responses=[DummyResult([agent])])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"          # what agent_permissions actually resolves to

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc:
        await chat_sessions_api.list_sessions(
            agent_id=agent_id, scope="all", current_user=current_user, db=db
        )
    assert exc.value.status_code == 403
    # and nothing was read: refusal happened before any session query
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_scope_all_at_manage_is_narrowed_to_readable_sessions(monkeypatch):
    """`manage` opens scope=all, but the SQL is narrowed by the privacy clause.

    A manager must not receive another principal's PRIVATE session. Proven by
    compiling the emitted statement and asserting the clause is present.
    """
    manager_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid.uuid4())
    current_user = SimpleNamespace(id=manager_id, role="member", is_active=True)

    db = RecordingDB(responses=[DummyResult([agent]), DummyResult([])])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    out = await chat_sessions_api.list_sessions(
        agent_id=agent_id, scope="all", current_user=current_user, db=db
    )
    assert out == []
    sql = str(db.statements[-1].compile(compile_kwargs={"literal_binds": False}))
    assert "visibility" in sql, sql          # shared-only branch present
    assert "user_id" in sql, sql             # own-sessions branch present


@pytest.mark.asyncio
async def test_creator_can_list_all_sessions(monkeypatch):
    creator_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user = SimpleNamespace(id=creator_id, role="member")
    agent = SimpleNamespace(id=agent_id, creator_id=creator_id)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=other_user_id,
        source_channel="web",
        title="Customer follow-up",
        created_at=now,
        last_message_at=now,
        peer_agent_id=None,
        is_group=False,
        group_name=None,
    )
    db = RecordingDB(
        responses=[
            DummyResult([agent]),
            DummyResult([session]),
            DummyResult([(str(session.id), 2)]),
            DummyResult([(other_user_id, "Bob")]),
        ]
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    sessions = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="all",
        current_user=current_user,
        db=db,
    )

    # The creator resolves to `manage`, so scope=all is permitted -- but the
    # rows returned are whatever readable_sessions_clause admits. This fake DB
    # cannot filter, so the assertion here is on ADMISSION only; the narrowing
    # itself is proven in test_scope_all_at_manage_is_narrowed_to_readable_sessions
    # and in can_read_session's unit tests.
    assert len(sessions) == 1
    assert sessions[0].user_id == str(other_user_id)
    assert sessions[0].username == "Bob"


@pytest.mark.asyncio
async def test_org_admin_cannot_view_other_users_private_session_messages(monkeypatch):
    viewer_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user = SimpleNamespace(id=viewer_id, role="org_admin", is_active=True)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=owner_id,
        source_channel="web",
        visibility="private",
    )
    message = SimpleNamespace(
        role="user",
        content="hello",
        created_at=now,
        participant_id=None,
    )
    db = RecordingDB(
        responses=[
            DummyResult([session]),
            DummyResult([message]),
        ]
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    # REWRITTEN: this previously asserted that an org_admin reads the owner's
    # messages. It is now a refusal, and `manage` would be refused too --
    # manage is a configuration right, not a reading right over private
    # conversations.
    with pytest.raises(HTTPException) as exc:
        await chat_sessions_api.get_session_messages(
            agent_id=agent_id,
            session_id=session_id,
            current_user=current_user,
            db=db,
        )
    assert exc.value.status_code == 403
    assert db.responses, "messages must not have been queried"


@pytest.mark.asyncio
async def test_creator_at_manage_cannot_read_a_private_session(monkeypatch):
    creator_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user = SimpleNamespace(id=creator_id, role="member", is_active=True)
    agent = SimpleNamespace(id=agent_id, creator_id=creator_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=other_user_id,
        source_channel="web",
        visibility="private",
    )
    message = SimpleNamespace(
        role="user",
        content="hello",
        created_at=now,
        participant_id=None,
    )
    db = RecordingDB(
        responses=[
            DummyResult([session]),
            DummyResult([message]),
        ]
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc:
        await chat_sessions_api.get_session_messages(
            agent_id=agent_id,
            session_id=session_id,
            current_user=current_user,
            db=db,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_manage_may_read_an_explicitly_shared_session(monkeypatch):
    """The other half of the law: shared sessions ARE readable at manage."""
    manager_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user = SimpleNamespace(id=manager_id, role="member", is_active=True)
    agent = SimpleNamespace(id=agent_id, creator_id=manager_id)
    session = SimpleNamespace(
        id=session_id, agent_id=agent_id, peer_agent_id=None,
        user_id=other_user_id, source_channel="agent", visibility="shared",
    )
    message = SimpleNamespace(role="user", content="hello", created_at=now, participant_id=None)
    db = RecordingDB(responses=[DummyResult([session]), DummyResult([message])])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    messages = await chat_sessions_api.get_session_messages(
        agent_id=agent_id, session_id=session_id, current_user=current_user, db=db,
    )
    assert messages[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_create_session_returns_web_session_shape(monkeypatch):
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    current_user = SimpleNamespace(id=user_id, role="member")
    db = RecordingDB()

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    session = await chat_sessions_api.create_session(
        agent_id=agent_id,
        current_user=current_user,
        db=db,
    )

    assert session.agent_id == str(agent_id)
    assert session.user_id == str(user_id)
    assert session.source_channel == "web"
    assert session.participant_type == "user"
    assert session.is_group is False
    assert db.committed is True
    assert len(db.added) == 1
