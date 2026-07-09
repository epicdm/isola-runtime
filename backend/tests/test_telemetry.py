"""S6 TDD: telemetry write — compute_cost (pure) + write_telemetry_row (integration).

Substrate constants from S0a probe (2026-05-22):
  Bull agent:    7cc35bb0-0de5-49c9-b43b-511fd7b6bbba
  Bull tenant:   df034033-b80f-4732-94eb-f29c06630b93
  deepseek id:   8db68763-c43f-4e38-b5b9-82e3c4b8887c (cost=0.11¢/1k output)

Integration test writes a real row, reads it back, then deletes it.
asyncio_mode=auto (pyproject.toml) handles coroutine tests automatically.
"""
import asyncio
from decimal import Decimal
import pytest
from sqlalchemy import text

from app.services.lcr import LcrResult, PoolModel
from app.services.telemetry import compute_cost, write_telemetry_row


# ── Fixtures ──────────────────────────────────────────────────────────────────

DEEPSEEK = PoolModel(
    id="8db68763-c43f-4e38-b5b9-82e3c4b8887c",
    provider="deepseek", model="deepseek-chat", tier="standard",
    enabled=True, health_status="healthy",
    latency_p95_ms=2000, cost_per_1k_output_cents=0.11,
    capability_tags=["text", "multilingual", "reasoning", "cite_sources"],
)
QWEN_CONTENT = PoolModel(
    id="cb8da9b6-5891-4d22-ab2c-2df7cf8122e4",
    provider="ollama", model="qwen2.5:3b-instruct-q4_K_M", tier="free-content",
    enabled=True, health_status="healthy",
    latency_p95_ms=20000, cost_per_1k_output_cents=0,
    capability_tags=["text", "multilingual"],
)

# Substrate: Bull agent + tenant (both have solo plan + verified tenant_subscription)
BULL_AGENT_ID = "7cc35bb0-0de5-49c9-b43b-511fd7b6bbba"
BULL_TENANT_ID = "df034033-b80f-4732-94eb-f29c06630b93"


# ── compute_cost (pure function) ──────────────────────────────────────────────

def test_compute_cost_deepseek_350_output_tokens():
    """350 output * 0.11¢/1k = 0.038500¢"""
    cost = compute_cost(DEEPSEEK, 350)
    assert cost == Decimal("0.038500")


def test_compute_cost_deepseek_1000_output_tokens():
    """1000 output * 0.11¢/1k = 0.110000¢"""
    cost = compute_cost(DEEPSEEK, 1000)
    assert cost == Decimal("0.110000")


def test_compute_cost_ollama_is_always_zero():
    """Ollama: cost_per_1k_output_cents=0 → any token count → 0."""
    assert compute_cost(QWEN_CONTENT, 5000) == Decimal("0.000000")


def test_compute_cost_zero_tokens():
    assert compute_cost(DEEPSEEK, 0) == Decimal("0.000000")


def test_compute_cost_fractional_precision():
    """Result is quantized to 6 decimal places."""
    cost = compute_cost(DEEPSEEK, 1)   # 0.11 / 1000 = 0.00011
    assert cost == Decimal("0.000110")


# ── LcrResult.fallback_used property ─────────────────────────────────────────

def test_lcr_result_fallback_used_both_false():
    result = LcrResult(model=DEEPSEEK, degraded=False, safety_override=False)
    assert result.fallback_used is False


def test_lcr_result_fallback_used_degraded_only():
    result = LcrResult(model=DEEPSEEK, degraded=True, safety_override=False)
    assert result.fallback_used is True


def test_lcr_result_fallback_used_safety_only():
    result = LcrResult(model=DEEPSEEK, degraded=False, safety_override=True)
    assert result.fallback_used is True


def test_lcr_result_fallback_used_both_true():
    result = LcrResult(model=DEEPSEEK, degraded=True, safety_override=True)
    assert result.fallback_used is True


# ── Engine disposal fixture (per-test loop isolation) ────────────────────────

@pytest.fixture(autouse=True)
async def _dispose_engine():
    """Dispose AsyncEngine after each test so the next test's fresh event loop
    gets new connections instead of inheriting stale pool futures."""
    yield
    try:
        from app.database import engine
        await engine.dispose()
    except Exception:
        pass


# ── write_telemetry_row (integration — real DB) ───────────────────────────────

async def test_write_telemetry_row_populates_all_fields():
    """Writes a row to llm_call_telemetry and reads it back.
    Cleans up after itself (DELETE WHERE created_at matches the test row id).
    """
    from app.database import async_session

    cost = compute_cost(DEEPSEEK, 350)

    await write_telemetry_row(
        tenant_id=BULL_TENANT_ID,
        agent_id=BULL_AGENT_ID,
        intent="greeting",
        difficulty="low",
        selected_model_id=DEEPSEEK.id,
        selected_provider=DEEPSEEK.provider,
        selected_model_name=DEEPSEEK.model,
        byo_used=False,
        fallback_used=False,
        input_tokens=200,
        output_tokens=350,
        cost_cents=cost,
        cost_billed_to="epic",
        latency_ms=1300,
        success=True,
        error_class=None,
        classified_by="rules",
        classifier_confidence=Decimal("1.0"),
    )

    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT id, intent, difficulty, selected_provider, selected_model,"
                "       byo_used, fallback_used, input_tokens, output_tokens,"
                "       cost_cents, cost_billed_to, latency_ms, success,"
                "       classified_by, classifier_confidence"
                " FROM llm_call_telemetry"
                " WHERE tenant_id = :tid AND agent_id = :aid"
                "   AND classified_by = 'rules' AND intent = 'greeting'"
                " ORDER BY created_at DESC LIMIT 1"
            ),
            {"tid": BULL_TENANT_ID, "aid": BULL_AGENT_ID},
        )
        row = result.fetchone()

    assert row is not None, "telemetry row not found after write"
    assert row.intent == "greeting"
    assert row.difficulty == "low"
    assert row.selected_provider == "deepseek"
    assert row.selected_model == "deepseek-chat"
    assert row.byo_used is False
    assert row.fallback_used is False
    assert row.input_tokens == 200
    assert row.output_tokens == 350
    assert Decimal(str(row.cost_cents)) == cost
    assert row.cost_billed_to == "epic"
    assert row.latency_ms == 1300
    assert row.success is True
    assert row.classified_by == "rules"
    assert Decimal(str(row.classifier_confidence)) == Decimal("1.0")

    # Cleanup: remove the test row
    async with async_session() as session:
        await session.execute(
            text("DELETE FROM llm_call_telemetry WHERE id = :id"),
            {"id": str(row.id)},
        )
        await session.commit()


async def test_write_telemetry_row_fallback_used_true_for_degraded():
    """Verify fallback_used=True is stored when LCR degradation applied."""
    from app.database import async_session

    await write_telemetry_row(
        tenant_id=BULL_TENANT_ID,
        agent_id=BULL_AGENT_ID,
        intent="greeting",
        difficulty="low",
        selected_model_id=QWEN_CONTENT.id,
        selected_provider=QWEN_CONTENT.provider,
        selected_model_name=QWEN_CONTENT.model,
        byo_used=False,
        fallback_used=True,      # degradation applied
        input_tokens=50,
        output_tokens=80,
        cost_cents=Decimal("0.000000"),
        cost_billed_to="epic",
        latency_ms=18000,
        success=True,
        error_class=None,
        classified_by="rules",
        classifier_confidence=Decimal("1.0"),
    )

    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT id, fallback_used, selected_provider"
                " FROM llm_call_telemetry"
                " WHERE tenant_id = :tid AND agent_id = :aid"
                "   AND fallback_used = true AND output_tokens = 80"
                " ORDER BY created_at DESC LIMIT 1"
            ),
            {"tid": BULL_TENANT_ID, "aid": BULL_AGENT_ID},
        )
        row = result.fetchone()

    assert row is not None
    assert row.fallback_used is True
    assert row.selected_provider == "ollama"

    async with async_session() as session:
        await session.execute(
            text("DELETE FROM llm_call_telemetry WHERE id = :id"),
            {"id": str(row.id)},
        )
        await session.commit()


async def test_write_telemetry_exception_does_not_propagate():
    """Bad tenant_id → DB constraint error → logged warning, no crash."""
    await write_telemetry_row(
        tenant_id="00000000-0000-0000-0000-000000000000",  # nonexistent FK
        agent_id="00000000-0000-0000-0000-000000000000",
        intent="greeting", difficulty="low",
        selected_model_id=None, selected_provider="deepseek",
        selected_model_name="deepseek-chat",
        byo_used=False, fallback_used=False,
        input_tokens=0, output_tokens=0,
        cost_cents=Decimal("0"), cost_billed_to="epic",
        latency_ms=None, success=True, error_class=None,
        classified_by="rules", classifier_confidence=None,
    )
    # Reaches here without raising — warning logged internally
