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

import subprocess
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

# The exact base commit R1 branched from (PORT: dec-clawith-run-scoped-
# assistant-reply-ownership-2026-08-03's pre-flight verification).
_BASE_SHA = "9279bed3d008ede469069885e46a06aa0c732714"

_ISOLA_BRIDGE_V2_RELPATH = "backend/app/api/isola_bridge_v2.py"
_DELIVERY_RELPATH = "backend/app/services/agent_runtime/delivery.py"

_LEGACY_BRIDGE_PATH = BACKEND_DIR / "app" / "api" / "isola_bridge.py"
_STRUCTURED_BRIDGE_PATH = BACKEND_DIR / "app" / "api" / "isola_bridge_structured.py"

_REMOVED_PATTERNS = (
    "_read_last_assistant",
    "ChatMessage.created_at >=",
    "order_by(ChatMessage.created_at.desc",
)


def _git_diff_is_empty(relpath: str) -> str:
    """Returns '' when `relpath` is byte-for-byte unchanged relative to
    `_BASE_SHA` (working tree AND any staged/untracked delta), else the raw
    `git diff`/`git status` output for diagnostics.

    Deliberately NOT a `Path.read_bytes()` SHA-256 against a pinned
    constant: on a checkout with CRLF line-ending conversion (e.g. Windows
    `core.autocrlf`/`.gitattributes`), the on-disk bytes differ from the
    LF-normalized git blob bytes a pinned constant would have been captured
    from, producing a false "changed" result for a file git itself
    considers identical. `git diff` compares git's own normalized blob
    representations on both sides, so it agrees with `git status`
    regardless of the working tree's checkout line-ending settings.
    """
    diff = subprocess.run(
        ["git", "diff", _BASE_SHA, "--", relpath],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if diff.returncode != 0:
        return diff.stderr or "git diff failed"
    if diff.stdout.strip():
        return diff.stdout
    untracked = subprocess.run(
        ["git", "status", "--porcelain", "--", relpath],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if untracked.returncode != 0:
        return untracked.stderr or "git status failed"
    return untracked.stdout


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
    diff = _git_diff_is_empty(_ISOLA_BRIDGE_V2_RELPATH)
    assert diff == "", f"isola_bridge_v2.py differs from base {_BASE_SHA}:\n{diff}"


@pytest.mark.skipif(not _git_repo_at_base(), reason="base commit unavailable in this checkout")
def test_delivery_module_is_byte_identical_to_the_ratified_base():
    diff = _git_diff_is_empty(_DELIVERY_RELPATH)
    assert diff == "", f"delivery.py differs from base {_BASE_SHA}:\n{diff}"


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
