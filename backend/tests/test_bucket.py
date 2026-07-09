"""S4 TDD: bucket debit, lazy renewal, usage extraction (pure-function tests).

debit_bucket (async DB write) is infrastructure tested at integration.
compute_debit and extract_usage are the logic — pure functions, no DB needed.

Acceptance: all tests pass.
"""
from datetime import datetime, timedelta, timezone
import pytest
from app.services.bucket import compute_debit, extract_usage


# ── Shared fixtures ───────────────────────────────────────────────────────────

RENEWED_AT = "2026-05-01T00:00:00+00:00"
NOW = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)  # 21 days after RENEWED_AT

FRESH_BUCKET = {
    "tokens_used": 1000,
    "messages_used": 5,
    "last_call_at": None,
    "renewed_at": RENEWED_AT,
}


# ── extract_usage ─────────────────────────────────────────────────────────────

def test_extract_usage_openai_compat():
    """deepseek / Ollama: prompt_tokens + completion_tokens."""
    assert extract_usage({"prompt_tokens": 200, "completion_tokens": 350}) == (200, 350)


def test_extract_usage_anthropic_format():
    """Anthropic / Gemini-normalized: input_tokens + output_tokens."""
    assert extract_usage({"input_tokens": 100, "output_tokens": 200}) == (100, 200)


def test_extract_usage_none_returns_zeros():
    assert extract_usage(None) == (0, 0)


def test_extract_usage_empty_dict_returns_zeros():
    assert extract_usage({}) == (0, 0)


def test_extract_usage_prefers_input_tokens_over_prompt_tokens():
    """If both keys present (shouldn't happen, but defensive)."""
    result = extract_usage({"input_tokens": 10, "prompt_tokens": 99,
                            "output_tokens": 20, "completion_tokens": 99})
    assert result == (10, 20)


# ── compute_debit — standard debit ───────────────────────────────────────────

def test_compute_debit_accumulates_tokens():
    result = compute_debit(FRESH_BUCKET, 200, 350, NOW)
    assert result["tokens_used"] == 1550   # 1000 + 200 + 350


def test_compute_debit_increments_messages():
    result = compute_debit(FRESH_BUCKET, 200, 350, NOW)
    assert result["messages_used"] == 6   # 5 + 1


def test_compute_debit_sets_last_call_at():
    result = compute_debit(FRESH_BUCKET, 100, 100, NOW)
    assert result["last_call_at"] == NOW.isoformat()


def test_compute_debit_preserves_renewed_at():
    result = compute_debit(FRESH_BUCKET, 100, 100, NOW)
    assert result["renewed_at"] == RENEWED_AT


def test_compute_debit_zero_tokens_still_increments_messages():
    result = compute_debit(FRESH_BUCKET, 0, 0, NOW)
    assert result["messages_used"] == 6
    assert result["tokens_used"] == 1000   # unchanged


def test_compute_debit_missing_tokens_key_treated_as_zero():
    """Bucket without tokens_used (e.g. fresh row) doesn't crash."""
    result = compute_debit({}, 100, 200, NOW)
    assert result["tokens_used"] == 300
    assert result["messages_used"] == 1


# ── compute_debit — lazy renewal ─────────────────────────────────────────────

def test_compute_debit_exactly_30_days_triggers_renewal():
    """Boundary: exactly 30d >= threshold → renewal fires."""
    renewed_at = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    bucket = {**FRESH_BUCKET, "renewed_at": renewed_at.isoformat()}
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)  # exactly 30d

    result = compute_debit(bucket, 200, 350, now)
    assert result["tokens_used"] == 550      # reset + only current call
    assert result["messages_used"] == 1      # counter reset
    assert result["renewed_at"] == now.isoformat()


def test_compute_debit_37_days_triggers_renewal():
    renewed_at = datetime(2026, 4, 15, 0, 0, 0, tzinfo=timezone.utc)
    bucket = {**FRESH_BUCKET, "tokens_used": 480_000, "messages_used": 300,
              "renewed_at": renewed_at.isoformat()}
    now = datetime(2026, 5, 22, 0, 0, 0, tzinfo=timezone.utc)  # 37d

    result = compute_debit(bucket, 200, 350, now)
    assert result["tokens_used"] == 550
    assert result["messages_used"] == 1
    assert result["renewed_at"] == now.isoformat()


def test_compute_debit_29_days_no_renewal():
    """Below threshold: 29d → no renewal, counter accumulates."""
    renewed_at = datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc)
    bucket = {**FRESH_BUCKET, "renewed_at": renewed_at.isoformat()}
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)  # 29d

    result = compute_debit(bucket, 200, 350, now)
    assert result["tokens_used"] == 1550   # accumulated, not reset
    assert result["messages_used"] == 6
    assert result["renewed_at"] == renewed_at.isoformat()   # unchanged


def test_compute_debit_no_renewed_at_skips_renewal():
    """Bucket without renewed_at → no renewal check, normal debit."""
    bucket = {"tokens_used": 500, "messages_used": 3,
              "last_call_at": None, "renewed_at": None}
    result = compute_debit(bucket, 100, 200, NOW)
    assert result["tokens_used"] == 800
    assert result["messages_used"] == 4
    assert result["renewed_at"] is None


def test_compute_debit_malformed_renewed_at_skips_renewal():
    """Malformed timestamp → graceful skip, no crash."""
    bucket = {**FRESH_BUCKET, "renewed_at": "not-a-date"}
    result = compute_debit(bucket, 100, 200, NOW)
    assert result["tokens_used"] == 1300   # normal debit, no crash
    assert result["renewed_at"] == "not-a-date"   # preserved as-is
