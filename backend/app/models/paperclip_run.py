"""Durable run ledger for POST /api/internal/paperclip-run (INT-001).

One row per Paperclip heartbeat runId.

The UNIQUE constraint on run_id is the idempotency primitive. Concurrent
duplicate deliveries of the same runId race on that index; exactly one INSERT
wins and every loser is answered from the winner's row without executing
anything and without writing a second comment. Idempotency that lives in
application memory is not idempotency -- two workers, or one worker restarted
mid-run, would both execute.

The row is also correlation evidence (b) in the four-source test: it carries
the same run_id the Paperclip run record carries, and the paperclip_agent_id
it was issued for.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PaperclipRun(Base):
    """Ledger row for one bounded internal agent run."""

    __tablename__ = "paperclip_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Idempotency key. Unique -- this is the whole mechanism.
    run_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)

    # Correlation: same identifiers Paperclip holds for the run.
    paperclip_agent_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    paperclip_company_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    issue_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Runtime-side agent this resolved to, when it resolved.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # accepted | running | completed | failed | timed_out
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="accepted", index=True)

    # The scoped service identity the run executed as. Never a creator, never
    # an owner. Recorded so acceptance can assert it rather than assume it.
    principal_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    principal_label: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Enforcement counters, for evidence.
    provider_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    denied_tools: Mapped[str | None] = mapped_column(Text, nullable=True)

    # At most one comment per run, enforced by a conditional UPDATE on this
    # column rather than by remembering in Python.
    comment_posted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    comment_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Redacted failure reason. Never carries provider or secret material.
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
