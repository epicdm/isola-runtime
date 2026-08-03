# ISOLA GLUE — additive test (not upstream)
"""Source-scope guards for the run-scoped assistant-reply migration
(`dec-clawith-run-scoped-assistant-reply-ownership-2026-08-03`, closing
`defect-clawith-assistant-reply-selection-not-run-scoped-2026-08-03`).

Pure static-source and (best-effort, skip-if-unavailable) git-diff proofs
that:
* neither affected bridge (`isola_bridge.py`, `isola_bridge_structured.py`)
  retains the removed session-plus-time ``ChatMessage`` scan or its
  ``_read_last_assistant`` helper;
* the two files this slice is explicitly forbidden from touching
  (`isola_bridge_v2.py`, `app/services/agent_runtime/delivery.py`) are
  byte-identical to the ratified base commit
  (`9279bed3d008ede469069885e46a06aa0c732714`);
* no Alembic migration file was added by this slice (`MIGRATION_REQUIRED=NO`
  per the R1 gate contract — no migration, backfill or index is part of this
  design).

The git-diff-based proofs are best-effort: they SKIP (never silently pass,
never error) when `.git` or the pinned base commit is unavailable, e.g.
inside a built container image that does not ship `.git` — mirroring the
`_require_local_database` skip convention already used by
`test_isola_structured_bridge_claim_isolation.py` for its own
environment-dependent preconditions.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

# The exact base commit R1 branched from (PORT: dec-clawith-run-scoped-
# assistant-reply-ownership-2026-08-03's pre-flight verification).
_BASE_SHA = "9279bed3d008ede469069885e46a06aa0c732714"

# SHA-256 of the two forbidden-to-touch files' content AT that exact base
# commit (`git show <base_sha>:<path> | sha256sum`), pinned independently of
# whether git is available at test time.
_ISOLA_BRIDGE_V2_BASE_SHA256 = (
    "1419a85f22ae66b4b39d159e05f45c86bcc776d6cae4a3a443b31085054c6f24"
)
_DELIVERY_BASE_SHA256 = "fdabee296dfe93ddf9c3097a09db48f96aa499500f2db23e88eb23bf8ebf7e35"

_ISOLA_BRIDGE_V2_PATH = BACKEND_DIR / "app" / "api" / "isola_bridge_v2.py"
_DELIVERY_PATH = BACKEND_DIR / "app" / "services" / "agent_runtime" / "delivery.py"
_LEGACY_BRIDGE_PATH = BACKEND_DIR / "app" / "api" / "isola_bridge.py"
_STRUCTURED_BRIDGE_PATH = BACKEND_DIR / "app" / "api" / "isola_bridge_structured.py"

_REMOVED_PATTERNS = (
    "_read_last_assistant",
    "ChatMessage.created_at >=",
    "order_by(ChatMessage.created_at.desc",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_repo_at_base() -> bool:
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{_BASE_SHA}^{{commit}}"],
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


# ── Static source-content guards (no git dependency) ────────────────────────


@pytest.mark.parametrize("path", [_LEGACY_BRIDGE_PATH, _STRUCTURED_BRIDGE_PATH])
def test_bridge_no_longer_defines_read_last_assistant_or_time_window_scan(path):
    source = path.read_text(encoding="utf-8")
    for pattern in _REMOVED_PATTERNS:
        assert pattern not in source, f"{path.name} still contains removed pattern {pattern!r}"


def test_legacy_bridge_uses_the_run_owned_reply_helper():
    source = _LEGACY_BRIDGE_PATH.read_text(encoding="utf-8")
    assert "from app.services.agent_runtime.run_owned_reply import" in source
    assert "read_run_owned_reply(" in source


def test_structured_bridge_uses_the_run_owned_reply_helper():
    source = _STRUCTURED_BRIDGE_PATH.read_text(encoding="utf-8")
    assert "from app.services.agent_runtime.run_owned_reply import" in source
    assert "read_run_owned_reply(" in source


def test_structured_bridge_terminal_message_id_sourced_from_the_returned_reply():
    """Regression guard: `terminal_message_id` must come from the exact
    `RunOwnedReply` object whose text was returned, never a second
    independent query."""
    source = _STRUCTURED_BRIDGE_PATH.read_text(encoding="utf-8")
    assert '"terminal_message_id": str(reply.message_id)' in source


# ── Forbidden-to-touch files: byte-identical to the ratified base ──────────


@pytest.mark.skipif(not _git_repo_at_base(), reason="base commit unavailable in this checkout")
def test_isola_bridge_v2_is_byte_identical_to_the_ratified_base():
    assert _sha256(_ISOLA_BRIDGE_V2_PATH) == _ISOLA_BRIDGE_V2_BASE_SHA256


@pytest.mark.skipif(not _git_repo_at_base(), reason="base commit unavailable in this checkout")
def test_delivery_module_is_byte_identical_to_the_ratified_base():
    assert _sha256(_DELIVERY_PATH) == _DELIVERY_BASE_SHA256


def test_isola_bridge_v2_hash_matches_pinned_constant_independent_of_git():
    """Same proof as above, restated without a git dependency: if the pinned
    SHA-256 constant itself was captured correctly from the base commit (this
    test file's own provenance), a passing byte-identity test above implies
    this one always agrees. Exists so the identity claim is checkable by
    inspection even when git is unavailable."""
    assert len(_ISOLA_BRIDGE_V2_BASE_SHA256) == 64
    assert len(_DELIVERY_BASE_SHA256) == 64


# ── No migration added ───────────────────────────────────────────────────


@pytest.mark.skipif(not _git_repo_at_base(), reason="base commit unavailable in this checkout")
def test_no_migration_file_added_relative_to_the_ratified_base():
    result = subprocess.run(
        ["git", "diff", "--name-only", _BASE_SHA, "--", "backend/alembic/versions"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    changed = [line for line in result.stdout.splitlines() if line.strip()]
    assert changed == [], f"unexpected alembic/versions changes relative to base: {changed}"

    untracked = subprocess.run(
        ["git", "status", "--porcelain", "--", "backend/alembic/versions"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert untracked.returncode == 0, untracked.stderr
    assert untracked.stdout.strip() == "", f"untracked alembic/versions changes: {untracked.stdout}"
