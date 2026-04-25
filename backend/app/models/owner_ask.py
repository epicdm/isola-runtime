"""OwnerAskInFlight — Tier 1.5a live owner-ask state.

A row exists for the duration that Rex is waiting on the owner's
WhatsApp answer to a customer's unknown question. Inserted when the
LLM emits an `[ask_owner: ...]` marker; resolved when the owner WAs
back (status=answered or skipped) or when 24h passes (status=expired).

Indices: primary lookup is "newest awaiting row for this agent_id"
when an owner inbound arrives, so (agent_id, status, asked_at).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# Status values — string column rather than enum so adding new states
# (e.g. "owner_replied_quoted" for P1 quote-reply matching) doesn't need
# a new migration.
STATUS_AWAITING = "awaiting"
STATUS_ANSWERED = "answered"
STATUS_SKIPPED = "skipped"
STATUS_EXPIRED = "expired"


class OwnerAskInFlight(Base):
    __tablename__ = "owner_ask_in_flight"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True
    )
    customer_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(200))
    customer_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    owner_phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_AWAITING, index=True
    )
    asked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Owner's raw text reply (for audit + later style-adaptation rewrite).
    owner_reply: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index(
            "owner_ask_in_flight_agent_status_asked_at_idx",
            "agent_id",
            "status",
            "asked_at",
        ),
    )
