"""Principal-scoped privacy boundary for agent chat sessions and tool calls.

ONE enforcement point, built on the permission system that already exists
(`agent_permissions`.scope_type / scope_id / access_level). This module adds
no second permission store and no new roles.

Two laws it implements:

1. Company-wide visibility must not confer company-wide access.
   Every read and every tool call is resolved against the *authenticated
   person's* live access level for that agent, not against the agent's
   creator and not against a coarse platform role.

2. Private owner conversations must not become shared staff memory.
   A chat session is `private` by default. A private session is readable
   only by the principal who owns it. `manage` access to the agent grants
   configuration rights, not a window into other people's conversations;
   sessions are surfaced to a manager only when they are explicitly
   `shared`.

Revocation is honoured because every resolution is a live read: delete the
`agent_permissions` row, or clear `users.is_active`, and the very next
request (and the very next tool call inside an in-flight turn) resolves to
None and is refused.
"""

from __future__ import annotations

import uuid
from typing import Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentPermission
from app.models.chat_session import ChatSession
from app.models.user import User

MANAGE = "manage"
USE = "use"

VISIBILITY_PRIVATE = "private"
VISIBILITY_SHARED = "shared"

# Tools that act with owner-level authority over the business. A principal
# holding only `use` may talk to the agent; it may not make the agent act as
# the owner. Names match app.services.agent_tools dispatch names.
OWNER_LEVEL_TOOLS: frozenset[str] = frozenset({
    "create_agent",
    "delete_agent",
    "update_agent_config",
    "set_agent_permission",
    "revoke_agent_permission",
    "create_credential",
    "update_credential",
    "delete_credential",
    "read_credential",
    "send_whatsapp_message",
    "send_payment_link",
    "create_payment_link",
    "escalate_to_human",
    "create_trigger",
    "cancel_trigger",
    "publish_page",
    "odoo_write",
    "odoo_unlink",
})


class PrincipalDenied(Exception):
    """Raised when the authenticated principal may not do this."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


async def resolve_access_level(
    db: AsyncSession,
    user: Optional[User],
    agent: Optional[Agent],
) -> Optional[str]:
    """Live resolution of a person's access level for one agent.

    Returns 'manage', 'use', or None. Never raises for a plain denial —
    callers decide the shape of the refusal.

    Deliberately a fresh read on every call so that revocation (permission
    row deleted, or user deactivated) takes effect on the next request
    rather than at next login.
    """
    if user is None or agent is None:
        return None
    if not getattr(user, "is_active", True):
        return None

    if getattr(user, "role", None) == "platform_admin":
        return MANAGE

    # Tenant isolation first: no cross-tenant access at any level.
    if getattr(agent, "tenant_id", None) != getattr(user, "tenant_id", None):
        return None

    if agent.creator_id == user.id:
        return MANAGE

    perms = await db.execute(
        select(AgentPermission).where(AgentPermission.agent_id == agent.id)
    )
    matched_levels: list[str] = []
    for perm in perms.scalars().all():
        matched = perm.scope_type == "company" or (
            perm.scope_type == "user" and perm.scope_id == user.id
        )
        if matched:
            matched_levels.append(perm.access_level or USE)
    if not matched_levels:
        return None
    # Least privilege: any 'use' grant caps the principal at 'use'. This never
    # widens what the previous first-row-wins resolution returned.
    return MANAGE if all(level == MANAGE for level in matched_levels) else USE


def session_visibility(session) -> str:
    """A session with no explicit visibility is private."""
    return getattr(session, "visibility", None) or VISIBILITY_PRIVATE


def can_read_session(user: Optional[User], session, access_level: Optional[str]) -> bool:
    """The whole retrieval law, in one predicate.

    - No access to the agent at all -> no.
    - Your own session -> yes.
    - Someone else's PRIVATE session -> no, at any access level. This is the
      rule that keeps the owner's WhatsApp conversation out of a staff
      member's context on a shared COO agent.
    - Someone else's SHARED session -> only with `manage`.
    """
    if user is None or access_level is None:
        return False
    if str(getattr(session, "user_id", "")) == str(user.id):
        return True
    if session_visibility(session) != VISIBILITY_SHARED:
        return False
    return access_level == MANAGE


def readable_sessions_clause(user: User, access_level: Optional[str]):
    """SQLAlchemy filter matching `can_read_session`, for list queries."""
    if access_level is None:
        # Impossible predicate — resolves to an empty result set.
        return ChatSession.id.is_(None)
    own = ChatSession.user_id == user.id
    if access_level != MANAGE:
        return own
    return or_(own, ChatSession.visibility == VISIBILITY_SHARED)


def filter_readable(user: User, sessions: Iterable, access_level: Optional[str]) -> list:
    return [s for s in sessions if can_read_session(user, s, access_level)]


async def assert_tool_allowed(
    db: AsyncSession,
    tool_name: str,
    agent_id: uuid.UUID,
    user_id: Optional[uuid.UUID],
) -> None:
    """Call-time authorization for a tool invocation.

    `agent_permissions` was previously consulted only when a route was
    entered or an agent list was rendered. This is the missing check at the
    moment of action.

    Raises PrincipalDenied on refusal.
    """
    if tool_name not in OWNER_LEVEL_TOOLS:
        return

    if user_id is None or str(user_id) == str(agent_id):
        # `execute_tool(..., user_id=user_id or agent_id)` used to smuggle the
        # agent's own id in as the principal. That is not a person.
        raise PrincipalDenied(
            f"'{tool_name}' requires an authenticated person; none was carried into this call."
        )

    agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    level = await resolve_access_level(db, user, agent)
    if level != MANAGE:
        raise PrincipalDenied(
            f"'{tool_name}' is an owner-level action. This principal holds "
            f"'{level or 'no'}' access to this agent."
        )
