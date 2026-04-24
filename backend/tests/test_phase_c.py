"""Phase C vertical AgentTemplates — dependent integration tests.

Same pattern as tests/test_phase_b.py: one test function running the
phase chain on one event loop so module-scoped-ish state stays on one
loop. Each phase's assertions must pass before the next phase runs.

Chain for C.1:
  _phase_c1_templates_seeded
    - Seeded templates exist for (rex, restaurant), (rex, hotel),
      (rex, clinic). Upstream Clawith generic built-ins are gone.
    - GET /api/agents/templates?role=rex -> 3 rows, all vertical-tagged.
    - GET /api/agents/templates?vertical=restaurant -> includes the Rex
      restaurant template.
  _phase_c1_provision_vertical
    - POST /api/agents/provision-vertical body={vertical:'hotel',
      roles:['rex']} -> 201 with one created agent matching the
      Rex×Hotel template.
    - Agent row in DB carries template_id pointing at the template.
    - Subsequent GET /api/agents lists the new agent.
"""

from __future__ import annotations

import secrets as _secrets
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

# Populate Base.metadata — selecting Agent/AgentTemplate cross-references
# tenants, users, llm_models so those mappers must be loaded or
# sort_tables() raises NoReferencedTableError.
from app.models import tenant as _tenant  # noqa: F401
from app.models import user as _user  # noqa: F401
from app.models import llm as _llm  # noqa: F401
from app.models import channel_config as _channel_config  # noqa: F401
from app.models import agent as _agent  # noqa: F401
from app.models import chat_session as _chat_session  # noqa: F401
from app.models import audit as _audit  # noqa: F401
from app.models import participant as _participant  # noqa: F401


BACKEND = "http://localhost:8000"


@pytest.fixture
async def http():
    async with httpx.AsyncClient(base_url=BACKEND, timeout=30.0, follow_redirects=True) as client:
        yield client


@pytest.fixture
async def test_state(http):
    """Register a fresh user per test run + capture JWT + tenant id."""
    suffix = _secrets.token_hex(4)
    email = f"test-c-{suffix}@isola.dev"
    username = f"test-c-{suffix}"
    password = "TestPhaseC-2026!"

    r = await http.post("/api/auth/register", json={
        "email": email,
        "username": username,
        "password": password,
        "display_name": f"Phase C Test {suffix}",
        "tenant_name": f"Test Tenant {suffix}",
        "tenant_slug": f"test-tenant-c-{suffix}",
    })
    assert r.status_code in (200, 201), f"register failed: {r.status_code} {r.text}"
    data = r.json()
    jwt = data["access_token"]
    user_id = data["user_id"]

    headers = {"Authorization": f"Bearer {jwt}"}
    yield {
        "jwt": jwt,
        "headers": headers,
        "user_id": user_id,
        "suffix": suffix,
    }


@pytest.fixture
async def db_session():
    import os
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://isolaruntime:isolaruntime@postgres:5432/isolaruntime",
    )
    engine = create_async_engine(dsn, echo=False)
    Session = sessionmaker(
        engine,
        class_=__import__("sqlalchemy.ext.asyncio", fromlist=["AsyncSession"]).AsyncSession,
        expire_on_commit=False,
    )
    try:
        yield Session
    finally:
        await engine.dispose()


# ─── Phase-C chain (one test, dependent phases) ─────────────────────


async def _phase_c1_templates_seeded(http, test_state, db_session):
    """B.1: seeder has replaced upstream generics with Isola Rex × 3 verticals."""
    from app.models.agent import AgentTemplate

    Session = db_session
    # Direct DB check — no upstream generics left + 3 Isola Rex templates
    async with Session() as db:
        legacy_r = await db.execute(
            select(AgentTemplate).where(
                AgentTemplate.is_builtin == True,  # noqa: E712
                AgentTemplate.role.is_(None),
                AgentTemplate.vertical.is_(None),
            )
        )
        legacy = list(legacy_r.scalars().all())
        assert not legacy, f"upstream generic templates not cleared: {[t.name for t in legacy]}"

        isola_r = await db.execute(
            select(AgentTemplate).where(
                AgentTemplate.role == "rex",
                AgentTemplate.vertical.in_(["restaurant", "hotel", "clinic"]),
            )
        )
        isola = {t.vertical: t for t in isola_r.scalars().all()}
        assert set(isola.keys()) == {"restaurant", "hotel", "clinic"}, (
            f"expected 3 Rex verticals, got {sorted(isola.keys())}"
        )

    # API filter — role=rex yields exactly 3 results
    r = await http.get(
        "/api/agents/templates", headers=test_state["headers"], params={"role": "rex"}
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3, f"role=rex filter returned {len(rows)} rows"
    verticals_seen = sorted(row["vertical"] for row in rows)
    assert verticals_seen == ["clinic", "hotel", "restaurant"]
    for row in rows:
        assert row["role"] == "rex"
        assert row["is_builtin"] is True
        assert row["soul_template"], "template missing soul_template"

    # API filter — vertical=restaurant includes the Rex × Restaurant template
    r = await http.get(
        "/api/agents/templates",
        headers=test_state["headers"],
        params={"vertical": "restaurant"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert any(row["role"] == "rex" and row["vertical"] == "restaurant" for row in rows), (
        f"vertical=restaurant didn't return Rex × Restaurant: {[r['name'] for r in rows]}"
    )


async def _phase_c1_provision_vertical(http, test_state, db_session):
    """C.1 provisioning: POST /provision-vertical creates agents from templates."""
    from app.models.agent import Agent, AgentTemplate

    # Provision a Hotel tenant with a single Rex agent.
    r = await http.post(
        "/api/agents/provision-vertical",
        headers=test_state["headers"],
        json={
            "vertical": "hotel",
            "roles": ["rex"],
            "name_prefix": f"Villa {test_state['suffix']}",
        },
    )
    assert r.status_code == 201, f"provision failed: {r.status_code} {r.text}"
    out = r.json()
    assert out["vertical"] == "hotel"
    assert out["roles_requested"] == ["rex"]
    agents = out["agents"]
    assert len(agents) == 1
    ag = agents[0]
    assert ag["role"] == "rex"
    assert ag["vertical"] == "hotel"
    assert ag["name"].startswith(f"Villa {test_state['suffix']}")
    agent_id = ag["agent_id"]
    template_id = ag["template_id"]

    # DB: the created agent is tagged with the Rex × Hotel template.
    Session = db_session
    async with Session() as db:
        ag_r = await db.execute(select(Agent).where(Agent.id == uuid.UUID(agent_id)))
        agent = ag_r.scalar_one_or_none()
        assert agent is not None
        assert agent.template_id == uuid.UUID(template_id)
        assert agent.autonomy_policy  # non-empty — inherited from template

        tmpl_r = await db.execute(
            select(AgentTemplate).where(AgentTemplate.id == uuid.UUID(template_id))
        )
        tmpl = tmpl_r.scalar_one_or_none()
        assert tmpl is not None
        assert tmpl.role == "rex"
        assert tmpl.vertical == "hotel"

    # Missing-role still 404s (fail-fast, no partial create). Post-C.3
    # 'joey' is now seeded, so the gap role is 'cash' (C.5 adds it).
    r = await http.post(
        "/api/agents/provision-vertical",
        headers=test_state["headers"],
        json={"vertical": "hotel", "roles": ["rex", "cash"]},
    )
    assert r.status_code == 404, f"expected 404 for missing Cash template, got {r.status_code}: {r.text}"
    detail = r.json().get("detail", "")
    assert "cash" in str(detail).lower()

    # And unknown vertical -> 404 with descriptive message.
    r = await http.post(
        "/api/agents/provision-vertical",
        headers=test_state["headers"],
        json={"vertical": "submarine", "roles": ["rex"]},
    )
    assert r.status_code == 404


async def _phase_c2_mara_verticals(http, test_state, db_session):
    """C.2: Mara × Restaurant / Hotel / Clinic seeded + provisionable.

    Depends on C.1b (proves the provision endpoint works for one role).
    """
    from app.models.agent import Agent, AgentTemplate

    # DB: 3 Mara verticals present
    Session = db_session
    async with Session() as db:
        mara_r = await db.execute(
            select(AgentTemplate).where(
                AgentTemplate.role == "mara",
                AgentTemplate.vertical.in_(["restaurant", "hotel", "clinic"]),
            )
        )
        mara = {t.vertical: t for t in mara_r.scalars().all()}
        assert set(mara.keys()) == {"restaurant", "hotel", "clinic"}, (
            f"expected 3 Mara verticals, got {sorted(mara.keys())}"
        )
        # Category should be "marketing" — distinct from Rex's "front-desk"
        for t in mara.values():
            assert t.category == "marketing"
            assert t.is_builtin is True
        # Autonomy-policy check: Mara's send_external_message is L3
        # (never DMs customers without owner approval).
        for t in mara.values():
            assert t.default_autonomy_policy.get("send_external_message") == "L3", (
                f"{t.name}: send_external_message should be L3 for Mara "
                f"(got {t.default_autonomy_policy.get('send_external_message')})"
            )

    # API filter — role=mara yields 3
    r = await http.get(
        "/api/agents/templates", headers=test_state["headers"], params={"role": "mara"}
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3
    assert sorted(row["vertical"] for row in rows) == ["clinic", "hotel", "restaurant"]

    # API filter — vertical=restaurant now returns BOTH Rex and Mara
    r = await http.get(
        "/api/agents/templates",
        headers=test_state["headers"],
        params={"vertical": "restaurant"},
    )
    assert r.status_code == 200
    rows = r.json()
    roles_seen = sorted({row["role"] for row in rows if row["role"]})
    assert "rex" in roles_seen and "mara" in roles_seen, (
        f"vertical=restaurant should include both rex + mara, got {roles_seen}"
    )

    # Provisioning: roles=[rex, mara] against restaurant now creates 2 agents.
    r = await http.post(
        "/api/agents/provision-vertical",
        headers=test_state["headers"],
        json={
            "vertical": "restaurant",
            "roles": ["rex", "mara"],
            "name_prefix": f"Lolo {test_state['suffix']}",
        },
    )
    assert r.status_code == 201, f"provision failed: {r.status_code} {r.text}"
    out = r.json()
    created = out["agents"]
    assert len(created) == 2
    by_role = {a["role"]: a for a in created}
    assert set(by_role.keys()) == {"rex", "mara"}
    assert by_role["rex"]["vertical"] == "restaurant"
    assert by_role["mara"]["vertical"] == "restaurant"

    # DB: both agents point at their respective template_ids, with
    # inherited autonomy_policy (Mara's is distinctly tighter).
    async with Session() as db:
        mara_agent_r = await db.execute(
            select(Agent).where(Agent.id == uuid.UUID(by_role["mara"]["agent_id"]))
        )
        mara_agent = mara_agent_r.scalar_one_or_none()
        assert mara_agent is not None
        assert mara_agent.autonomy_policy.get("send_external_message") == "L3"


async def _phase_c3_joey_verticals(http, test_state, db_session):
    """C.3: Joey × Restaurant / Hotel / Clinic seeded + trio-provision works.

    Depends on C.2. Proves the full Rex + Mara + Joey trio provisions
    cleanly for every vertical.
    """
    from app.models.agent import Agent, AgentTemplate

    # DB: 3 Joey verticals present with sales category
    Session = db_session
    async with Session() as db:
        joey_r = await db.execute(
            select(AgentTemplate).where(
                AgentTemplate.role == "joey",
                AgentTemplate.vertical.in_(["restaurant", "hotel", "clinic"]),
            )
        )
        joey = {t.vertical: t for t in joey_r.scalars().all()}
        assert set(joey.keys()) == {"restaurant", "hotel", "clinic"}, (
            f"expected 3 Joey verticals, got {sorted(joey.keys())}"
        )
        for t in joey.values():
            assert t.category == "sales"
            assert t.is_builtin is True
            # Joey autonomy invariants:
            # - send_external_message L2 (DMs prospects live)
            # - financial_operations L3 (quotes always approved)
            # - access_business_system_write L3 (contracts always approved)
            pol = t.default_autonomy_policy
            assert pol.get("send_external_message") == "L2", (
                f"{t.name}: Joey should DM prospects (L2 send), got "
                f"{pol.get('send_external_message')}"
            )
            assert pol.get("financial_operations") == "L3"
            assert pol.get("access_business_system_write") == "L3"

    # API filter — role=joey yields 3
    r = await http.get(
        "/api/agents/templates", headers=test_state["headers"], params={"role": "joey"}
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3
    assert sorted(row["vertical"] for row in rows) == ["clinic", "hotel", "restaurant"]

    # API filter — vertical=hotel now includes rex + mara + joey (3 roles)
    r = await http.get(
        "/api/agents/templates",
        headers=test_state["headers"],
        params={"vertical": "hotel"},
    )
    assert r.status_code == 200
    rows = r.json()
    roles_seen = sorted({row["role"] for row in rows if row["role"]})
    assert "rex" in roles_seen and "mara" in roles_seen and "joey" in roles_seen, (
        f"vertical=hotel should include rex + mara + joey, got {roles_seen}"
    )

    # Full-trio provisioning for a clinic tenant.
    r = await http.post(
        "/api/agents/provision-vertical",
        headers=test_state["headers"],
        json={
            "vertical": "clinic",
            "roles": ["rex", "mara", "joey"],
            "name_prefix": f"Care {test_state['suffix']}",
        },
    )
    assert r.status_code == 201, f"trio provision failed: {r.status_code} {r.text}"
    out = r.json()
    created = out["agents"]
    assert len(created) == 3
    by_role = {a["role"]: a for a in created}
    assert set(by_role.keys()) == {"rex", "mara", "joey"}
    for role, ag in by_role.items():
        assert ag["vertical"] == "clinic"
        assert ag["name"].startswith(f"Care {test_state['suffix']}")

    # DB: Joey agent autonomy reflects L2 outbound + L3 financial
    async with Session() as db:
        joey_agent_r = await db.execute(
            select(Agent).where(Agent.id == uuid.UUID(by_role["joey"]["agent_id"]))
        )
        joey_agent = joey_agent_r.scalar_one_or_none()
        assert joey_agent is not None
        pol = joey_agent.autonomy_policy or {}
        assert pol.get("send_external_message") == "L2"
        assert pol.get("financial_operations") == "L3"


async def test_phase_c_chain(http, test_state, db_session):
    """C.1 -> C.2 -> C.3 dependent chain.

    Single test function so fixtures stay on one event loop (same
    pattern as test_phase_b.py). Each phase's assertions must pass
    before the next runs — fail-fast.
    """
    print("\n--- Phase C.1a: Isola templates seeded, upstream generics cleared ---")
    await _phase_c1_templates_seeded(http, test_state, db_session)
    print("Phase C.1a PASS")

    print("--- Phase C.1b: provision-vertical creates agents from templates ---")
    await _phase_c1_provision_vertical(http, test_state, db_session)
    print("Phase C.1b PASS")

    print("--- Phase C.2: Mara × 3 verticals seeded + provisionable ---")
    await _phase_c2_mara_verticals(http, test_state, db_session)
    print("Phase C.2 PASS")

    print("--- Phase C.3: Joey × 3 verticals + full rex+mara+joey trio provisioning ---")
    await _phase_c3_joey_verticals(http, test_state, db_session)
    print("Phase C.3 PASS")
