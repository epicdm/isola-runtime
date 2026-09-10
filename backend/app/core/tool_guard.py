"""Call-time principal authorization for tool dispatch.

`app.services.agent_tools.execute_tool` receives a `user_id` and has never
authorized with it: the only gates are the sandbox flag and
`autonomy_service.check_and_enforce`, which is per-AGENT
(`agents.autonomy_policy`), not per-PRINCIPAL. `user_id` reaches the DB only
as the log string `requested_by`.

This module is the missing check, applied at the moment of action rather
than at the moment a route was entered. Importers get a drop-in replacement
for `execute_tool`:

    from app.core.tool_guard import execute_tool

It is a wrapper rather than an edit inside `agent_tools.py` because that
module is 9,392 lines and every caller that matters imports the symbol; the
wrapper keeps the authorization decision in one small, reviewable place next
to `session_privacy`.

COVERAGE, stated exactly, because a security control whose reach is vague is
not a control:

  * `app/services/llm/caller.py` (both call sites) -- the human chat turn and
    every inbound channel turn (WhatsApp, Discord, Teams, Feishu, Slack)
    funnel through here. GATED.
  * `app/services/heartbeat.py` (3 sites) -- autonomous wakes, which pass
    `agent_creator_id`: a real principal that resolves to `manage`, so the
    gate admits them. Left importing the raw symbol; behaviour is identical
    either way.
  * `app/services/agent_tools.py:4762` -- the agent-to-agent dispatch, which
    passes the target agent's `owner_id` from inside the module itself.
    NOT gated by this wrapper. See docs/privacy/PRIVACY-BOUNDARY-REPAIR.
"""

from __future__ import annotations

import uuid
from typing import Optional

from loguru import logger

from app.core.session_privacy import PrincipalDenied, assert_tool_allowed


async def execute_tool(
    tool_name: str,
    arguments: dict,
    agent_id: uuid.UUID,
    user_id: Optional[uuid.UUID] = None,
    session_id: str = "",
) -> str:
    """Authorize the principal, then delegate to the real dispatcher.

    A refusal returns the same shape a tool failure returns (a string the
    model sees), so a denied action is reported to the agent rather than
    raised into the turn and lost.
    """
    from app.database import async_session
    from app.services.agent_tools import execute_tool as _raw_execute_tool

    async with async_session() as db:
        try:
            await assert_tool_allowed(db, tool_name, agent_id, user_id)
        except PrincipalDenied as denial:
            logger.info(
                f"[Privacy] {tool_name} denied for principal {user_id} "
                f"on agent {agent_id}: {denial.detail}"
            )
            return f"❌ {denial.detail}"

    return await _raw_execute_tool(
        tool_name, arguments, agent_id, user_id, session_id
    )
