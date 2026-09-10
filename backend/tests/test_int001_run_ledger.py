"""Acceptance tests for the INT-001 run ledger against REAL PostgreSQL.

Idempotency and the state machine are database properties, so they are tested
against a real server. SQLite would not exercise ON CONFLICT the same way and
would not exercise concurrency at all.

Run with:
    INT001_TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host/db \
        pytest tests/test_int001_run_ledger.py

Skipped when that variable is unset, so it never fails a CI box that has no
database rather than pretending to pass.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import select, text, update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.paperclip_run import PaperclipRun
from app.services.run_bounds import (
    ALL_STATES,
    STATE_ACCEPTED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_TIMED_OUT,
)

DB_URL = os.environ.get("INT001_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DB_URL, reason="INT001_TEST_DATABASE_URL unset")

COMPANY = "48f327a1-244e-4b58-ae1e-8222b6472794"
AGENT = "e1a4a504-17a2-4020-9303-7057e77ae0c9"


@pytest.fixture
async def sessionmaker_fixture():
    engine = create_async_engine(DB_URL, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(PaperclipRun.metadata.create_all, tables=[PaperclipRun.__table__])
        await conn.execute(text("TRUNCATE paperclip_runs"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


def _claim_stmt(run_id: str):
    return (
        pg_insert(PaperclipRun)
        .values(
            id=uuid.uuid4(),
            run_id=run_id,
            paperclip_agent_id=AGENT,
            paperclip_company_id=COMPANY,
            issue_id="issue-1",
            state=STATE_ACCEPTED,
            principal_label="isola.service.int001",
        )
        .on_conflict_do_nothing(index_elements=["run_id"])
        .returning(PaperclipRun.id)
    )


@pytest.mark.asyncio
async def test_concurrent_duplicate_delivery_yields_exactly_one_execution(
    sessionmaker_fixture,
):
    """Twenty simultaneous deliveries of one runId. Exactly one claims it."""
    run_id = f"run-{uuid.uuid4()}"
    executed: list[int] = []

    async def deliver(n: int) -> bool:
        async with sessionmaker_fixture() as db:
            result = await db.execute(_claim_stmt(run_id))
            won = result.first() is not None
            await db.commit()
            if won:
                executed.append(n)
            return won

    outcomes = await asyncio.gather(*(deliver(i) for i in range(20)))

    assert sum(1 for w in outcomes if w) == 1
    assert len(executed) == 1

    async with sessionmaker_fixture() as db:
        rows = (
            await db.execute(select(PaperclipRun).where(PaperclipRun.run_id == run_id))
        ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_comment_slot_is_claimed_exactly_once_under_concurrency(
    sessionmaker_fixture,
):
    """Two finishers racing cannot both post. The conditional UPDATE decides."""
    run_id = f"run-{uuid.uuid4()}"
    async with sessionmaker_fixture() as db:
        await db.execute(_claim_stmt(run_id))
        await db.commit()

    async def claim() -> bool:
        async with sessionmaker_fixture() as db:
            result = await db.execute(
                sql_update(PaperclipRun)
                .where(
                    PaperclipRun.run_id == run_id,
                    PaperclipRun.comment_posted.is_(False),
                )
                .values(comment_posted=True)
                .returning(PaperclipRun.id)
            )
            won = result.first() is not None
            await db.commit()
            return won

    results = await asyncio.gather(*(claim() for _ in range(10)))
    assert sum(1 for r in results if r) == 1


@pytest.mark.asyncio
async def test_all_five_states_persist_and_read_back(sessionmaker_fixture):
    assert set(ALL_STATES) == {
        STATE_ACCEPTED,
        STATE_RUNNING,
        STATE_COMPLETED,
        STATE_FAILED,
        STATE_TIMED_OUT,
    }
    for state in ALL_STATES:
        run_id = f"run-{state}-{uuid.uuid4()}"
        async with sessionmaker_fixture() as db:
            await db.execute(_claim_stmt(run_id))
            await db.commit()
            await db.execute(
                sql_update(PaperclipRun)
                .where(PaperclipRun.run_id == run_id)
                .values(state=state)
            )
            await db.commit()
            row = (
                await db.execute(
                    select(PaperclipRun).where(PaperclipRun.run_id == run_id)
                )
            ).scalar_one()
        assert row.state == state


@pytest.mark.asyncio
async def test_ledger_row_carries_the_correlation_identifiers(sessionmaker_fixture):
    """Correlation evidence (b): the runtime row names the same runId and the
    same paperclip_agent_id the Paperclip run record names."""
    run_id = f"run-{uuid.uuid4()}"
    async with sessionmaker_fixture() as db:
        await db.execute(_claim_stmt(run_id))
        await db.commit()
        row = (
            await db.execute(select(PaperclipRun).where(PaperclipRun.run_id == run_id))
        ).scalar_one()
    assert row.run_id == run_id
    assert row.paperclip_agent_id == AGENT
    assert row.paperclip_company_id == COMPANY
    assert row.principal_label == "isola.service.int001"
    assert row.principal_label != "creator"
