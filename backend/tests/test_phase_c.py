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

    # Now provision the same tenant with roles=['rex','mara'] — missing
    # Mara template should produce 404, not partial create.
    r = await http.post(
        "/api/agents/provision-vertical",
        headers=test_state["headers"],
        json={"vertical": "hotel", "roles": ["rex", "mara"]},
    )
    assert r.status_code == 404, f"expected 404 for missing Mara template, got {r.status_code}: {r.text}"
    detail = r.json().get("detail", "")
    assert "mara" in str(detail).lower()

    # And unknown vertical -> 404 with descriptive message.
    r = await http.post(
        "/api/agents/provision-vertical",
        headers=test_state["headers"],
        json={"vertical": "submarine", "roles": ["rex"]},
    )
    assert r.status_code == 404


async def test_phase_c_chain(http, test_state, db_session):
    """C.1 dependent chain: templates seeded -> provision-vertical works.

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
