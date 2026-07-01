"""S2+S3+S5 TDD: LCR happy path, bucket exhaustion, capability safety override.

All fixtures use actual substrate values from S0a DB probe (2026-05-22):
  deepseek-chat: tier=standard, p95=2000ms, cost=0.11¢/1k
                 capability_tags=[text, multilingual, reasoning, cite_sources]
  qwen1.5b:      tier=free-router, p95=7000ms, cost=0
  qwen3b:        tier=free-content, p95=20000ms, cost=0
  qwen7b:        tier=batch, p95=35000ms, cost=0
  solo plan tier_access: [free-router, free-content, standard]
  solo plan: included_tokens=500_000, overage_policy=degrade
"""
import dataclasses
import pytest
from app.services.lcr import lcr, degrade_tier_list, IntentRow, PoolModel, TenantBucket


# ── Pool fixtures (substrate-exact values from S0a) ──────────────────────────

DEEPSEEK = PoolModel(
    id="8db68763-c43f-4e38-b5b9-82e3c4b8887c",
    provider="deepseek", model="deepseek-chat", tier="standard",
    enabled=True, health_status="healthy",
    latency_p95_ms=2000, cost_per_1k_output_cents=0.11,
    capability_tags=["text", "multilingual", "reasoning", "cite_sources"],
)
QWEN_ROUTER = PoolModel(
    id="01d32867-337f-4f47-af60-0882324e3f4c",
    provider="ollama", model="qwen2.5:1.5b", tier="free-router",
    enabled=True, health_status="healthy",
    latency_p95_ms=7000, cost_per_1k_output_cents=0,
    capability_tags=["text", "fast"],
)
QWEN_CONTENT = PoolModel(
    id="cb8da9b6-5891-4d22-ab2c-2df7cf8122e4",
    provider="ollama", model="qwen2.5:3b-instruct-q4_K_M", tier="free-content",
    enabled=True, health_status="healthy",
    latency_p95_ms=20000, cost_per_1k_output_cents=0,
    capability_tags=["text", "multilingual"],
)
QWEN_BATCH = PoolModel(
    id="45b8135f-61a6-4588-a0d6-6f313678fa54",
    provider="ollama", model="qwen2.5:7b-instruct-q4_K_M", tier="batch",
    enabled=True, health_status="healthy",
    latency_p95_ms=35000, cost_per_1k_output_cents=0,
    capability_tags=["text", "multilingual", "batch_ok"],
)

FULL_POOL = [DEEPSEEK, QWEN_ROUTER, QWEN_CONTENT, QWEN_BATCH]

# ── Tenant fixtures ───────────────────────────────────────────────────────────

SOLO_BUCKET = TenantBucket(
    plan_id="solo",
    entitled_tiers=["free-router", "free-content", "standard"],
    tokens_used=0,
    included_tokens=500_000,
    overage_policy="degrade",
)
SOLO_EXHAUSTED = dataclasses.replace(SOLO_BUCKET, tokens_used=500_001)
SOLO_ALMOST = dataclasses.replace(SOLO_BUCKET, tokens_used=499_999)

# ── Intent row fixtures ───────────────────────────────────────────────────────

GREETING_ROW = IntentRow(
    vertical="pharmacy", intent_pattern="greeting",
    difficulty="low", preferred_tier="free-content",
    capability_required=[], max_cost_cents=10,
)
DOSAGE_ROW = IntentRow(
    vertical="pharmacy", intent_pattern="medication_dosage_question",
    difficulty="critical", preferred_tier="standard",
    capability_required=["cite_sources"], max_cost_cents=50,
)


# ── S2: LCR happy path ───────────────────────────────────────────────────────

def test_lcr_low_difficulty_full_bucket_selects_deepseek():
    """Low difficulty: all Ollama p95 > 3000ms gets +1.0 penalty → deepseek wins at 0.11."""
    selected = lcr(intent_row=GREETING_ROW, tenant_bucket=SOLO_BUCKET, pool=FULL_POOL)
    assert selected is not None
    assert selected.provider == "deepseek"
    assert selected.model == "deepseek-chat"


def test_lcr_medium_difficulty_pure_cost_wins():
    """Medium difficulty: no latency penalty → free models (cost=0) beat deepseek (0.11)."""
    medium_row = dataclasses.replace(GREETING_ROW, difficulty="medium")
    selected = lcr(intent_row=medium_row, tenant_bucket=SOLO_BUCKET, pool=FULL_POOL)
    assert selected is not None
    assert selected.cost_per_1k_output_cents == 0


def test_lcr_excludes_batch_tier_from_solo():
    """Batch tier not in Solo plan → batch-only pool returns None."""
    selected = lcr(intent_row=GREETING_ROW, tenant_bucket=SOLO_BUCKET,
                   pool=[QWEN_BATCH])
    assert selected is None


def test_lcr_excludes_disabled_models():
    """Disabled model is never selected even if it would win on cost+latency."""
    disabled_deepseek = dataclasses.replace(DEEPSEEK, enabled=False)
    selected = lcr(intent_row=GREETING_ROW, tenant_bucket=SOLO_BUCKET,
                   pool=[disabled_deepseek, QWEN_ROUTER, QWEN_CONTENT])
    assert selected is not None
    assert selected.provider == "ollama"


def test_lcr_capability_filter_cite_sources():
    """cite_sources required → only deepseek qualifies; Ollama lacks the tag."""
    critical_row = dataclasses.replace(
        GREETING_ROW, difficulty="critical",
        capability_required=["cite_sources"],
    )
    selected = lcr(intent_row=critical_row, tenant_bucket=SOLO_BUCKET, pool=FULL_POOL)
    assert selected is not None
    assert selected.model == "deepseek-chat"


def test_lcr_no_candidates_returns_none():
    """Empty eligible pool → None; caller falls back to agent.primary_model_id."""
    selected = lcr(intent_row=GREETING_ROW, tenant_bucket=SOLO_BUCKET, pool=[])
    assert selected is None


# ── S3: Bucket exhaustion / degradation ──────────────────────────────────────

def test_lcr_bucket_exhausted_drops_standard_tier():
    """tokens_used > included_tokens → standard tier removed; Ollama selected."""
    selected = lcr(intent_row=GREETING_ROW, tenant_bucket=SOLO_EXHAUSTED, pool=FULL_POOL)
    assert selected is not None
    assert selected.provider == "ollama"
    assert selected.tier != "standard"


def test_lcr_bucket_exactly_exhausted_boundary():
    """tokens_used == included_tokens is the exhaustion threshold (>=)."""
    at_limit = dataclasses.replace(SOLO_BUCKET, tokens_used=500_000)
    selected = lcr(intent_row=GREETING_ROW, tenant_bucket=at_limit, pool=FULL_POOL)
    assert selected is not None
    assert selected.provider == "ollama"
    assert selected.tier != "standard"


def test_lcr_bucket_almost_exhausted_still_gets_standard():
    """tokens_used < included_tokens → bucket valid; deepseek still wins."""
    selected = lcr(intent_row=GREETING_ROW, tenant_bucket=SOLO_ALMOST, pool=FULL_POOL)
    assert selected is not None
    assert selected.model == "deepseek-chat"


def test_lcr_block_policy_exhausted_returns_none():
    """overage_policy=block + exhausted → all tiers removed → None."""
    block_bucket = dataclasses.replace(SOLO_EXHAUSTED, overage_policy="block")
    selected = lcr(intent_row=GREETING_ROW, tenant_bucket=block_bucket, pool=FULL_POOL)
    assert selected is None


def test_lcr_credit_policy_with_balance_no_degradation():
    """overage_policy=credit + credit_balance > 0 → no degradation; deepseek wins."""
    credit_bucket = dataclasses.replace(
        SOLO_EXHAUSTED, overage_policy="credit", credit_balance_cents=500,
    )
    selected = lcr(intent_row=GREETING_ROW, tenant_bucket=credit_bucket, pool=FULL_POOL)
    assert selected is not None
    assert selected.model == "deepseek-chat"


def test_lcr_credit_policy_zero_balance_blocks():
    """overage_policy=credit + credit_balance=0 → blocked → None."""
    credit_bucket = dataclasses.replace(
        SOLO_EXHAUSTED, overage_policy="credit", credit_balance_cents=0,
    )
    selected = lcr(intent_row=GREETING_ROW, tenant_bucket=credit_bucket, pool=FULL_POOL)
    assert selected is None


# ── degrade_tier_list unit tests ─────────────────────────────────────────────

def test_degrade_removes_highest_tier():
    """degrade drops 'standard' from [free-router, free-content, standard]."""
    result = degrade_tier_list(["free-router", "free-content", "standard"], "degrade")
    assert "standard" not in result
    assert set(result) == {"free-router", "free-content"}


def test_degrade_at_minimum_tier_stays():
    """degrade on a single-tier list returns that tier (no further downgrade)."""
    result = degrade_tier_list(["free-router"], "degrade")
    assert result == ["free-router"]


def test_block_policy_empties_tiers():
    result = degrade_tier_list(["free-router", "standard"], "block")
    assert result == []


def test_credit_policy_with_balance_preserves_tiers():
    result = degrade_tier_list(["free-router", "standard"], "credit",
                                credit_balance_cents=100)
    assert set(result) == {"free-router", "standard"}


def test_credit_policy_zero_balance_empties_tiers():
    result = degrade_tier_list(["free-router", "standard"], "credit",
                                credit_balance_cents=0)
    assert result == []


# ── S5: Safety capability override ───────────────────────────────────────────

def test_lcr_safety_critical_overrides_bucket_exhaustion():
    """cite_sources required + bucket exhausted → safety override restores standard.

    Degradation removes standard tier. No entitled tier has cite_sources.
    Safety layer restores standard. deepseek-chat (only cite_sources model) selected.
    """
    selected = lcr(intent_row=DOSAGE_ROW, tenant_bucket=SOLO_EXHAUSTED, pool=FULL_POOL)
    assert selected is not None
    assert selected.provider == "deepseek"
    assert selected.model == "deepseek-chat"
    assert selected.tier == "standard"


def test_lcr_safety_critical_full_bucket_also_routes_to_deepseek():
    """cite_sources + full bucket → capability filter alone sends to deepseek."""
    selected = lcr(intent_row=DOSAGE_ROW, tenant_bucket=SOLO_BUCKET, pool=FULL_POOL)
    assert selected is not None
    assert selected.model == "deepseek-chat"


def test_lcr_non_safety_intent_exhausted_degrades_without_override():
    """greeting (no capability_required) + exhausted → degradation applies, no override.

    Substrate note: free-router and free-content both score 1.0 on low-difficulty
    (cost=0 + latency_penalty=1.0). min() returns whichever appears first in pool.
    Assert provider and tier class, not specific model (tie-break is pool-order).
    """
    selected = lcr(intent_row=GREETING_ROW, tenant_bucket=SOLO_EXHAUSTED, pool=FULL_POOL)
    assert selected is not None
    assert selected.provider == "ollama"
    assert selected.tier != "standard"   # standard removed, override not triggered


def test_lcr_safety_override_only_fires_when_no_entitled_tier_qualifies():
    """Safety override is surgical: if an entitled tier already has the capability,
    no override needed. Degrade removes standard; if free-content had cite_sources,
    override would NOT restore standard (because free-content already covers it).
    """
    qwen_with_cite = dataclasses.replace(
        QWEN_CONTENT, capability_tags=["text", "multilingual", "cite_sources"]
    )
    pool = [DEEPSEEK, QWEN_ROUTER, qwen_with_cite, QWEN_BATCH]
    selected = lcr(intent_row=DOSAGE_ROW, tenant_bucket=SOLO_EXHAUSTED, pool=pool)
    assert selected is not None
    # free-content now has cite_sources AND is in entitled tiers → override not needed
    # Both qwen_with_cite (cost=0) and deepseek (cost=0.11) qualify on capability.
    # critical-difficulty uses pure cost → qwen_with_cite wins.
    assert selected.tier == "free-content"
    assert selected.provider == "ollama"


def test_lcr_safety_override_block_policy_still_applies():
    """Block policy + safety-critical intent → safety override restores capability tiers
    even under block policy. Safety wins over budget (including hard block)."""
    block_bucket = dataclasses.replace(SOLO_EXHAUSTED, overage_policy="block")
    selected = lcr(intent_row=DOSAGE_ROW, tenant_bucket=block_bucket, pool=FULL_POOL)
    assert selected is not None
    assert selected.model == "deepseek-chat"
