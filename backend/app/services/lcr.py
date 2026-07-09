"""LCR (Latency-Cost Router) — pure function, no DB calls.

Callers fetch PoolModel list, IntentRow, and TenantBucket from DB,
then pass to lcr() or lcr_detailed(). Integration wiring lands in
channel_common (S7).

Three-layer decision stack:
  1. Baseline entitlement      — plan.tier_access
  2. Bucket-based degradation  — removes tiers when tokens_used >= included_tokens
  3. Safety capability override — restores tiers if required capability unavailable
                                  (safety wins over budget)

Sort key:
  low difficulty  → cost_per_1k + 1.0 if p95 > 3000ms (latency-aware)
  medium/high/critical → cost_per_1k only (pure cost)

Public API:
  lcr()          → PoolModel | None          (S2-S5 stable interface)
  lcr_detailed() → LcrResult | None          (S6+ for telemetry + footer flags)

# FLOOR-13: bucket logic + safety override replaceable by LiteLLM virtual key budgets
"""
from __future__ import annotations
from dataclasses import dataclass, field

_LATENCY_THRESHOLD_MS = 3_000
_LATENCY_PENALTY = 1.0
_TIER_ORDER = ["free-router", "free-content", "standard", "premium", "batch"]


@dataclass
class PoolModel:
    id: str
    provider: str
    model: str
    tier: str
    enabled: bool
    health_status: str
    latency_p95_ms: int | None
    cost_per_1k_output_cents: float
    cost_per_1k_input_cents: float = 0.0  # DB column lands in Floor #13; 0 for all Phase 2 models
    capability_tags: list[str] = field(default_factory=list)


@dataclass
class IntentRow:
    vertical: str
    intent_pattern: str
    difficulty: str          # low | medium | high | critical
    preferred_tier: str
    capability_required: list[str] = field(default_factory=list)
    max_cost_cents: int | None = None
    fallback_tier: str | None = None


@dataclass
class TenantBucket:
    plan_id: str
    entitled_tiers: list[str]
    tokens_used: int
    included_tokens: int
    overage_policy: str = "degrade"   # degrade | block | credit
    credit_balance_cents: int = 0


@dataclass
class LcrResult:
    """Routing outcome with diagnostic flags for telemetry and footer logic."""
    model: PoolModel
    degraded: bool          # Layer 2 applied (bucket exhaustion reduced tiers)
    safety_override: bool   # Layer 3 applied (capability restored a removed tier)

    @property
    def fallback_used(self) -> bool:
        return self.degraded or self.safety_override


def degrade_tier_list(
    tiers: list[str],
    policy: str,
    credit_balance_cents: int = 0,
) -> list[str]:
    """Apply overage_policy to entitled tiers when bucket is exhausted."""
    if policy == "degrade":
        ranked = sorted(
            [t for t in tiers if t in _TIER_ORDER],
            key=lambda t: _TIER_ORDER.index(t),
        )
        return ranked[:-1] if len(ranked) > 1 else ranked
    if policy == "block":
        return []
    if policy == "credit":
        return tiers if credit_balance_cents > 0 else []
    return tiers


def lcr_detailed(
    intent_row: IntentRow,
    tenant_bucket: TenantBucket,
    pool: list[PoolModel],
) -> LcrResult | None:
    """Select best model and return routing flags for telemetry/footer.

    Returns None when no eligible model exists.
    """
    # Layer 1: baseline entitlement
    entitled = list(tenant_bucket.entitled_tiers)
    degraded = False
    safety_override = False

    # Layer 2: bucket-based degradation
    if tenant_bucket.tokens_used >= tenant_bucket.included_tokens:
        degraded_entitled = degrade_tier_list(
            entitled,
            tenant_bucket.overage_policy,
            tenant_bucket.credit_balance_cents,
        )
        if degraded_entitled != entitled:
            degraded = True
        entitled = degraded_entitled

    # Layer 3: safety capability override
    # If required capability unavailable in entitled tiers, restore tiers that
    # carry it. Safety wins over budget — including hard block policy.
    # (FLOOR-13: may be expressed via LiteLLM router conditions)
    required = set(intent_row.capability_required or [])
    if required:
        safety_tiers = {
            m.tier for m in pool
            if m.enabled and required.issubset(set(m.capability_tags or []))
        }
        if safety_tiers and not (set(entitled) & safety_tiers):
            entitled = list(set(entitled) | safety_tiers)
            safety_override = True

    entitled_set = set(entitled)

    candidates = [
        m for m in pool
        if m.enabled
        and m.health_status == "healthy"
        and m.tier in entitled_set
        and required.issubset(set(m.capability_tags or []))
    ]

    if not candidates:
        return None

    def sort_key(m: PoolModel) -> float:
        cost = m.cost_per_1k_output_cents or 0.0
        if intent_row.difficulty == "low":
            slow = m.latency_p95_ms is None or m.latency_p95_ms > _LATENCY_THRESHOLD_MS
            return cost + (_LATENCY_PENALTY if slow else 0.0)
        return cost

    return LcrResult(
        model=min(candidates, key=sort_key),
        degraded=degraded,
        safety_override=safety_override,
    )


def lcr(
    intent_row: IntentRow,
    tenant_bucket: TenantBucket,
    pool: list[PoolModel],
) -> PoolModel | None:
    """Stable interface for callers that only need the selected model.

    Returns None when no eligible model exists; caller falls back to
    agent.primary_model_id.
    """
    result = lcr_detailed(intent_row, tenant_bucket, pool)
    return result.model if result else None
