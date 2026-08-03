"""ISOLA GLUE (not upstream): dedicated claim table for the structured bridge.

Revision ID: add_structured_bridge_requests
Revises: add_isola_bridge_requests
Create Date: 2026-08-02

Ratified by `dec-clawith-structured-claim-isolation-dedicated-table-2026-08-02`.
That decision's suggested revision id, ``add_isola_structured_bridge_requests``
(36 chars), exceeds this repository's ``alembic_version.version_num`` column
width (``VARCHAR(32)``, Alembic's default, unmodified anywhere in this repo's
migration history — confirmed by attempting the longer id against a live
Postgres 15 test database, which failed with
``StringDataRightTruncationError``). This revision drops the redundant
``isola_`` prefix (the repository is already ``isola-runtime``) to fit in 30
characters; the table itself keeps its full ratified name,
``isola_structured_bridge_requests``, unaffected by the revision-id shortening.

WHY A NEW TABLE INSTEAD OF REUSING isola_bridge_requests
----------------------------------------------------------
The structured bridge (`isola_bridge_structured.py`) and the legacy
asynchronous bridge (`isola_bridge_v2.py`) previously shared
``isola_bridge_requests``. Legacy v2 accepts caller-supplied
``stable_request_id`` and ``metadata_labels`` from any holder of the shared
``X-Isola-Secret``. That let a v2 caller pre-claim the structured route's
``f"structured:{correlation_id}"`` namespace (permanently poisoning a future
structured request with a non-retryable 409) or supply the publicly
derivable empty-tool-set digest (``4f53cda18c2baa0c``) to make the structured
route join the caller's own run/session and leak its reply back to
Foundation as a schema-valid customer response. See
`defect-clawith-structured-bridge-v2-request-table-collision-2026-08-02` and
`evidence-clawith-cross-route-claim-isolation-design-audit-2026-08-02` for
the full audit and threat model.

Both routes already own a private, hand-written copy of every SQL statement
against the shared table (no ORM model, no shared claim service exists for
either route), so giving the structured route its own relation is a change
to five SQL string literals in the one file that owns them plus this
additive migration — not a parallel model/service stack.

This migration creates ONLY the new relation and its indexes. It does not
ALTER, UPDATE, DELETE or backfill ``isola_bridge_requests`` or any other
existing table. Legacy v2 keeps using the shared table, byte-identical.

WHY NO FOREIGN KEYS (deliberate, matching the legacy table's own posture)
----------------------------------------------------------------------------
* The claim ledger must be allowed to outlive ``chat_sessions`` /
  ``agent_runs`` / ``chat_messages`` retention — a claim row is evidence of
  what was authorized for a turn, independent of how long the runtime keeps
  the turn's own working state.
* Tenant and agent validity are already checked BEFORE claim creation (the
  structured route resolves ``designated_agent_id`` against ``agents`` and
  derives ``tenant_id`` from it — see ``_resolve_tenant`` in
  ``isola_bridge_structured.py``), so an FK would only re-check what the
  application already verified, while adding a row lock on every insert and
  a validation scan against those tables.
* ``isola_bridge_requests`` (the legacy table this mirrors) already has zero
  foreign keys in either direction; this preserves that symmetry rather than
  introducing an inconsistent constraint model between the two bridges.
* Rollback and insertion must never acquire locks on hot operational rows —
  an FK on ``agent_id``/``session_id``/``run_id`` would contend with the
  runtime's own writes to those tables under load.

RETENTION, NO CLEANUP JOB
--------------------------
``expires_at`` is populated by the application at claim time
(``ISOLA_BRIDGE_STRUCTURED_RETENTION_HOURS``, default 24h) but this slice
introduces no automatic deletion or expiry job — matching the current
codebase, which has none for the legacy table either. Rows do not disappear
on their own; a future cleanup mechanism is a separate decision.

DOWNGRADE IS GUARDED, NOT UNCONDITIONAL
------------------------------------------
``downgrade()`` refuses to drop the table while it contains any row. Once a
structured claim exists, a plain ``alembic downgrade`` here would destroy
the durable record that a ``correlation_id`` was already claimed and let a
retried request start a SECOND reasoning run for the same logical turn —
exactly the duplication this table exists to prevent. Downgrade is therefore
safe ONLY while the table is empty (e.g. immediately after this migration is
applied and before any structured traffic exists). Once structured claims
exist, the recommended production rollback is IMAGE-ONLY: restore the prior
container image. The previous code never names this relation, so the table
sits inert with zero data loss and nothing to reconcile on re-apply.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "add_structured_bridge_requests"
down_revision: str | None = "add_isola_bridge_requests"
branch_labels = None
depends_on = None

_TABLE = "isola_structured_bridge_requests"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_policy_digest", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("external_conversation_id", sa.Text(), nullable=True),
        sa.Column("contact_ref", sa.Text(), nullable=True),
        sa.Column("clawith_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("initiating_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("terminal_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata_labels",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint(
            "state IN ('accepted','running','completed','failed','cancelled','expired','rejected')",
            name="ck_isola_structured_bridge_requests_state",
        ),
        sa.CheckConstraint(
            "length(correlation_id) BETWEEN 1 AND 180",
            name="ck_isola_structured_bridge_requests_correlation_len",
        ),
        sa.CheckConstraint(
            "tool_policy_digest ~ '^[0-9a-f]{16}$'",
            name="ck_isola_structured_bridge_requests_digest_shape",
        ),
    )
    # The claim-isolation invariant: one (tenant, correlation_id) pair can
    # never win two claims. This is the sole conflict-inference target for
    # the structured route's `INSERT ... ON CONFLICT (tenant_id,
    # correlation_id) DO NOTHING` — no other route/table can name it.
    op.create_unique_constraint(
        "uq_isola_structured_bridge_requests_tenant_correlation",
        _TABLE,
        ["tenant_id", "correlation_id"],
    )
    op.create_index(
        "ix_isola_structured_bridge_requests_open",
        _TABLE,
        ["state", "expires_at"],
    )
    op.create_index(
        "ix_isola_structured_bridge_requests_session",
        _TABLE,
        ["session_id"],
    )
    op.create_index(
        "ix_isola_structured_bridge_requests_agent_accepted",
        _TABLE,
        ["agent_id", sa.text("accepted_at DESC")],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in inspector.get_table_names():
        row_count = bind.execute(sa.text(f"SELECT count(*) FROM {_TABLE}")).scalar_one()
        if row_count > 0:
            raise RuntimeError(
                f"refusing to downgrade: {_TABLE} contains {row_count} row(s). "
                "Dropping populated structured claims would let a retried "
                "correlation_id start a second reasoning run. Downgrade is "
                "safe only while this table is empty; once structured claims "
                "exist, roll back by restoring the prior container image "
                "instead of running `alembic downgrade` here."
            )

    op.drop_index("ix_isola_structured_bridge_requests_agent_accepted", table_name=_TABLE)
    op.drop_index("ix_isola_structured_bridge_requests_session", table_name=_TABLE)
    op.drop_index("ix_isola_structured_bridge_requests_open", table_name=_TABLE)
    op.drop_constraint(
        "uq_isola_structured_bridge_requests_tenant_correlation", _TABLE, type_="unique"
    )
    op.drop_table(_TABLE)
