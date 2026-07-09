"""S8 TDD: pre-call cost estimator + max_cost_cents guardrail (pure functions).

All tests are synchronous — no DB, no async.

Substrate constants (S0a probe, 2026-05-22):
  deepseek-chat: cost_per_1k_output_cents=0.11, cost_per_1k_input_cents=0.027
  Ollama: both costs = 0
  max_cost_cents DB seed: greeting=10, domain=30-50 (all far above Phase 2 realistic costs)
  Tight cap (max_cost_cents=1) chosen for blocking test to keep message sizes realistic.

chars // 3 heuristic per Q6 grilling decision.
"""
from decimal import Decimal
import pytest
from app.services.lcr import PoolModel
from app.services.cost_estimator import (
    PreCallDecision,
    enforce_pre_call_cap,
    estimate_call_cost,
    estimate_tokens_from_chars,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

DEEPSEEK = PoolModel(
    id="8db68763-c43f-4e38-b5b9-82e3c4b8887c",
    provider="deepseek", model="deepseek-chat", tier="standard",
    enabled=True, health_status="healthy",
    latency_p95_ms=2000, cost_per_1k_output_cents=0.11,
    cost_per_1k_input_cents=0.027,
    capability_tags=["text", "multilingual", "reasoning", "cite_sources"],
)
OLLAMA = PoolModel(
    id="cb8da9b6-5891-4d22-ab2c-2df7cf8122e4",
    provider="ollama", model="qwen2.5:3b-instruct-q4_K_M", tier="free-content",
    enabled=True, health_status="healthy",
    latency_p95_ms=20000, cost_per_1k_output_cents=0,
    cost_per_1k_input_cents=0,
    capability_tags=["text", "multilingual"],
)


# ── estimate_tokens_from_chars ────────────────────────────────────────────────

def test_estimate_tokens_900_chars():
    """900 chars → 300 tokens (chars // 3 per Q6 grilling)."""
    assert estimate_tokens_from_chars("x" * 900) == 300


def test_estimate_tokens_empty():
    assert estimate_tokens_from_chars("") == 0


def test_estimate_tokens_short_floor_division():
    """2 chars → 0 tokens (floor division, not rounding)."""
    assert estimate_tokens_from_chars("hi") == 0


def test_estimate_tokens_exact_multiple():
    assert estimate_tokens_from_chars("x" * 3000) == 1000


def test_estimate_tokens_remainder_truncated():
    """100 chars → 33 tokens (100 // 3 = 33, remainder discarded)."""
    assert estimate_tokens_from_chars("x" * 100) == 33


# ── estimate_call_cost ────────────────────────────────────────────────────────

def test_estimate_call_cost_ollama_is_zero():
    """Ollama: both costs 0 → 0¢ regardless of message length."""
    cost = estimate_call_cost("x" * 10_000, OLLAMA, max_output_tokens=2000)
    assert cost == Decimal("0.000000")


def test_estimate_call_cost_deepseek_empty_input():
    """Empty input → 0 input tokens; only output cost applies.
    2000 tokens × 0.11¢/1k = 0.22¢.
    """
    cost = estimate_call_cost("", DEEPSEEK, max_output_tokens=2000)
    assert cost == Decimal("0.220000")


def test_estimate_call_cost_deepseek_with_input():
    """900-char input → 300 input tokens; combined with 2000 output tokens.
    input_cost  = 300 × 0.027 / 1000 = 0.0081¢
    output_cost = 2000 × 0.11  / 1000 = 0.22¢
    total                              = 0.2281¢
    """
    cost = estimate_call_cost("x" * 900, DEEPSEEK, max_output_tokens=2000)
    assert cost == Decimal("0.228100")


def test_estimate_call_cost_large_message_deepseek_exceeds_one_cent():
    """100k-char message → ~33k input tokens → total > 1¢.
    input_cost  = 33333 × 0.027 / 1000 = 0.899991¢
    output_cost = 2000  × 0.11  / 1000 = 0.22¢
    total                               ≈ 1.12¢
    """
    cost = estimate_call_cost("x" * 100_000, DEEPSEEK, max_output_tokens=2000)
    assert cost > Decimal("1.0")


def test_estimate_call_cost_default_max_output_tokens():
    """Calling without max_output_tokens uses 2000-token default."""
    cost_explicit = estimate_call_cost("", DEEPSEEK, max_output_tokens=2000)
    cost_default = estimate_call_cost("", DEEPSEEK)
    assert cost_explicit == cost_default


# ── enforce_pre_call_cap ──────────────────────────────────────────────────────

def test_enforce_cap_blocks_when_over():
    """Estimated cost > cap → rejected with correct reason."""
    decision = enforce_pre_call_cap(
        estimated_cost_cents=Decimal("1.5"), max_cost_cents=1
    )
    assert decision.allowed is False
    assert decision.reason == "projected_cost_exceeds_cap"


def test_enforce_cap_allows_when_under():
    """Estimated cost < cap → allowed."""
    decision = enforce_pre_call_cap(
        estimated_cost_cents=Decimal("0.22"), max_cost_cents=10
    )
    assert decision.allowed is True
    assert decision.reason == "within_cap"


def test_enforce_cap_allows_when_equal():
    """Estimated cost == cap → allowed (not strictly greater)."""
    decision = enforce_pre_call_cap(
        estimated_cost_cents=Decimal("10"), max_cost_cents=10
    )
    assert decision.allowed is True


def test_enforce_cap_none_always_allows():
    """max_cost_cents=None → no guardrail configured → always allowed."""
    decision = enforce_pre_call_cap(
        estimated_cost_cents=Decimal("999"), max_cost_cents=None
    )
    assert decision.allowed is True
    assert decision.reason == "no_cap_configured"
    assert decision.cap_cents is None


def test_enforce_cap_decision_carries_fields():
    """Decision dataclass carries estimated_cost_cents and cap_cents for telemetry."""
    decision = enforce_pre_call_cap(
        estimated_cost_cents=Decimal("15.5"), max_cost_cents=10
    )
    assert decision.estimated_cost_cents == Decimal("15.5")
    assert decision.cap_cents == 10


# ── End-to-end: block path ────────────────────────────────────────────────────

def test_e2e_long_message_deepseek_blocked_by_tight_cap():
    """100k-char message on DeepSeek: estimated >1¢ → blocked by 1¢ cap."""
    cost = estimate_call_cost("x" * 100_000, DEEPSEEK, max_output_tokens=2000)
    decision = enforce_pre_call_cap(estimated_cost_cents=cost, max_cost_cents=1)
    assert decision.allowed is False
    assert decision.reason == "projected_cost_exceeds_cap"


def test_e2e_short_message_deepseek_within_standard_cap():
    """Typical 30-char message on DeepSeek stays well under 10¢ cap."""
    cost = estimate_call_cost("How much does ibuprofen cost?", DEEPSEEK, max_output_tokens=2000)
    decision = enforce_pre_call_cap(estimated_cost_cents=cost, max_cost_cents=10)
    assert decision.allowed is True


def test_e2e_ollama_free_passes_any_cap():
    """Ollama (0¢) passes any non-None cap, even 1¢."""
    cost = estimate_call_cost("x" * 100_000, OLLAMA, max_output_tokens=2000)
    decision = enforce_pre_call_cap(estimated_cost_cents=cost, max_cost_cents=1)
    assert decision.allowed is True
