"""Secret-matching logic for the BFF -> Clawith dispatch endpoint.

Split out from app/api/internal_dispatch.py (no FastAPI/app imports here)
so the prod-vs-staging secret comparison is unit-testable without pulling
in that module's transitive dependency graph (paperclip_client, agent
models, LLM caller, etc.).

Decision d35 / PKT-04 (2026-07-05): staging BFF (bff-v2-staging) cannot
authenticate to the shared Clawith :8800 dispatch endpoint because it only
ever had one valid secret, BFF_CLAWITH_SHARED_SECRET, provisioned for the
production BFF. Option C (selected): give the staging caller its own
second secret value, BFF_CLAWITH_SHARED_SECRET_STAGING, checked alongside
the existing prod secret. Neither the endpoint, the header scheme, nor any
other caller's behavior changes -- this only widens the set of values the
existing Authorization: Bearer check will accept.
"""
from __future__ import annotations

import os
import secrets


def resolve_bff_clawith_secret(presented: str | None) -> str | None:
    """Return which configured secret `presented` matches, or None.

    Checks BFF_CLAWITH_SHARED_SECRET (prod) first, then
    BFF_CLAWITH_SHARED_SECRET_STAGING. An unset/blank env value never
    matches, even against an empty presented token, so leaving the staging
    secret unconfigured leaves behavior identical to today (prod-only).

    Returns "prod", "staging", or None (no match / nothing configured).
    """
    if not presented:
        return None

    prod_secret = os.environ.get("BFF_CLAWITH_SHARED_SECRET", "").strip()
    if prod_secret and secrets.compare_digest(presented, prod_secret):
        return "prod"

    staging_secret = os.environ.get("BFF_CLAWITH_SHARED_SECRET_STAGING", "").strip()
    if staging_secret and secrets.compare_digest(presented, staging_secret):
        return "staging"

    return None


def any_bff_clawith_secret_configured() -> bool:
    """True if at least one of the two secrets is set (non-blank)."""
    prod_secret = os.environ.get("BFF_CLAWITH_SHARED_SECRET", "").strip()
    staging_secret = os.environ.get("BFF_CLAWITH_SHARED_SECRET_STAGING", "").strip()
    return bool(prod_secret or staging_secret)
