"""PKT-04 / decision d35: BFF->Clawith dispatch secret matching.

Pure unit test -- imports only app.core.bff_clawith_auth, which has no
FastAPI/DB/docker dependencies, so this runs standalone (no container,
no DATABASE_URL, no docker exec) via `pytest backend/tests/test_bff_clawith_staging_auth.py`.

Proves:
  1. The existing prod-secret path is unchanged (spine parity).
  2. The new staging secret authenticates when configured.
  3. A wrong/absent token is still rejected.
  4. Leaving the staging secret unset reproduces today's prod-only behavior.
  5. Nothing configured at all -> no match (endpoint-level 503 handled
     by any_bff_clawith_secret_configured(), tested separately below).
"""
from __future__ import annotations

import pytest

from app.core.bff_clawith_auth import (
    any_bff_clawith_secret_configured,
    resolve_bff_clawith_secret,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with both secrets unset, regardless of the host env."""
    monkeypatch.delenv("BFF_CLAWITH_SHARED_SECRET", raising=False)
    monkeypatch.delenv("BFF_CLAWITH_SHARED_SECRET_STAGING", raising=False)
    yield


def test_prod_secret_still_matches_unchanged(monkeypatch):
    """Existing prod path: unaffected by the staging addition."""
    monkeypatch.setenv("BFF_CLAWITH_SHARED_SECRET", "prod-secret-value")
    assert resolve_bff_clawith_secret("prod-secret-value") == "prod"


def test_prod_secret_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("BFF_CLAWITH_SHARED_SECRET", "prod-secret-value")
    assert resolve_bff_clawith_secret("not-the-secret") is None


def test_staging_secret_matches_when_configured(monkeypatch):
    monkeypatch.setenv("BFF_CLAWITH_SHARED_SECRET", "prod-secret-value")
    monkeypatch.setenv("BFF_CLAWITH_SHARED_SECRET_STAGING", "staging-secret-value")
    assert resolve_bff_clawith_secret("staging-secret-value") == "staging"
    # Prod path still works alongside it.
    assert resolve_bff_clawith_secret("prod-secret-value") == "prod"


def test_staging_secret_does_not_leak_into_prod_when_unset(monkeypatch):
    """If staging is never configured, presenting its value must NOT
    accidentally match anything -- confirms no broadening beyond intent."""
    monkeypatch.setenv("BFF_CLAWITH_SHARED_SECRET", "prod-secret-value")
    assert resolve_bff_clawith_secret("staging-secret-value") is None


def test_no_secret_configured_never_matches(monkeypatch):
    assert resolve_bff_clawith_secret("anything") is None
    assert resolve_bff_clawith_secret("") is None
    assert resolve_bff_clawith_secret(None) is None


def test_cross_wires_are_rejected(monkeypatch):
    """A token equal to neither configured secret is rejected even when
    both are set (prevents a copy-paste of the wrong env value from
    silently working)."""
    monkeypatch.setenv("BFF_CLAWITH_SHARED_SECRET", "prod-secret-value")
    monkeypatch.setenv("BFF_CLAWITH_SHARED_SECRET_STAGING", "staging-secret-value")
    assert resolve_bff_clawith_secret("some-other-value") is None


def test_any_configured_false_when_both_blank():
    assert any_bff_clawith_secret_configured() is False


def test_any_configured_true_with_only_staging_set(monkeypatch):
    monkeypatch.setenv("BFF_CLAWITH_SHARED_SECRET_STAGING", "staging-secret-value")
    assert any_bff_clawith_secret_configured() is True


def test_any_configured_true_with_only_prod_set(monkeypatch):
    monkeypatch.setenv("BFF_CLAWITH_SHARED_SECRET", "prod-secret-value")
    assert any_bff_clawith_secret_configured() is True
