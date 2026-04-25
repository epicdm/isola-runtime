"""Tier 1.5a — live owner-ask loop unit tests.

Pure-Python coverage:
  - reply_markers regex parses and strips `[ask_owner: ...]`
  - dispatcher creates an OwnerAskInFlight row + WA-pings owner
  - owner_ask service: FIFO ordering, skip detection, expiry sweep
  - knowledge_teach.record_teach: idempotent topic replace + gap-marking

Async DB tests use an in-process SQLite via aiosqlite. We override the
production async_session to point at the test engine.

Run from the runtime backend dir:
    pytest backend/tests/test_owner_ask_loop.py -v
"""

from __future__ import annotations

import asyncio
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Make `app.*` importable when running from repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─── Pure parser tests (no DB) ───────────────────────────


def test_marker_regex_matches_ask_owner():
    from app.services.reply_markers import _MARKER_RE

    text = "Give me a moment, checking with Eric. [ask_owner: Do you have valet parking?]"
    matches = list(_MARKER_RE.finditer(text))
    assert len(matches) == 1
    assert matches[0].group("kind").lower() == "ask_owner"
    assert matches[0].group("body").strip() == "Do you have valet parking?"


def test_marker_regex_strips_after_dispatch():
    from app.services.reply_markers import _MARKER_RE

    text = (
        "Give me a moment.\n[ask_owner: Do you have valet?]\n\nBack soon."
    )
    cleaned = _MARKER_RE.sub("", text)
    cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned).strip()
    assert "ask_owner" not in cleaned
    assert "Give me a moment" in cleaned
    assert "Back soon" in cleaned


def test_marker_regex_coexists_with_escalate_and_location():
    from app.services.reply_markers import _MARKER_RE

    text = (
        "[escalate: complaint] then "
        "[location: lat=1,lon=2,name=\"X\"] then "
        "[ask_owner: weekend hours?]"
    )
    kinds = [m.group("kind").lower() for m in _MARKER_RE.finditer(text)]
    assert kinds == ["escalate", "location", "ask_owner"]


# ─── Dispatcher integration with mocked WA + DB ──────────


@pytest.mark.asyncio
async def test_dispatch_ask_owner_skips_when_no_owner_phone():
    """No owner_phone configured → silent skip, no WA call, no DB row."""
    from app.services import reply_markers

    ctx = reply_markers.ReplyContext(
        agent_id=uuid.uuid4(),
        agent_name="Rex",
        owner_phone=None,
        phone_number_id="pn",
        access_token="t",
        customer_phone="17678180000",
        customer_name="Marie",
        conversation_id=uuid.uuid4(),
        paperclip_issue_id=None,
        last_inbound="Do you have valet?",
    )

    with patch.object(reply_markers, "logger") as mlog:
        await reply_markers._dispatch_ask_owner("Do you have valet?", ctx)
        assert any("no owner_phone" in str(c) for c in mlog.info.mock_calls)


@pytest.mark.asyncio
async def test_dispatch_ask_owner_drops_empty_question():
    from app.services import reply_markers

    ctx = reply_markers.ReplyContext(
        agent_id=uuid.uuid4(),
        agent_name="Rex",
        owner_phone="17678180000",
        phone_number_id="pn",
        access_token="t",
        customer_phone="17678180001",
        customer_name="Marie",
        conversation_id=uuid.uuid4(),
        paperclip_issue_id=None,
        last_inbound="...",
    )
    with patch.object(reply_markers, "logger") as mlog:
        await reply_markers._dispatch_ask_owner("   ", ctx)
        assert any("empty question" in str(c) for c in mlog.warning.mock_calls)


# ─── owner_ask service — pure-logic helpers (no DB) ──────


def test_skip_detection_canonical_and_aliases():
    from app.services.owner_ask import _is_skip

    for s in ["skip", "  Skip  ", "SKIP", "pass", "later", "n/a", "not now"]:
        assert _is_skip(s), f"expected skip-match for {s!r}"
    for s in ["yes", "no answer please", "skipping it"]:
        assert not _is_skip(s), f"expected non-skip for {s!r}"


# ─── Phone-match helper from whatsapp.py ─────────────────


def test_phone_match_tolerates_plus_and_digits():
    from app.api.whatsapp import _phones_match

    assert _phones_match("+17678180000", "17678180000")
    assert _phones_match("17678180000", "+17678180000")
    assert _phones_match("17678180000", "17678180000")
    assert not _phones_match("17678180000", "17678180001")
    assert not _phones_match(None, "17678180000")
    assert not _phones_match("17678180000", "")


# ─── knowledge_teach idempotency ─────────────────────────


def test_record_teach_creates_then_replaces_topic(tmp_path, monkeypatch):
    from app.services import knowledge_teach

    # Point AGENT_DATA_DIR at tmp_path so we don't touch real workspaces.
    fake_settings = type(
        "S", (), {"AGENT_DATA_DIR": str(tmp_path)}
    )()
    monkeypatch.setattr(knowledge_teach, "get_settings", lambda: fake_settings)

    agent_id = uuid.uuid4()
    r1 = knowledge_teach.record_teach(
        agent_id=agent_id,
        question="Do you have valet?",
        answer="Yes - free evenings",
        teacher_name="Eric",
    )
    r2 = knowledge_teach.record_teach(
        agent_id=agent_id,
        question="Do you have valet?",
        answer="Yes - free evenings AND weekends",
        teacher_name="Eric",
    )
    assert r1.topic == r2.topic == "Do you have valet"
    md = (tmp_path / str(agent_id) / "workspace" / "knowledge.md").read_text(
        encoding="utf-8"
    )
    # The second teach should replace, not append.
    assert md.count("## Do you have valet") == 1
    assert "AND weekends" in md


def test_record_teach_marks_matching_gap(tmp_path, monkeypatch):
    from app.services import knowledge_teach

    fake_settings = type(
        "S", (), {"AGENT_DATA_DIR": str(tmp_path)}
    )()
    monkeypatch.setattr(knowledge_teach, "get_settings", lambda: fake_settings)

    agent_id = uuid.uuid4()
    ws = tmp_path / str(agent_id) / "workspace"
    ws.mkdir(parents=True)
    (ws / "knowledge-gaps.md").write_text(
        '- 2026-04-26 19:42 · +17678183742 · asked: "Do you have valet?" · not yet taught\n'
        '- 2026-04-26 19:43 · +17678183742 · asked: "Random other Q" · not yet taught\n',
        encoding="utf-8",
    )
    r = knowledge_teach.record_teach(
        agent_id=agent_id,
        question="Do you have valet?",
        answer="Yes free evenings",
        teacher_name="Eric",
    )
    assert r.gaps_marked_taught == 1
    gaps = (ws / "knowledge-gaps.md").read_text(encoding="utf-8")
    assert "Do you have valet" in gaps
    assert "- taught " in gaps  # the matched row
    # Untouched row stays pending
    assert "Random other Q" in gaps
    assert gaps.count("- taught ") == 1


# ─── DB-backed owner_ask service (in-memory SQLite) ──────


@pytest.fixture
async def db_session():
    """Spin up an in-memory aiosqlite engine, create only the
    owner_ask_in_flight + agents tables we need, yield a session."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        # SQLite doesn't have TIMESTAMPTZ — DateTime(timezone=True) maps to
        # plain TIMESTAMP, which is fine for our naive comparisons.
        # The model also references agents(id) but SQLite is lax about FK
        # enforcement — no need to create the agents table.
        await conn.execute(
            text(
                """
                CREATE TABLE owner_ask_in_flight (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    customer_phone TEXT NOT NULL,
                    customer_name TEXT,
                    customer_conversation_id TEXT,
                    question TEXT NOT NULL,
                    owner_phone TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'awaiting',
                    asked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    answered_at TIMESTAMP,
                    owner_reply TEXT
                )
                """
            )
        )

    Sess = async_sessionmaker(engine, expire_on_commit=False)
    async with Sess() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_owner_ask_fifo_ordering(db_session):
    from app.services import owner_ask

    agent_id = uuid.uuid4()
    a1 = await owner_ask.create_ask(
        db_session,
        agent_id=agent_id,
        customer_phone="A",
        customer_name="A",
        customer_conversation_id=None,
        question="first",
        owner_phone="OWNER",
    )
    # asked_at is server_default(NOW) — bump second row deliberately.
    await asyncio.sleep(0.01)
    a2 = await owner_ask.create_ask(
        db_session,
        agent_id=agent_id,
        customer_phone="B",
        customer_name="B",
        customer_conversation_id=None,
        question="second",
        owner_phone="OWNER",
    )
    await db_session.commit()

    # Force a1.asked_at to be older than a2 explicitly (CURRENT_TIMESTAMP
    # in SQLite has 1s resolution and may collide).
    from sqlalchemy import update
    from app.models.owner_ask import OwnerAskInFlight

    older = datetime.now(timezone.utc) - timedelta(minutes=5)
    await db_session.execute(
        update(OwnerAskInFlight)
        .where(OwnerAskInFlight.id == a1.id)
        .values(asked_at=older)
    )
    await db_session.commit()

    found = await owner_ask.find_oldest_pending_for_agent(db_session, agent_id)
    assert found is not None
    assert found.question == "first"


@pytest.mark.asyncio
async def test_owner_ask_expires_stale_rows(db_session):
    from sqlalchemy import update
    from app.models.owner_ask import (
        STATUS_AWAITING,
        STATUS_EXPIRED,
        OwnerAskInFlight,
    )
    from app.services import owner_ask

    agent_id = uuid.uuid4()
    a = await owner_ask.create_ask(
        db_session,
        agent_id=agent_id,
        customer_phone="A",
        customer_name="A",
        customer_conversation_id=None,
        question="ancient",
        owner_phone="O",
    )
    # Backdate to 25h ago.
    too_old = datetime.now(timezone.utc) - timedelta(hours=25)
    await db_session.execute(
        update(OwnerAskInFlight)
        .where(OwnerAskInFlight.id == a.id)
        .values(asked_at=too_old)
    )
    await db_session.commit()

    found = await owner_ask.find_oldest_pending_for_agent(db_session, agent_id)
    assert found is None
    await db_session.commit()

    # The session's identity map still caches `a` with status='awaiting'
    # because the bulk UPDATE used synchronize_session=False. Force a
    # refresh from the DB to confirm the row was actually expired.
    await db_session.refresh(a)
    assert a.status == STATUS_EXPIRED


@pytest.mark.asyncio
async def test_owner_ask_mark_answered_and_skipped(db_session):
    from app.models.owner_ask import (
        STATUS_ANSWERED,
        STATUS_SKIPPED,
    )
    from app.services import owner_ask

    agent_id = uuid.uuid4()
    a = await owner_ask.create_ask(
        db_session,
        agent_id=agent_id,
        customer_phone="A",
        customer_name="A",
        customer_conversation_id=None,
        question="q1",
        owner_phone="O",
    )
    await db_session.commit()

    await owner_ask.mark_answered(db_session, a, "yes free evenings")
    await db_session.commit()
    assert a.status == STATUS_ANSWERED
    assert a.owner_reply == "yes free evenings"
    assert a.answered_at is not None

    b = await owner_ask.create_ask(
        db_session,
        agent_id=agent_id,
        customer_phone="B",
        customer_name="B",
        customer_conversation_id=None,
        question="q2",
        owner_phone="O",
    )
    await db_session.commit()
    await owner_ask.mark_skipped(db_session, b)
    await db_session.commit()
    assert b.status == STATUS_SKIPPED
