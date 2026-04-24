# Phase F.1 — Design Brief

**Scope:** unblock the "new tenant gets a working Rex out of onboarding" path.

**Status:** design — awaiting Eric sign-off before build.

**Duration estimate:** 1 week focused work.

**Maps to memories:**
- `project_isola_agent_sync_gap.md` — agent store split
- `project_isola_specialist_agent_build_strategy.md` — franchise model (EPIC-built archetypes)
- `project_isola_canonical_framing.md` — HR metaphor (Rex hires out of the box)
- Task #25 MVP-Rex skills registration, #36 MVP-BLOCKER Odoo shared instance

---

## 1. The problem

A Caribbean restaurant owner signs up to Isola today. Here's what happens:

1. Onboarding writes a row in apps/isola `agents` table named "Rex"
2. Owner clicks Home → iframe loads `app.isola.epic.dm/agents/<id>?embedded=1`
3. Clawith hits `GET /api/agents/<id>` on runtime → **404** (agent only exists in apps/isola DB)
4. **Workspace spins forever.**

Even if we fix the sync gap (step 3), once Rex loads:

5. Customer WhatsApps "what are your hours?"
6. Rex has no hours-lookup skill → falls back to generic LLM guess or "I don't know"
7. Customer is confused. Owner loses trust in the product on day one.

Fixing this requires **three coupled pieces** shipped together:

| Piece | What it does | Without it |
|---|---|---|
| **Agent-sync** | Mirror apps/isola agents into runtime so Clawith workspace loads | Workspace 404s |
| **Odoo tenant data** | Store the restaurant's menu / hours / location / bookings in a structured backend | Skills have no data to query |
| **Rex's 12 skills** | Hours, menu, booking, contact capture, etc. — the actual job Rex does | Rex can't answer anything specific |

---

## 2. What F.1 delivers

Three deliverables, tested together via extended dependent chain:

### F.1.a — Agent-sync endpoint + bridge integration
- New runtime endpoint `POST /api/internal/agents/ensure` (service-secret gated)
- Bridge handler (apps/isola) calls it on first `/dashboard/agents/*` hit for each apps/isola agent the user owns
- Uses same UUID — apps/isola agent id *is* runtime agent id
- Idempotent: repeat calls update (name, role_description, tone, welcome_message) and return `created: false`

### F.1.b — Odoo shared-tenant provisioning + 7-tool connector
- One Odoo.sh instance at `odoo.epic.dm` (or similar), multi-company
- Each Isola tenant = one Odoo company record (auto-provisioned at tenant-ensure)
- Modules enabled: **Sales + Website + Calendar + Invoicing + CRM**
- Runtime-side `OdooService` client (XML-RPC): 7 canonical tool methods (spec below)
- Per-tenant Odoo API key stored in runtime `ChannelConfig` (encrypted)

### F.1.c — Rex's 12 SKILL.md bundle, registered on the Rex template
- 12 markdown skills committed to `isola-runtime/backend/skills/rex/`
- Each links to Odoo tool(s) or runtime primitives (memory, draft, escalation, handoff)
- Runtime `skill_seeder.py` auto-installs them on any agent whose template is Rex-restaurant / Rex-hotel / Rex-clinic / Rex-retail / Rex-service
- Agent-ensure endpoint (F.1.a) triggers seed for newly-mirrored agents

---

## 3. Architecture — how they connect

```
Owner signs up → apps/isola onboarding →
  ├─ Vercel Postgres: tenants + agents rows (Drizzle) — unchanged
  └─ First hit /dashboard/agents/<id> →
       middleware → /api/runtime/bridge →
         ├─ ensureRuntimeTenant()  — Phase E.6d  ✅ done
         ├─ ensureUser()           — Phase E.2.1 ✅ done
         ├─ NEW: ensureAgent(id, tenant_id, name, role, template_hint)
         │     → POST runtime.epic.dm/api/internal/agents/ensure
         │     → runtime creates Agent row + seeds skills + links tools
         │     → runtime calls OdooService.ensure_company(external_id, name)
         │     → Odoo creates company + default products/services
         ├─ ensureRuntimeJWT()     — Phase E.2.1 ✅ done
         └─ redirect to iframe workspace

Customer WhatsApps "hours?" →
  runtime WA webhook (Phase B) →
    Rex agent receives message →
      hours-lookup skill fires →
        OdooService.get_hours(tenant_company_id) →
          Odoo XML-RPC → reads calendar.schedule →
          returns "Mon-Fri 8am-10pm, Sat-Sun 10am-11pm"
        → Rex composes reply with LLM using that data →
        → WhatsApp send
```

---

## 4. Schemas + APIs

### F.1.a — `POST /api/internal/agents/ensure`

**Request:**
```json
{
  "external_id": "uuid-from-apps-isola-agents-table",
  "tenant_runtime_id": "uuid-from-ensure-tenant",
  "creator_id": "uuid-from-ensure-user",
  "name": "Rex",
  "role_description": "Front desk receptionist for Eric Cafe",
  "template_hint": "rex-restaurant",
  "tone": 1,
  "welcome_message": "Hi — thanks for messaging Eric Cafe!",
  "escalation_keywords": ["manager", "complaint", "refund"]
}
```

**Response:**
```json
{
  "id": "uuid",
  "created": true,
  "template_id": "uuid-of-rex-restaurant",
  "skills_seeded": 12,
  "odoo_company_id": 42
}
```

**Idempotency:** `(external_id, tenant_runtime_id)` is the unique key. Second call updates name/role/tone/welcome/escalation_keywords in-place, returns `created: false`.

**Preconditions:**
- `tenant_runtime_id` must exist (caller should have called `ensure-tenant` first)
- `creator_id` must exist (caller should have called `ensure-and-mint-user` first)
- `template_hint` must map to an existing `agent_templates` row (role × vertical)

### F.1.b — Odoo connector (runtime-side tool methods)

`app.services.odoo_service.OdooService` — 7 canonical methods:

| Method | Purpose | Used by skill |
|---|---|---|
| `ensure_company(external_id, name, vertical) → company_id` | Create/get Odoo company for a tenant | Called during agent-ensure |
| `get_hours(company_id, day?) → {open, close, tz, is_open_now}` | Read operating hours | `hours-lookup` |
| `get_location(company_id) → {address, map_url, phone}` | Tenant contact details | `location-share` |
| `list_products(company_id, category?, limit=20) → [{id, name, price, description}]` | Menu / catalog / services | `menu-query` |
| `check_availability(company_id, start, end, party_size?) → {available, alternative_slots?}` | Calendar slot check | `reservation-book` |
| `create_booking(company_id, customer_phone, customer_name, start, party_size, notes?) → {booking_id, confirmation_url}` | Create reservation | `reservation-book`, `booking-confirm` |
| `upsert_contact(company_id, phone, name?, email?, tags?) → {contact_id}` | CRM contact | `contact-capture`, `review-ask` |

All methods take `company_id` as first arg. Runtime resolves it from `Agent.tenant_id → Tenant.odoo_company_id` cached on the tenant row (new column in F.1.a migration).

### F.1.c — Runtime schema additions

**Alembic migration `f1_agent_sync_odoo`:**
```sql
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS odoo_company_id INTEGER;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS odoo_api_key VARCHAR(200);  -- encrypted
ALTER TABLE agents ADD COLUMN IF NOT EXISTS external_id VARCHAR(200);  -- apps/isola agents.id
CREATE INDEX IF NOT EXISTS agents_external_id_idx ON agents (external_id);
ALTER TABLE agents ADD COLUMN IF NOT EXISTS tone INTEGER DEFAULT 1;  -- 0=formal 1=balanced 2=casual
ALTER TABLE agents ADD COLUMN IF NOT EXISTS welcome_message TEXT;  -- already possibly present; check
```

---

## 5. Rex's 12 skills — canonical bundle

Each skill lives at `isola-runtime/backend/skills/rex/<name>.md`. Format follows existing Clawith SKILL.md convention (trigger prompt + action + fallback).

| # | Skill | Trigger intent | Data source | Output |
|:-:|---|---|---|---|
| 1 | `hours-lookup` | "what time do you open/close", "are you open now" | OdooService.get_hours | Current + today's hours + "yes open" / "closed now, opens at X" |
| 2 | `location-share` | "where are you", "address", "how to find" | OdooService.get_location | Address + map link + phone |
| 3 | `menu-query` | "menu", "what do you have", "do you have <item>", "prices" | OdooService.list_products | Top 10 items OR specific item with price + description |
| 4 | `reservation-book` | "i want to book", "reservation", "table for 4 at 7pm" | OdooService.check_availability → create_booking | Confirmed booking OR alternative slots |
| 5 | `booking-confirm` | (proactive, cron) 4h before booking | n/a — reads existing booking | WhatsApp reminder with confirm-or-cancel buttons |
| 6 | `escalation-detect` | keywords: manager / complaint / urgent / refund / legal | Phase B.6 logic | Silent ping to owner's WA + return "I'm getting someone right now" |
| 7 | `contact-capture` | First-time customer OR inquiry without phone linkage | OdooService.upsert_contact | Save to CRM, silent ack |
| 8 | `review-ask` | (proactive, cron) 2h after visit | OdooService reads bookings | WA template message with review link |
| 9 | `draft-on-probation` | Low LLM confidence OR no matching skill fired | Runtime Draft table | Paperclip issue created, owner WA'd with draft-card |
| 10 | `knowledge-lookup` | FAQ-style question that doesn't match other skills | Tenant knowledge base (new Odoo custom module OR Postgres table) | Answer OR hand to #9 |
| 11 | `handoff-to-owner` | Customer explicitly asks for human OR sensitive topic | n/a — direct WA to owner | Silent ping, customer gets "owner will reach out" |
| 12 | `small-talk` | Hi / hello / thanks | None | Polite greeting that preserves agent voice + tenant tone |

**Notes:**
- Skills 5 + 8 are proactive (driven by Triggers) — they don't need user input, they run on schedule
- Skill 9 is the trust-building wedge — any skill that fails falls back to #9 (draft for owner)
- Skills 10's knowledge base is tenant-per-Odoo; initial seeding happens during onboarding (see Step 7 memory)

---

## 6. Odoo topology decisions

**Shared instance vs per-tenant:** shared. One Odoo.sh deployment at `odoo.epic.dm`, multi-company mode. Each Isola tenant = one Odoo company + one Odoo admin user. Keeps ops simple, keeps per-tenant cost near zero.

**Why not per-tenant Odoo:** cost scales linearly (each Odoo.sh instance ~EC$50/mo), ops complexity scales worse (24 customers = 24 Odoo instances to patch/upgrade/backup). Shared is the right MVP choice.

**Per-tenant data isolation:** Odoo's native multi-company access control. Each tenant's Odoo admin user has a record rule limiting visibility to their company's records. Runtime's XML-RPC calls authenticate as each tenant's admin user, so cross-tenant leakage is impossible at the Odoo level.

**Modules enabled on shared instance:**
- **Sales** (Sales Orders, Quotes) — for retail / service tenants
- **Website** — for landing pages (Mara uses later)
- **Calendar** — hours + bookings
- **Invoicing** — quotes + collections (Cash uses later)
- **CRM** — contact + lead management (Joey uses later)
- (Later) **Marketing Automation** — Mara

**Per-tenant provisioning flow (during F.1.a ensure-agent first call):**
1. Runtime ensure-agent endpoint reads `tenant.odoo_company_id`
2. If null → OdooService.ensure_company(external_id, tenant_name, vertical)
3. OdooService via admin XML-RPC:
   - Creates `res.company` row with tenant name + country (Dominica default)
   - Creates `res.users` admin scoped to that company with a generated password
   - Seeds default `res.partner` categories, `product.category`, `calendar.schedule`
   - For restaurants: seeds 3 default menu categories (Starters / Mains / Drinks) + 5 stub products
   - For clinics: seeds appointment types
   - For hotels: seeds room types
4. Persists `odoo_company_id` + encrypted `odoo_api_key` on tenant row
5. Agent auto-gets access via its tenant

**Defaults to ship:** 5 products/services per vertical (placeholder), default 9-5 hours, empty contact list. Owner edits from `/dashboard/catalog` + `/dashboard/hours` (apps/isola pages that hit Odoo through new proxy routes).

---

## 7. Build sequence (sub-phases within F.1)

Each sub-phase has its own commit + test. Chain test extends through all four.

| # | Scope | Duration | Depends on |
|:-:|---|:-:|---|
| **F.1.a-1** | Alembic migration `f1_agent_sync_odoo` + Tenant + Agent model fields | 2 hr | — |
| **F.1.a-2** | `POST /api/internal/agents/ensure` endpoint + test step | 3 hr | F.1.a-1 |
| **F.1.a-3** | apps/isola bridge integration: call ensure-agent for user's agents | 2 hr | F.1.a-2 |
| **F.1.b-1** | Odoo.sh instance stood up, modules enabled, admin access | 4 hr | — |
| **F.1.b-2** | `OdooService` XML-RPC client with 7 methods + unit tests | 6 hr | F.1.b-1 |
| **F.1.b-3** | `ensure_company` wired into F.1.a-2 (new tenants auto-provision Odoo) | 2 hr | F.1.a-2 + F.1.b-2 |
| **F.1.b-4** | apps/isola proxy routes: `/api/catalog`, `/api/hours`, `/api/contacts` → Odoo | 3 hr | F.1.b-2 |
| **F.1.c-1** | 12 SKILL.md files in `backend/skills/rex/` | 4 hr | F.1.b-2 (for skills that use Odoo) |
| **F.1.c-2** | skill_seeder updated to auto-install on Rex templates | 2 hr | F.1.c-1 |
| **F.1.c-3** | Test chain step `_phase_f1_rex_answers` — fake Meta, real Odoo, assert hours-query → reply with hours | 3 hr | All above |

**Total: ~31 hours = ~4 focused days. One week with interruption budget.**

---

## 8. Test plan

Extended dependent chain. New step in `tests/test_phase_b.py::test_phase_b_chain`:

```
Phase B.1-B.6 PASS  (existing)
Phase E.1 PASS       (existing)
Phase E.6a + E.6c PASS (existing)
Phase F.1.a PASS:
  - Agent-ensure endpoint creates runtime agent with same UUID as external_id
  - Repeat call updates name + returns created:false
  - Skill auto-seed fires once, not twice
Phase F.1.b PASS:
  - ensure_company auto-creates Odoo company on first agent-ensure
  - Subsequent agent-ensure for same tenant reuses same odoo_company_id
  - get_hours returns seeded 9-5 default
  - upsert_contact creates then updates
Phase F.1.c PASS:
  - Rex receives signed WA webhook with "what are your hours?"
  - Activity log shows hours-lookup skill fired
  - Mock Meta sees outbound message containing "9:00" or similar
```

**Odoo test strategy:** test chain runs against a DEDICATED test Odoo instance (separate subdomain or separate database on shared instance). Not prod tenant data.

---

## 9. Success criteria

F.1 is done when:

1. **A new onboarding creates a working agent with zero manual intervention.** Owner signs up → clicks Home → sees Rex's workspace → Rex's tabs are populated (Skills shows 12, Tools shows 7 Odoo tools linked) → customer WhatsApps "hours?" → Rex replies with actual hours from Odoo.
2. **Odoo serves as the business-data backbone.** Rex's catalog, hours, location, bookings, contacts all live there. Apps/isola `/dashboard/catalog` + `/hours` are thin editors that CRUD against Odoo.
3. **Test chain passes: B.1→B.6 + E.1 + E.6 + F.1.a→c.** Zero regressions.
4. **Works end-to-end for Eric Cafe** (EPIC's dogfood tenant) — you can ACTUALLY interact with Rex via WhatsApp and it answers correctly.

---

## 10. Risks + unknowns — flag for Eric

1. **Odoo XML-RPC latency from deepseek → Odoo.sh** — estimated 200-800ms per call. Not blocking for WhatsApp reply (which already takes 1-3s for LLM inference), but matters for proactive skills. Need to measure during F.1.b-2.
2. **Odoo.sh cost at scale** — shared instance is fine up to ~50 tenants. Past that, consider self-hosting or splitting. Not a F.1 concern but worth flagging.
3. **Skill evaluation accuracy** — skill routing inside Rex (which skill fires for which intent) depends on LLM classification quality. We trust Clawith's existing skill-selection logic from Phase C; if it's inadequate, we invest in better routing later.
4. **Odoo data model vs Isola concepts** — some mapping decisions (e.g. "reservation" as Odoo calendar event vs as sales quote vs as custom model) are subjective. Recommend: use native Odoo models (Sale Order for quotes, Calendar Event for bookings) and layer Isola-specific fields only where necessary.
5. **Per-tenant Odoo admin password security** — password generated at ensure_company, stored encrypted in runtime DB. If breached, tenant Odoo data is exposed. Standard encryption-at-rest + service-secret gating should mitigate. Not introducing anything worse than the existing OpenClaw api-key pattern.

---

## 11. Decisions Eric needs to make before build starts

| # | Decision | Locked |
|:-:|---|---|
| 1 | Odoo hosting | **Self-host on deepseek for now**, migrate to Odoo.sh when we hit the 30-tenant gate (matches  and synthesis-doc 30-tenant review pattern). Docker Compose + nginx + certbot + multi-company config. |
| 2 | Odoo hostname | **** |
| 3 | Rex skill inventory | **12 as listed** in §5 |
| 4 | Agent-sync mode | **Lazy** — triggered on first /dashboard/agents/* hit via bridge |
| 5 | Template mapping | **Explicit ** — apps/isola commits to a template at agent-create time, passes it through the bridge |

**Additional locked consequence of decision 1:** §6 (Odoo topology) now reads:

> One Docker Odoo on deepseek at . Runtime reaches it at  via the  Docker network (same host, no public hop). External users reach it through nginx → :8069 with TLS. Certbot provisions. Multi-company enabled. Modules on shared instance: Sales, Website, Calendar, Invoicing, CRM. Per-tenant data isolation via Odoo's native record rules. When ≥30 paying tenants, migrate DB to Odoo.sh and point  there.

**Ready to build. Starting F.1.a-1 (Alembic migration) immediately.**

---

## 12. What comes next (F.2 and beyond)

This brief covers **F.1 only**. Adjacent work after F.1 ships:

- **F.2** — Probation loop end-to-end (Task #26) + Morning Briefing (Task #29). Depends on F.1.c skills 9 + 10.
- **F.3** — Flow/Routing surface (tenant controller view). Depends on F.2 for enough trigger data to visualize.
- **F.4** — Ema ops layer. Depends on F.2-F.3 signal.
- **F.5** — Joey / Mara / Cash quality pass (parallelizable after F.1).
- **F.6** — Token-credit economy backend.

Each gets its own design brief when we start it.

---

## 13. Ready to build?

If Decisions 1-5 in §11 are locked, build order per §7 starts **F.1.a-1** (Alembic migration). One commit, one test, move on.

If any decision in §11 needs discussion, resolve before writing code.
