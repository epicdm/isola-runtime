"""Provenance reconciliation — escalate_to_human handoff-signal regression.

Covers the three-line ``execute_tool`` branch reconstructed from the live-only
commit ``e3a232a7`` (see
``defect-isola-runtime-live-local-commit-tracks-secret-backup-files-2026-08-06``).
The behaviour was running in production but had never been pushed to a remote,
so nothing on the canonical branch proved it. No existing test in this repo
referenced ``escalate_to_human`` or ``needs_handoff_signal``.

Probes:
  (a) ``escalate_to_human`` sets ``needs_handoff_signal`` and returns the
      operator-facing flag message that ``internal_dispatch.py`` relies on
      (it reads ``needs_handoff_signal.get()`` into its ``needs_handoff``
      response field).
  (b) an unrelated tool leaves the signal untouched — the new branch must not
      change dispatch for anything else.
  (c) the branch is genuinely reachable: ``escalate_to_human`` is neither
      sandbox-gated nor autonomy-gated, so neither early return shadows it.

Only ``_get_agent_tenant_id`` and ``ensure_workspace`` are stubbed — the two
unconditional awaits between the function entry and the dispatch chain. The
dispatch decision itself is left real.

Run from the runtime backend dir:
    pytest backend/tests/test_escalate_to_human_handoff_signal.py -v
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import agent_tools  # noqa: E402

AGENT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()

EXPECTED_MESSAGE = (
    "Flagged for a team member to follow up so a person can take over."
)


def _patch_preamble():
    """Stub only the two unconditional awaits ahead of the dispatch chain."""
    return (
        patch.object(
            agent_tools, "_get_agent_tenant_id", AsyncMock(return_value=None)
        ),
        patch.object(
            agent_tools, "ensure_workspace", AsyncMock(return_value="/tmp/ws")
        ),
    )


async def _run(tool_name: str, arguments: dict | None = None) -> str:
    tenant_patch, ws_patch = _patch_preamble()
    with tenant_patch, ws_patch:
        return await agent_tools.execute_tool(
            tool_name, arguments or {}, AGENT_ID, USER_ID
        )


@pytest.mark.asyncio
async def test_escalate_to_human_sets_the_handoff_signal():
    agent_tools.needs_handoff_signal.set(False)

    result = await _run("escalate_to_human")

    assert agent_tools.needs_handoff_signal.get() is True
    assert result == EXPECTED_MESSAGE


@pytest.mark.asyncio
async def test_unrelated_tool_does_not_set_the_handoff_signal():
    """The added branch must not change dispatch for any other tool."""
    agent_tools.needs_handoff_signal.set(False)

    with patch.object(agent_tools, "_list_files", return_value="[]"):
        result = await _run("list_files", {"path": ""})

    assert agent_tools.needs_handoff_signal.get() is False
    assert result != EXPECTED_MESSAGE


def test_escalate_to_human_is_not_shadowed_by_an_earlier_gate():
    """Neither early return in execute_tool can pre-empt the branch."""
    assert "escalate_to_human" not in agent_tools.SANDBOX_GATED_TOOLS
    assert "escalate_to_human" not in agent_tools._TOOL_AUTONOMY_MAP


def test_dispatch_reads_the_same_signal_object():
    """internal_dispatch must observe the very ContextVar the tool sets.

    ``internal_dispatch`` imports the ContextVar inside the request handler,
    so assert on the function's own symbol table rather than the module dict.
    """
    from app.api import internal_dispatch

    assert (
        "needs_handoff_signal"
        in internal_dispatch.internal_dispatch.__code__.co_names
    )
