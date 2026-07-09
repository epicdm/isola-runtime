"""LCR (Latency-Cost Router) — pure function, no DB calls.

Callers fetch PoolModel list, IntentRow, and TenantBucket from DB,
then pass to lcr(). Integration wiring lands in channel_common (S4).

Sort key:
  low difficulty  → cost_per_1k + 1.0 if p95 > 3000ms (latency-aware)
  medium/high/critical → cost_per_1k only (pure cost)
"""
from __future__ import annotations
from dataclasses import dataclass, field

_LATENCY_THRESHOLD_MS = 3_000
_LATENCY_PENALTY = 1.0


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


def lcr(
    intent_row: IntentRow,
    tenant_bucket: TenantBucket,
    pool: list[PoolModel],
) -> PoolModel | None:
    """Select the best pool model for this intent + tenant state.

    Returns None when no eligible model exists; caller falls back to
    agent.primary_model_id.
    """
    required = set(intent_row.capability_required or [])
    entitled = set(tenant_bucket.entitled_tiers)

    candidates = [
        m for m in pool
        if m.enabled
        and m.health_status == "healthy"
        and m.tier in entitled
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

    return min(candidates, key=sort_key)
