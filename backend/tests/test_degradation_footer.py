"""S7 TDD: degradation footer — apply_degradation_footer (pure function).

All tests are synchronous (no DB, no async); the function is pure.

Footer rules ratified at D6 grilling:
  - Show when lcr_result.degraded=True AND difficulty != 'low'
  - Suppress on low-difficulty (silent degradation — greetings, small_talk)
  - Suppress when safety_override=True only (lcr_result.degraded stays False)
  - Suppress on happy path (degraded=False, safety_override=False)
  - Renewal date from bucket_renews_at; fallback 'next month' if None

Substrate note: parse_and_dispatch (reply_markers.py) only strips [marker: ...]
syntax via _MARKER_RE — _italic_ markdown passes through unmodified to WA.
"""
from datetime import date
from app.services.lcr import LcrResult, PoolModel
from app.services.degradation_footer import apply_degradation_footer


# ── Fixtures ──────────────────────────────────────────────────────────────────

OLLAMA_QWEN = PoolModel(
    id="cb8da9b6-5891-4d22-ab2c-2df7cf8122e4",
    provider="ollama", model="qwen2.5:3b-instruct-q4_K_M", tier="free-content",
    enabled=True, health_status="healthy",
    latency_p95_ms=20000, cost_per_1k_output_cents=0,
    capability_tags=["text", "multilingual"],
)
DEEPSEEK = PoolModel(
    id="8db68763-c43f-4e38-b5b9-82e3c4b8887c",
    provider="deepseek", model="deepseek-chat", tier="standard",
    enabled=True, health_status="healthy",
    latency_p95_ms=2000, cost_per_1k_output_cents=0.11,
    capability_tags=["text", "multilingual", "reasoning", "cite_sources"],
)

RENEWS = "2026-06-22"


def _lcr(model, degraded, safety_override=False):
    return LcrResult(model=model, degraded=degraded, safety_override=safety_override)


# ── Footer shown: degraded + difficulty >= medium ─────────────────────────────

def test_footer_attached_medium_difficulty_degraded():
    """Medium-difficulty + degraded → footer appended with renewal date."""
    base = "Available grants this month include..."
    result = apply_degradation_footer(
        base_reply=base,
        lcr_result=_lcr(OLLAMA_QWEN, degraded=True),
        intent_difficulty="medium",
        bucket_renews_at=RENEWS,
    )
    assert "Quick reply with limited detail" in result
    assert RENEWS in result
    assert base in result


def test_footer_attached_high_difficulty_degraded():
    """High-difficulty + degraded → footer shown."""
    result = apply_degradation_footer(
        base_reply="Complex answer...",
        lcr_result=_lcr(OLLAMA_QWEN, degraded=True),
        intent_difficulty="high",
        bucket_renews_at=RENEWS,
    )
    assert "Quick reply with limited detail" in result


def test_footer_attached_critical_difficulty_degraded():
    """Critical-difficulty + degraded → footer shown."""
    result = apply_degradation_footer(
        base_reply="Critical response...",
        lcr_result=_lcr(OLLAMA_QWEN, degraded=True),
        intent_difficulty="critical",
        bucket_renews_at=RENEWS,
    )
    assert "Quick reply with limited detail" in result


# ── Footer suppressed: low-difficulty (silent degradation) ────────────────────

def test_no_footer_low_difficulty_degraded():
    """Low-difficulty + degraded → silent degradation, reply unchanged."""
    base = "Hello! How can I help?"
    result = apply_degradation_footer(
        base_reply=base,
        lcr_result=_lcr(OLLAMA_QWEN, degraded=True),
        intent_difficulty="low",
        bucket_renews_at=RENEWS,
    )
    assert "Quick reply" not in result
    assert result == base


# ── Footer suppressed: safety override (customer got the right model) ─────────

def test_no_footer_safety_override_only():
    """Safety override fired → lcr_result.degraded=False → footer suppressed."""
    result = apply_degradation_footer(
        base_reply="For your safety, consult your pharmacist directly...",
        lcr_result=_lcr(DEEPSEEK, degraded=False, safety_override=True),
        intent_difficulty="critical",
        bucket_renews_at=RENEWS,
    )
    assert "Quick reply" not in result


# ── Footer suppressed: happy path ─────────────────────────────────────────────

def test_no_footer_happy_path():
    """No degradation, no override → reply unchanged."""
    base = "Available grants this month..."
    result = apply_degradation_footer(
        base_reply=base,
        lcr_result=_lcr(DEEPSEEK, degraded=False),
        intent_difficulty="medium",
        bucket_renews_at=RENEWS,
    )
    assert "Quick reply" not in result
    assert result == base


# ── Renewal date variants ──────────────────────────────────────────────────────

def test_footer_renewal_date_from_string():
    """ISO date string → embedded as-is in footer."""
    result = apply_degradation_footer(
        base_reply="...",
        lcr_result=_lcr(OLLAMA_QWEN, degraded=True),
        intent_difficulty="medium",
        bucket_renews_at="2026-08-15",
    )
    assert "2026-08-15" in result


def test_footer_renewal_date_from_date_object():
    """date object → formatted YYYY-MM-DD in footer."""
    result = apply_degradation_footer(
        base_reply="...",
        lcr_result=_lcr(OLLAMA_QWEN, degraded=True),
        intent_difficulty="medium",
        bucket_renews_at=date(2026, 6, 22),
    )
    assert "2026-06-22" in result


def test_footer_renewal_date_none_uses_fallback():
    """No renewal date → 'next month' fallback in footer."""
    result = apply_degradation_footer(
        base_reply="...",
        lcr_result=_lcr(OLLAMA_QWEN, degraded=True),
        intent_difficulty="medium",
        bucket_renews_at=None,
    )
    assert "next month" in result


# ── Content preservation ───────────────────────────────────────────────────────

def test_footer_appended_not_prepended():
    """Footer comes after the original reply, not before."""
    base = "This is the main content."
    result = apply_degradation_footer(
        base_reply=base,
        lcr_result=_lcr(OLLAMA_QWEN, degraded=True),
        intent_difficulty="medium",
        bucket_renews_at=RENEWS,
    )
    assert result.startswith(base)


def test_footer_uses_italic_markdown():
    """Footer text is wrapped in _italic_ for WhatsApp rendering."""
    result = apply_degradation_footer(
        base_reply="...",
        lcr_result=_lcr(OLLAMA_QWEN, degraded=True),
        intent_difficulty="medium",
        bucket_renews_at=RENEWS,
    )
    assert "_Quick reply with limited detail" in result
