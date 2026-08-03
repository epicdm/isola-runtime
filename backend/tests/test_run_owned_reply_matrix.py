# ISOLA GLUE — additive test (not upstream)
"""Deterministic concurrency/security/regression matrix for run-scoped
assistant-reply ownership (`dec-clawith-run-scoped-assistant-reply-
ownership-2026-08-03`), closing `defect-clawith-assistant-reply-selection-
not-run-scoped-2026-08-03`.

Real, disposable local Postgres only (`_require_local_database`, same skip
convention as `test_isola_structured_bridge_claim_isolation.py`). No
production database, no real LLM: every Run's terminal/waiting delivery is
produced by calling the REAL `deliver_runtime_message` service directly
(the same function the actual runtime worker calls after a graph finishes),
never a fake string. Adversarial receipts are constructed by writing directly
to `agent_run_events`/`chat_messages` -- the only way to represent a
corrupted or forged receipt, since the real service never produces one.

`read_run_owned_reply` has NO in-process state of any kind -- every call is
an independent, durable Postgres read. A "multi-process" or "process
restart" proof therefore does not need real OS subprocesses (unlike
`isola_structured_bridge_requests`'s claim race, which is a genuine
INSERT-race hazard `test_isola_structured_bridge_concurrency.py` proves with
real subprocesses): running many concurrent calls from `asyncio.gather`
against the same real Postgres already exercises the only shared arbiter
this function has -- the database -- with no in-process cache/singleton to
defeat that subprocess isolation could additionally rule out.

SCOPE NOTE on "structured + legacy overlap": `read_run_owned_reply` takes
no route/bridge parameter at all -- an `AgentRun` row created by
`isola_bridge.py` and one created by `isola_bridge_structured.py` are
structurally indistinguishable to it once both exist. The crossover-
prevention tests below therefore prove cross-route safety directly: two
runs sharing one session behave identically whether you imagine them as
"two structured turns" or "one legacy turn and one structured turn" -- there
is no code path that could tell the difference. The endpoint-level wiring
that feeds this helper the right `run_id`/`session_id`/`agent_id`/`user_id`
per route is separately proven by `test_isola_bridge_structured.py`'s
monkeypatched-collaborator tests (they assert the exact arguments each
route passes through).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.config import get_settings
from app.database import async_session
from app.database import engine as _sqlalchemy_engine
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.tenant import Tenant
from app.models.user import User
from app.services.agent_runtime.delivery import DeliveryRequest, deliver_runtime_message
from app.services.agent_runtime.run_owned_reply import (
    RunOwnedReply,
    RunOwnedReplyError,
    read_run_owned_reply,
)


@pytest.fixture(scope="module", autouse=True)
def _require_local_database():
    dsn = get_settings().DATABASE_URL
    allowed_hosts = ("localhost", "127.0.0.1", "@postgres:", "//postgres:")
    if not any(host in dsn for host in allowed_hosts):
        pytest.skip(f"refusing to run against non-local DATABASE_URL host: {dsn}")


def _asyncpg_dsn() -> str:
    return get_settings().DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


@pytest.fixture(autouse=True)
async def _fresh_loop_bound_engine():
    # pytest-asyncio (function-scoped loops) gives each test its own event
    # loop; app.database.engine is a module-level singleton whose pooled
    # asyncpg connections are bound to whichever loop created them. Dispose
    # at the start of every test so this test's connections are fresh.
    await _sqlalchemy_engine.dispose()
    yield
    await _sqlalchemy_engine.dispose()


# ── Seeding ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Scope:
    tenant_id: uuid.UUID
    model_id: uuid.UUID
    user_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID


async def _seed_scope(db) -> Scope:
    tenant_id = uuid.uuid4()
    model_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()

    # Flushed in explicit FK-dependency order (tenant -> model/user ->
    # agent -> session) rather than relying on the unit-of-work's automatic
    # table sort, which is not guaranteed across heterogeneous objects added
    # in one batch without ORM-level relationship() links between them.
    db.add(Tenant(id=tenant_id, name="Matrix Tenant", slug=f"matrix-{tenant_id.hex[:12]}"))
    await db.flush()

    db.add(
        LLMModel(
            id=model_id,
            tenant_id=tenant_id,
            provider="anthropic",
            model="claude-test",
            api_key_encrypted="encrypted",
            label="Matrix Model",
        )
    )
    db.add(User(id=user_id, tenant_id=tenant_id, display_name="Matrix Customer", role="member", is_active=True))
    await db.flush()

    db.add(
        Agent(
            id=agent_id,
            tenant_id=tenant_id,
            creator_id=user_id,
            name="Matrix Agent",
            avatar_url="agent.png",
            status="idle",
        )
    )
    await db.flush()

    db.add(
        ChatSession(
            id=session_id,
            tenant_id=tenant_id,
            session_type="direct",
            agent_id=agent_id,
            user_id=user_id,
            created_by_participant_id=None,
            title="Matrix Session",
            source_channel="web",
            is_primary=True,
        )
    )
    await db.flush()
    return Scope(tenant_id=tenant_id, model_id=model_id, user_id=user_id, agent_id=agent_id, session_id=session_id)


async def _seed_other_agent(db, scope: Scope) -> uuid.UUID:
    """A second, real Agent row in the SAME tenant -- ChatMessage.agent_id
    has a real foreign key, so an adversarial/doctored row still needs a
    row that actually exists in `agents`, not just an arbitrary UUID."""
    other_agent_id = uuid.uuid4()
    db.add(
        Agent(
            id=other_agent_id, tenant_id=scope.tenant_id, creator_id=scope.user_id,
            name="Matrix Other Agent", avatar_url="agent.png", status="idle",
        )
    )
    await db.flush()
    return other_agent_id


async def _seed_other_user(db, scope: Scope) -> uuid.UUID:
    """A second, real User row in the SAME tenant -- ChatMessage.user_id has
    a real foreign key."""
    other_user_id = uuid.uuid4()
    db.add(
        User(
            id=other_user_id, tenant_id=scope.tenant_id, display_name="Matrix Other Customer",
            role="member", is_active=True,
        )
    )
    await db.flush()
    return other_user_id


async def _make_run(db, scope: Scope, *, session_id: uuid.UUID | None = None) -> AgentRun:
    run_id = uuid.uuid4()
    run = AgentRun(
        id=run_id,
        tenant_id=scope.tenant_id,
        agent_id=scope.agent_id,
        session_id=session_id if session_id is not None else scope.session_id,
        source_type="chat",
        origin_user_id=scope.user_id,
        goal="Answer the customer's turn",
        run_kind="foreground",
        model_id=scope.model_id,
        runtime_type="langgraph",
        runtime_thread_id=str(run_id),
        graph_name="matrix_graph",
        graph_version="v1",
        lane_held=False,
        delivery_status="pending",
        delivery_target=None,
    )
    db.add(run)
    await db.flush()
    return run


async def _deliver(db, run: AgentRun, *, kind: str, content: str, lifecycle_status=None, interrupt_id=None):
    """Calls the REAL production delivery service -- never a fake string."""
    request = DeliveryRequest(
        tenant_id=run.tenant_id,
        run_id=run.id,
        kind=kind,
        content=content,
        checkpoint_id=None if kind == "ack" else f"checkpoint-{kind}",
        lifecycle_status=lifecycle_status,
        interrupt_id=interrupt_id,
    )
    return await deliver_runtime_message(db, request)


async def _read(scope: Scope, run: AgentRun, *, session_id=None, agent_id=None, user_id=None) -> RunOwnedReply | None:
    async with async_session() as db:
        return await read_run_owned_reply(
            db,
            tenant_id=scope.tenant_id,
            run_id=run.id,
            session_id=session_id if session_id is not None else (run.session_id or scope.session_id),
            agent_id=agent_id if agent_id is not None else scope.agent_id,
            user_id=user_id if user_id is not None else scope.user_id,
        )


# ── Crossover prevention: the exact defect this design closes ──────────────


@pytest.mark.asyncio
async def test_two_concurrent_runs_same_session_each_get_own_reply_forward_order():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run_a = await _make_run(db, scope)
        run_b = await _make_run(db, scope)
        await _deliver(db, run_a, kind="terminal", content="Answer for A", lifecycle_status="completed")
        await _deliver(db, run_b, kind="terminal", content="Answer for B", lifecycle_status="completed")

    reply_a = await _read(scope, run_a)
    reply_b = await _read(scope, run_b)
    assert reply_a is not None and reply_a.content == "Answer for A"
    assert reply_b is not None and reply_b.content == "Answer for B"


@pytest.mark.asyncio
async def test_two_concurrent_runs_same_session_each_get_own_reply_reversed_completion_order():
    """Same as forward order, but B's delivery is written to the database
    BEFORE A's -- the newest ChatMessage in the session is B's, yet A's
    lookup must still return A's own reply, never B's (this is exactly the
    session-plus-time query's failure mode)."""
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run_a = await _make_run(db, scope)
        run_b = await _make_run(db, scope)
        # B completes and delivers FIRST.
        await _deliver(db, run_b, kind="terminal", content="Answer for B", lifecycle_status="completed")
        await _deliver(db, run_a, kind="terminal", content="Answer for A", lifecycle_status="completed")

    reply_a = await _read(scope, run_a)
    reply_b = await _read(scope, run_b)
    assert reply_a is not None and reply_a.content == "Answer for A"
    assert reply_b is not None and reply_b.content == "Answer for B"


@pytest.mark.asyncio
async def test_unrelated_newer_assistant_message_in_session_does_not_leak_into_older_run():
    """A newer, unrelated assistant ChatMessage exists in the same session
    (e.g. from a later, completely separate run) -- the older run's lookup
    must still return its OWN reply, never the newer unrelated one."""
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run_old = await _make_run(db, scope)
        await _deliver(db, run_old, kind="terminal", content="Old run's answer", lifecycle_status="completed")

        run_new = await _make_run(db, scope)
        await _deliver(db, run_new, kind="terminal", content="Newer unrelated answer", lifecycle_status="completed")

    reply_old = await _read(scope, run_old)
    assert reply_old is not None
    assert reply_old.content == "Old run's answer"


@pytest.mark.asyncio
async def test_multiprocess_style_concurrency_many_runs_in_one_session_no_crossover():
    """`read_run_owned_reply` has zero in-process state -- every call is an
    independent Postgres read -- so concurrent `asyncio.gather` calls against
    the same real database already exercise the only shared arbiter this
    function has. 25 runs share one session; every reader must get exactly
    its own run's reply."""
    n = 25
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        runs = [await _make_run(db, scope) for _ in range(n)]
        for i, run in enumerate(runs):
            await _deliver(db, run, kind="terminal", content=f"Answer #{i}", lifecycle_status="completed")

    results = await asyncio.gather(*[_read(scope, run) for run in runs])
    for i, reply in enumerate(results):
        assert reply is not None
        assert reply.content == f"Answer #{i}"


# ── Ordering: terminal-over-waiting, identical timestamps ──────────────────


@pytest.mark.asyncio
async def test_waiting_delivery_accepted_when_no_terminal_exists():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        await _deliver(
            db, run, kind="waiting", content="What's your account number?",
            lifecycle_status="waiting_user", interrupt_id="interrupt-1",
        )

    reply = await _read(scope, run)
    assert reply is not None
    assert reply.delivery_kind == "waiting"
    assert reply.content == "What's your account number?"


@pytest.mark.asyncio
async def test_terminal_preferred_over_waiting_when_both_exist():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        await _deliver(
            db, run, kind="waiting", content="Identity check first",
            lifecycle_status="waiting_user", interrupt_id="interrupt-1",
        )
        await _deliver(db, run, kind="terminal", content="Final answer", lifecycle_status="completed")

    reply = await _read(scope, run)
    assert reply is not None
    assert reply.delivery_kind == "terminal"
    assert reply.content == "Final answer"


@pytest.mark.asyncio
async def test_ack_only_delivery_is_not_treated_as_the_turn_answer():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        await _deliver(db, run, kind="ack", content="Working on it...")

    reply = await _read(scope, run)
    assert reply is None


@pytest.mark.asyncio
async def test_two_terminal_receipts_with_identical_created_at_pick_deterministically():
    """Two run-owned assistant messages with identical `created_at` (a
    duplicate/retry delivery race), each a fully valid receipt in its own
    right (correct uuid5 identity, correct session): the winner must be
    picked by a deterministic id tie-break, and repeated lookups must
    always agree on the SAME one."""
    same_instant = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)

        idem_key_a = f"run:{run.id}:terminal:completed:a"
        idem_key_b = f"run:{run.id}:terminal:completed:b"
        message_a_id = uuid.uuid5(run.id, f"delivery-message:{idem_key_a}")
        message_b_id = uuid.uuid5(run.id, f"delivery-message:{idem_key_b}")

        for message_id, content in ((message_a_id, "Candidate A"), (message_b_id, "Candidate B")):
            db.add(
                ChatMessage(
                    id=message_id, agent_id=scope.agent_id, user_id=scope.user_id, role="assistant",
                    content=content, conversation_id=str(scope.session_id),
                    created_at=same_instant,
                )
            )
        for message_id, idem_key in ((message_a_id, idem_key_a), (message_b_id, idem_key_b)):
            db.add(
                AgentRunEvent(
                    id=uuid.uuid4(), run_id=run.id, tenant_id=scope.tenant_id,
                    agent_id=scope.agent_id, event_type="delivery_succeeded",
                    summary="Runtime delivery succeeded",
                    payload={
                        "version": 1, "status": "delivered", "delivery_kind": "terminal",
                        "message_id": str(message_id), "actual_session_id": str(scope.session_id),
                        "lifecycle_status": "completed",
                    },
                    idempotency_key=idem_key, created_at=same_instant,
                )
            )

    first = await _read(scope, run)
    second = await _read(scope, run)
    assert first is not None and second is not None
    assert first.message_id in (message_a_id, message_b_id)
    # Deterministic: repeated reads of the identical, unchanged data always
    # agree on which of the two tied candidates wins.
    assert first.message_id == second.message_id
    assert first.content == second.content


# ── Missing / malformed receipts fail closed, never fall back ──────────────


@pytest.mark.asyncio
async def test_completed_run_with_no_delivery_event_returns_none_not_an_error():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)

    reply = await _read(scope, run)
    assert reply is None


@pytest.mark.asyncio
async def test_doctored_message_id_fails_the_uuid5_assertion():
    """The receipt's payload.message_id was tampered with -- it no longer
    equals uuid5(run_id, 'delivery-message:' + idempotency_key)."""
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        idem_key = f"run:{run.id}:terminal:completed"
        forged_message_id = uuid.uuid4()  # NOT uuid5(run.id, "delivery-message:" + idem_key)
        db.add(
            ChatMessage(
                id=forged_message_id, agent_id=scope.agent_id, role="assistant",
                content="Forged content", conversation_id=str(scope.session_id),
            )
        )
        db.add(
            AgentRunEvent(
                id=uuid.uuid4(), run_id=run.id, tenant_id=scope.tenant_id,
                agent_id=scope.agent_id, event_type="delivery_succeeded",
                summary="Runtime delivery succeeded",
                payload={
                    "version": 1, "status": "delivered", "delivery_kind": "terminal",
                    "message_id": str(forged_message_id), "actual_session_id": str(scope.session_id),
                    "lifecycle_status": "completed",
                },
                idempotency_key=idem_key,
            )
        )

    with pytest.raises(RunOwnedReplyError) as exc_info:
        await _read(scope, run)
    assert exc_info.value.code == "message_id_uuid5_mismatch"


@pytest.mark.asyncio
async def test_idempotency_key_wrong_run_prefix_fails_closed():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        other_run_id = uuid.uuid4()
        bad_idem_key = f"run:{other_run_id}:terminal:completed"
        message_id = uuid.uuid5(run.id, f"delivery-message:{bad_idem_key}")
        db.add(
            ChatMessage(
                id=message_id, agent_id=scope.agent_id, role="assistant",
                content="Mismatched prefix", conversation_id=str(scope.session_id),
            )
        )
        db.add(
            AgentRunEvent(
                id=uuid.uuid4(), run_id=run.id, tenant_id=scope.tenant_id,
                agent_id=scope.agent_id, event_type="delivery_succeeded",
                summary="Runtime delivery succeeded",
                payload={
                    "version": 1, "status": "delivered", "delivery_kind": "terminal",
                    "message_id": str(message_id), "actual_session_id": str(scope.session_id),
                    "lifecycle_status": "completed",
                },
                idempotency_key=bad_idem_key,
            )
        )

    with pytest.raises(RunOwnedReplyError) as exc_info:
        await _read(scope, run)
    assert exc_info.value.code == "idempotency_key_run_prefix_mismatch"


@pytest.mark.asyncio
async def test_receipt_referencing_message_in_wrong_session_fails_closed():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        wrong_session_id = uuid.uuid4()
        idem_key = f"run:{run.id}:terminal:completed"
        message_id = uuid.uuid5(run.id, f"delivery-message:{idem_key}")
        db.add(
            ChatMessage(
                id=message_id, agent_id=scope.agent_id, role="assistant",
                content="Wrong session", conversation_id=str(wrong_session_id),
            )
        )
        db.add(
            AgentRunEvent(
                id=uuid.uuid4(), run_id=run.id, tenant_id=scope.tenant_id,
                agent_id=scope.agent_id, event_type="delivery_succeeded",
                summary="Runtime delivery succeeded",
                payload={
                    "version": 1, "status": "delivered", "delivery_kind": "terminal",
                    "message_id": str(message_id),
                    # actual_session_id in the receipt is the WRONG session too.
                    "actual_session_id": str(wrong_session_id),
                    "lifecycle_status": "completed",
                },
                idempotency_key=idem_key,
            )
        )

    with pytest.raises(RunOwnedReplyError) as exc_info:
        await _read(scope, run)
    assert exc_info.value.code == "receipt_session_mismatch"


@pytest.mark.asyncio
async def test_receipt_referencing_wrong_role_message_fails_closed():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        idem_key = f"run:{run.id}:terminal:completed"
        message_id = uuid.uuid5(run.id, f"delivery-message:{idem_key}")
        db.add(
            ChatMessage(
                id=message_id, agent_id=scope.agent_id, user_id=scope.user_id, role="user",
                content="This is a user message, not an assistant reply",
                conversation_id=str(scope.session_id),
            )
        )
        db.add(
            AgentRunEvent(
                id=uuid.uuid4(), run_id=run.id, tenant_id=scope.tenant_id,
                agent_id=scope.agent_id, event_type="delivery_succeeded",
                summary="Runtime delivery succeeded",
                payload={
                    "version": 1, "status": "delivered", "delivery_kind": "terminal",
                    "message_id": str(message_id), "actual_session_id": str(scope.session_id),
                    "lifecycle_status": "completed",
                },
                idempotency_key=idem_key,
            )
        )

    with pytest.raises(RunOwnedReplyError) as exc_info:
        await _read(scope, run)
    assert exc_info.value.code == "message_wrong_role"


@pytest.mark.asyncio
async def test_receipt_referencing_message_with_wrong_agent_id_fails_closed():
    """Regression test for an adversarial-review finding: a
    delivery_succeeded receipt can carry a correct run-id/idempotency-key/
    uuid5 binding while the ChatMessage row it names was authored for a
    DIFFERENT agent (a corrupted or doctored row). The message's own
    agent_id must be checked, not just the re-read AgentRun's -- otherwise a
    scope-inconsistent row's content would still be returned."""
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        idem_key = f"run:{run.id}:terminal:completed"
        message_id = uuid.uuid5(run.id, f"delivery-message:{idem_key}")
        other_agent_id = await _seed_other_agent(db, scope)
        db.add(
            ChatMessage(
                id=message_id, agent_id=other_agent_id, user_id=scope.user_id, role="assistant",
                content="Belongs to a different agent",
                conversation_id=str(scope.session_id),
            )
        )
        db.add(
            AgentRunEvent(
                id=uuid.uuid4(), run_id=run.id, tenant_id=scope.tenant_id,
                agent_id=scope.agent_id, event_type="delivery_succeeded",
                summary="Runtime delivery succeeded",
                payload={
                    "version": 1, "status": "delivered", "delivery_kind": "terminal",
                    "message_id": str(message_id), "actual_session_id": str(scope.session_id),
                    "lifecycle_status": "completed",
                },
                idempotency_key=idem_key,
            )
        )

    with pytest.raises(RunOwnedReplyError) as exc_info:
        await _read(scope, run)
    assert exc_info.value.code == "message_agent_mismatch"


@pytest.mark.asyncio
async def test_receipt_referencing_message_with_wrong_user_id_fails_closed():
    """Same regression as above for the message's own user_id: a
    scope-inconsistent row must fail closed even when the run/idempotency/
    uuid5/session/role/content checks all pass."""
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        idem_key = f"run:{run.id}:terminal:completed"
        message_id = uuid.uuid5(run.id, f"delivery-message:{idem_key}")
        other_user_id = await _seed_other_user(db, scope)
        db.add(
            ChatMessage(
                id=message_id, agent_id=scope.agent_id, user_id=other_user_id, role="assistant",
                content="Belongs to a different customer",
                conversation_id=str(scope.session_id),
            )
        )
        db.add(
            AgentRunEvent(
                id=uuid.uuid4(), run_id=run.id, tenant_id=scope.tenant_id,
                agent_id=scope.agent_id, event_type="delivery_succeeded",
                summary="Runtime delivery succeeded",
                payload={
                    "version": 1, "status": "delivered", "delivery_kind": "terminal",
                    "message_id": str(message_id), "actual_session_id": str(scope.session_id),
                    "lifecycle_status": "completed",
                },
                idempotency_key=idem_key,
            )
        )

    with pytest.raises(RunOwnedReplyError) as exc_info:
        await _read(scope, run)
    assert exc_info.value.code == "message_user_mismatch"


@pytest.mark.asyncio
async def test_receipt_referencing_empty_content_message_fails_closed():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        idem_key = f"run:{run.id}:terminal:completed"
        message_id = uuid.uuid5(run.id, f"delivery-message:{idem_key}")
        db.add(
            ChatMessage(
                id=message_id, agent_id=scope.agent_id, role="assistant",
                content="   ", conversation_id=str(scope.session_id),
            )
        )
        db.add(
            AgentRunEvent(
                id=uuid.uuid4(), run_id=run.id, tenant_id=scope.tenant_id,
                agent_id=scope.agent_id, event_type="delivery_succeeded",
                summary="Runtime delivery succeeded",
                payload={
                    "version": 1, "status": "delivered", "delivery_kind": "terminal",
                    "message_id": str(message_id), "actual_session_id": str(scope.session_id),
                    "lifecycle_status": "completed",
                },
                idempotency_key=idem_key,
            )
        )

    with pytest.raises(RunOwnedReplyError) as exc_info:
        await _read(scope, run)
    assert exc_info.value.code == "message_empty_content"


@pytest.mark.asyncio
async def test_message_referenced_by_receipt_but_never_persisted_fails_closed():
    """The receipt is otherwise well-formed (correct uuid5 identity, correct
    actual_session_id) but the ChatMessage row it names was never written --
    a torn/partial write. Must fail closed, never resolve to any OTHER
    message in the session."""
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        idem_key = f"run:{run.id}:terminal:completed"
        message_id = uuid.uuid5(run.id, f"delivery-message:{idem_key}")
        # No ChatMessage row is ever inserted for message_id.
        db.add(
            AgentRunEvent(
                id=uuid.uuid4(), run_id=run.id, tenant_id=scope.tenant_id,
                agent_id=scope.agent_id, event_type="delivery_succeeded",
                summary="Runtime delivery succeeded",
                payload={
                    "version": 1, "status": "delivered", "delivery_kind": "terminal",
                    "message_id": str(message_id), "actual_session_id": str(scope.session_id),
                    "lifecycle_status": "completed",
                },
                idempotency_key=idem_key,
            )
        )

    with pytest.raises(RunOwnedReplyError) as exc_info:
        await _read(scope, run)
    assert exc_info.value.code == "message_not_found"


# ── Tenant / agent / user ownership isolation ───────────────────────────────


@pytest.mark.asyncio
async def test_cross_tenant_lookup_is_rejected():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        await _deliver(db, run, kind="terminal", content="Real answer", lifecycle_status="completed")

    # The receipt query's own WHERE clause is scoped by tenant_id, so a
    # wrong tenant_id makes the run's real delivery event invisible at the
    # SQL level -- the fail-closed outcome here is "not found" (None),
    # exactly like a run that has not delivered yet. It never falls through
    # to any other tenant's data.
    other_tenant_id = uuid.uuid4()
    async with async_session() as db:
        result = await read_run_owned_reply(
            db, tenant_id=other_tenant_id, run_id=run.id,
            session_id=scope.session_id, agent_id=scope.agent_id, user_id=scope.user_id,
        )
    assert result is None


@pytest.mark.asyncio
async def test_cross_agent_lookup_is_rejected():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        await _deliver(db, run, kind="terminal", content="Real answer", lifecycle_status="completed")

    other_agent_id = uuid.uuid4()
    with pytest.raises(RunOwnedReplyError) as exc_info:
        await _read(scope, run, agent_id=other_agent_id)
    # Caught by the message-level ownership check (added as a defence-in-
    # depth hardening) before the AgentRun re-read is even reached -- both
    # are valid fail-closed outcomes for this scenario.
    assert exc_info.value.code == "message_agent_mismatch"


@pytest.mark.asyncio
async def test_cross_user_lookup_is_rejected():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        await _deliver(db, run, kind="terminal", content="Real answer", lifecycle_status="completed")

    other_user_id = uuid.uuid4()
    with pytest.raises(RunOwnedReplyError) as exc_info:
        await _read(scope, run, user_id=other_user_id)
    # Caught by the message-level ownership check (added as a defence-in-
    # depth hardening) before the AgentRun re-read is even reached -- both
    # are valid fail-closed outcomes for this scenario.
    assert exc_info.value.code == "message_user_mismatch"


@pytest.mark.asyncio
async def test_cross_session_lookup_is_rejected():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        await _deliver(db, run, kind="terminal", content="Real answer", lifecycle_status="completed")

    other_session_id = uuid.uuid4()
    with pytest.raises(RunOwnedReplyError):
        await _read(scope, run, session_id=other_session_id)


# ── Process-restart-style replay: durable DB ownership is sufficient ───────


@pytest.mark.asyncio
async def test_process_restart_style_replay_is_stable_across_fresh_sessions_and_engine_disposal():
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        await _deliver(db, run, kind="terminal", content="Stable answer", lifecycle_status="completed")

    first = await _read(scope, run)
    # Simulates a process restart: dispose the pooled engine entirely (no
    # connection, no ORM identity map survives) before reading again.
    await _sqlalchemy_engine.dispose()
    second = await _read(scope, run)

    assert first is not None and second is not None
    assert first.message_id == second.message_id
    assert first.content == second.content == "Stable answer"


@pytest.mark.asyncio
async def test_replay_of_the_same_claim_never_produces_different_reasoning():
    """A structured-bridge-style replay: reading the same run-owned reply
    twice must return the identical message id both times -- no new
    reasoning, no new enqueue, no drift."""
    async with async_session() as db, db.begin():
        scope = await _seed_scope(db)
        run = await _make_run(db, scope)
        await _deliver(db, run, kind="terminal", content="Only answer", lifecycle_status="completed")

    replies = await asyncio.gather(*[_read(scope, run) for _ in range(10)])
    message_ids = {reply.message_id for reply in replies if reply is not None}
    assert len(replies) == 10
    assert all(reply is not None for reply in replies)
    assert message_ids == {replies[0].message_id}
