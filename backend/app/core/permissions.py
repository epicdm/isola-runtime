"""RBAC permission checking utilities."""

import uuid
from datetime import datetime, timezone
from typing import Tuple

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.session_privacy import MANAGE, resolve_access_level
from app.models.agent import Agent, AgentPermission
from app.models.user import User


async def check_agent_access(db: AsyncSession, user: User, agent_id: uuid.UUID) -> Tuple[Agent, str]:
    """Check if a user has access to a specific agent.

    Returns (agent, access_level) where access_level is 'manage' or 'use'.

    Access is granted if:
    1. User is platform admin → manage
    2. User is the agent creator → manage
    3. User has explicit permission (company/user scope) → from permission record

    Resolution is delegated to app.core.session_privacy.resolve_access_level so
    that routes, retrieval and call-time tool authorization all answer from the
    same live read of `agent_permissions`. A deactivated user or a deleted
    permission row is refused on the next request.
    """
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    level = await resolve_access_level(db, user, agent)
    if level is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this agent")
    return agent, level


def is_agent_creator(user: User, agent: Agent) -> bool:
    """Check if the user is the creator (admin) of the agent."""
    return agent.creator_id == user.id or user.role == "platform_admin"


def is_agent_expired(agent: Agent) -> bool:
    """Return True if the agent is manually marked expired or its expires_at is in the past."""
    if getattr(agent, 'is_expired', False):
        return True
    expires_at = getattr(agent, 'expires_at', None)
    if expires_at and datetime.now(timezone.utc) > expires_at:
        return True
    return False
