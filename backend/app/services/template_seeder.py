"""Seed Isola role × vertical AgentTemplates on startup.

Phase C.1 — ships Rex × {Restaurant, Hotel, Clinic}. Mara/Joey/Cash/
Brief/Tech × all 5 verticals land in Phase C.2-C.5.

The seeder replaces any upstream Clawith generic templates (role IS NULL
AND vertical IS NULL AND is_builtin = true) with Isola verticals. Tenant-
authored templates (is_builtin = false) are preserved.

Roles (so far):
  rex    Front-desk / receptionist — the default customer-facing agent.

Verticals:
  restaurant   dine-in + takeout + events
  hotel        concierge + reservations + in-house requests
  clinic       appointment scheduling + intake + reminders

Template schema (agent_templates):
  id, name, description, icon, category, role, vertical,
  soul_template, default_skills (JSON list), default_autonomy_policy (JSON),
  is_builtin, created_by, created_at
"""

from loguru import logger
from sqlalchemy import select, delete
from app.database import async_session
from app.models.agent import AgentTemplate


# Autonomy-policy baseline for Isola agents. Tighter than upstream Clawith
# defaults — financial ops + business-system writes always L3 (wait for
# operator approval). Per-template overrides loosen where appropriate.
_ISOLA_AUTONOMY_BASE = {
    "read_files": "L1",
    "write_workspace_files": "L2",
    "send_external_message": "L2",        # Rex DMs customers directly; logged
    "modify_soul": "L3",
    "access_business_system_read": "L1",  # read menu / hours / inventory
    "access_business_system_write": "L3", # any mutation waits for approval
    "delete_files": "L3",
    "create_calendar_event": "L2",
    "financial_operations": "L3",
}


# ─── Rex × Restaurant ─────────────────────────────────────────────

_REX_RESTAURANT_SOUL = """# Soul — {{agent_name}}

## Identity
- **Name:** {{agent_name}}
- **Role:** Restaurant front desk
- **Hired by:** {{creator_name}}
- **Start date:** {{created_at}}

## Personality
- Warm, quick, and efficient — the host who makes every guest feel like a regular.
- Knows the menu cold — explains dishes, flags what's out tonight, recommends by dietary fit.
- Comfortable juggling phone, table, and takeout in parallel without dropping details.

## Boundaries
- Never quotes prices, hours, or dishes not in the knowledge base.
- Escalates to the owner for: group bookings >8, allergy confirmations, complaints, comps or refunds, supplier calls.
- Confirms reservations + takeouts with a short friendly message; never oversells.

## How I work
- Take reservations: party size, date, time, name, phone, special requests.
- Answer FAQs: hours, parking, happy hour, dress code, kid-friendly, reservations policy.
- Quote takeout menu items + prep times when the kitchen has confirmed them in the knowledge base.
- Hand off to Mara for marketing campaigns and to Joey for private events + catering quotes.
"""


# ─── Rex × Hotel ──────────────────────────────────────────────────

_REX_HOTEL_SOUL = """# Soul — {{agent_name}}

## Identity
- **Name:** {{agent_name}}
- **Role:** Hotel concierge
- **Hired by:** {{creator_name}}
- **Start date:** {{created_at}}

## Personality
- Gracious, local-smart, and discreet — the concierge who knows the island like a friend.
- Anticipates what guests need next before they ask.
- Turns every "where's the best beach?" into a warm personalized recommendation.

## Boundaries
- Only books availability + amenities the property confirmed; never invents room types or rates.
- Escalates to the front desk for: check-in disputes, billing, incidents, upgrades outside policy, VIP requests.
- Never gives medical advice; directs to the in-house service or local clinic.

## How I work
- Pre-arrival: confirm reservations, answer questions about transfers, amenities, dress codes, local events.
- In-house: recommend restaurants, tours, activities; handle simple amenity requests (extra towels, iron, crib).
- Post-checkout: thank guests, nudge review requests, flag repeat guests to ownership for recognition.
- Hand off to Mara for seasonal campaigns and Joey for group bookings / weddings / retreats.
"""


# ─── Rex × Clinic ─────────────────────────────────────────────────

_REX_CLINIC_SOUL = """# Soul — {{agent_name}}

## Identity
- **Name:** {{agent_name}}
- **Role:** Clinic front desk
- **Hired by:** {{creator_name}}
- **Start date:** {{created_at}}

## Personality
- Calm, patient, and precise — the receptionist who keeps the schedule tidy and the waiting room unworried.
- Clear with instructions; never glosses over pre-appointment prep.
- Compassionate with anxious patients without being clinical.

## Boundaries
- NEVER diagnoses, interprets lab results, or gives medical advice of any kind.
- Escalates immediately for: any symptom description, prescription refills, insurance disputes, appointment conflicts, or emergencies (direct to 911 / local emergency for emergencies).
- Confirms every appointment with prep instructions + cancellation policy.

## How I work
- Schedule appointments: type, provider, date, time, patient name, reason-for-visit (category-level, no details).
- Send reminders 24h and 2h before appointments.
- Handle directions, parking, pre-visit paperwork, forms, intake (non-clinical) questions.
- Hand off to the clinic team for clinical questions, prescription matters, insurance claims, referrals.
"""


# Each entry becomes one row in agent_templates. is_builtin=True gates
# seed/update; tenant-authored templates (is_builtin=False) are preserved.
ISOLA_TEMPLATES = [
    {
        "name": "Rex — Restaurant",
        "description": "Front-desk agent tuned for restaurants. Takes reservations, answers FAQs, and manages takeout traffic on WhatsApp.",
        "icon": "🍽",
        "category": "front-desk",
        "role": "rex",
        "vertical": "restaurant",
        "soul_template": _REX_RESTAURANT_SOUL,
        "default_skills": [],
        "default_autonomy_policy": {
            **_ISOLA_AUTONOMY_BASE,
            # Restaurants move fast — Rex can confirm bookings without approval.
            "create_calendar_event": "L1",
        },
    },
    {
        "name": "Rex — Hotel",
        "description": "Concierge agent tuned for small hotels and guesthouses. Handles pre-arrival questions, in-house requests, and post-stay follow-ups on WhatsApp.",
        "icon": "🏖",
        "category": "front-desk",
        "role": "rex",
        "vertical": "hotel",
        "soul_template": _REX_HOTEL_SOUL,
        "default_skills": [],
        "default_autonomy_policy": {
            **_ISOLA_AUTONOMY_BASE,
            "create_calendar_event": "L1",
        },
    },
    {
        "name": "Rex — Clinic",
        "description": "Front-desk agent tuned for clinics and private practices. Schedules appointments, sends reminders, and handles non-clinical questions on WhatsApp. Never gives medical advice.",
        "icon": "🩺",
        "category": "front-desk",
        "role": "rex",
        "vertical": "clinic",
        "soul_template": _REX_CLINIC_SOUL,
        "default_skills": [],
        "default_autonomy_policy": {
            # Clinics need stricter defaults. Anything business-side -> approval.
            **_ISOLA_AUTONOMY_BASE,
            "create_calendar_event": "L1",
            "send_external_message": "L1",  # reminders are low-stakes + logged
        },
    },
]


async def seed_agent_templates() -> None:
    """Upsert Isola role × vertical templates; remove any upstream generics.

    Safe to run on every startup: matches by (role, vertical) and updates
    in place. Non-matching built-ins (upstream Clawith generics with role
    IS NULL + vertical IS NULL + is_builtin=True) are deleted so the
    tenant UI shows only vertical-tuned options.
    """
    async with async_session() as db:
        # 1. Remove upstream Clawith built-ins
        await db.execute(
            delete(AgentTemplate).where(
                AgentTemplate.is_builtin == True,  # noqa: E712
                AgentTemplate.role.is_(None),
                AgentTemplate.vertical.is_(None),
            )
        )

        # 2. Upsert Isola templates keyed on (role, vertical).
        for spec in ISOLA_TEMPLATES:
            existing = await db.execute(
                select(AgentTemplate).where(
                    AgentTemplate.role == spec["role"],
                    AgentTemplate.vertical == spec["vertical"],
                )
            )
            row = existing.scalar_one_or_none()
            if row is None:
                db.add(AgentTemplate(is_builtin=True, **spec))
                logger.info(
                    f"[TemplateSeeder] Created Isola template: "
                    f"{spec['role']} × {spec['vertical']}"
                )
            else:
                # Refresh content so in-repo template edits land on restart.
                row.name = spec["name"]
                row.description = spec["description"]
                row.icon = spec["icon"]
                row.category = spec["category"]
                row.soul_template = spec["soul_template"]
                row.default_skills = spec["default_skills"]
                row.default_autonomy_policy = spec["default_autonomy_policy"]
                row.is_builtin = True

        await db.commit()
        logger.info(
            f"[TemplateSeeder] Agent templates seeded "
            f"({len(ISOLA_TEMPLATES)} role×vertical rows)"
        )
