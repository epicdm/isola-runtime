"""f12_p4a: relax intent_routing scope XOR to at-least-one + add agent_id index

Revision ID: f12_p4a_relax_xor
Revises: f12_p4_intent_route
Create Date: 2026-05-22

Patch follow-up to f12_p4_intent_route:

Per PM ratification (Step 4 turn, option 2 + (b) gap close), relax the
scope CHECK from XOR (exactly-one of agent_id/vertical) to OR (at-least-one).
This enables future "agent override scoped within a vertical" rows where
both agent_id and vertical are set on the same row.

Also closes the (b) lookup-performance gap by adding a standalone partial
index on agent_id (for routing queries that filter by agent_id without
intent_pattern in the predicate).

Vertical-uniqueness index updated to apply ONLY to vertical-default rows
(agent_id IS NULL), so agent-within-vertical overrides do not conflict with
vertical defaults.

Forward-only patch. No data migration required — table is empty (Step 9 has
not yet seeded rows).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f12_p4a_relax_xor"
down_revision: Union[str, None] = "f12_p4_intent_route"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE intent_routing DROP CONSTRAINT IF EXISTS intent_routing_scope_xor")
    op.execute("""
        ALTER TABLE intent_routing
        ADD CONSTRAINT intent_routing_scope_atleast_one
        CHECK (agent_id IS NOT NULL OR vertical IS NOT NULL)
    """)
    op.execute("DROP INDEX IF EXISTS uq_intent_routing_vertical_intent")
    op.execute("""
        CREATE UNIQUE INDEX uq_intent_routing_vertical_intent
        ON intent_routing(vertical, intent_pattern)
        WHERE vertical IS NOT NULL AND agent_id IS NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_intent_routing_agent_id
        ON intent_routing(agent_id)
        WHERE agent_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_intent_routing_agent_id")
    op.execute("DROP INDEX IF EXISTS uq_intent_routing_vertical_intent")
    op.execute("""
        CREATE UNIQUE INDEX uq_intent_routing_vertical_intent
        ON intent_routing(vertical, intent_pattern)
        WHERE vertical IS NOT NULL
    """)
    op.execute("ALTER TABLE intent_routing DROP CONSTRAINT IF EXISTS intent_routing_scope_atleast_one")
    op.execute("""
        ALTER TABLE intent_routing
        ADD CONSTRAINT intent_routing_scope_xor
        CHECK (
            (agent_id IS NOT NULL AND vertical IS NULL)
            OR
            (agent_id IS NULL AND vertical IS NOT NULL)
        )
    """)
