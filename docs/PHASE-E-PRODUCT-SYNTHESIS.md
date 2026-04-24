# Phase E — Product Synthesis: merging Clawith's depth with Isola's clarity

**Decision output for Audience C** (everyone — SMB owner + operator + agency + EPIC team). Single UI. Progressive disclosure. OpenClaw as a premium privacy tier.

## TL;DR

Isola's `apps/isola` is a tight customer-facing funnel (marketing → 6-step HR-metaphor onboarding → business dashboard) but has a weak agent-workspace that can't tell the operator what the agent is doing. Clawith's UI is an 11-tab operator cockpit (soul, memory, triggers, tools, skills, A2A, workspace, chat-stream, activity log, approvals, settings) with no buyer funnel and no business-ops surfaces. **The merge: one UI at `isola.epic.dm`, apps/isola's front door intact, Clawith's depth progressively disclosed per-agent behind an Advanced toggle. OpenClaw (the edge-runtime we stripped in Phase A.2) returns as a premium privacy-tier differentiator for clinics / law / banks.**

---

## 1. What Isola is today

### Market + brand
- Caribbean SMB — restaurants, hotels, clinics, retail, service businesses
- Positioning: *"Your AI front desk on WhatsApp"*
- Pricing: EC$149 / 249 / 449 (Starter / Pro / Business)
- Brand voice: HR — *hire, team, probation, drafts-on-probation*
- Wedge (per `project_isola_verticals.md`): Restaurants + Hotels + Clinics first

### Current `apps/isola` surface

| Area | Routes |
|---|---|
| Marketing | `/`, `/for/{clinics,hotels,restaurants}`, `/how-it-works`, `/pricing`, `/privacy`, `/terms` |
| Auth | `/auth/[pathname]` (Better-Auth: register + login) |
| Onboarding | `/onboarding` — 6-step quiz: business → role → hours → tone → WhatsApp → launch |
| Dashboard | `/dashboard/{bookings,catalog,channels,contacts,drafts,hours,inbox,insights,integrations,knowledge,outbound,settings,team,whatsapp,billing}` |
| Agent pages | `/dashboard/agents` (list), `/dashboard/agents/[id]` (legacy detail), `/dashboard/agent/[agentId]` (agent-centric workspace), `/dashboard/agents/new` |
| Assistant | `/dashboard/ema`, `/dashboard/ema/reports`, `/dashboard/ema/settings` |
| QA | `/qa`, `/qa/admin` |

### Strengths to preserve
- Caribbean-specific marketing funnel (`/for/clinics` etc.)
- 6-step HR-metaphor onboarding — this is a genuine USP
- Business-ops surfaces (catalog, bookings, contacts, hours) — SMB owners live here
- Drafts-on-probation — the trust-building wedge for new agents
- Next.js 15 + Vercel + shadcn/ui = fast to iterate

### Weaknesses to fix
- **Agent workspace is shallow.** 5 tabs (ported from a Lovable prototype). Owner can edit settings but can't see what the agent is doing or thinking.
- **No chat transparency.** No streaming, no tool-call viz. Owner doesn't know what the LLM did between receiving and replying.
- **No soul / memory editor.** Agent personality is baked at onboarding; can't be directly edited without SQL.
- **No trigger editor.** Can't add "send review request 2h after reservation" or "morning briefing at 9am" without engineer help.
- **No activity log.** "Did the agent actually send that reply?" requires opening Chatwoot or BFF logs.
- **No approvals queue.** L3 actions from the autonomy policy exist in the backend but are invisible to operators.

---

## 2. What Clawith is

### Market + brand (upstream)
- Chinese enterprise SaaS for Feishu / WeCom / DingTalk knowledge workers
- Positioning: *"Digital Employee Platform"* — hire agents as team members
- Linear-style dark UI, aesthetically confident
- Language: Chinese primary, English ~93% complete via `en.json`

### Current UI surface (at `staging.isola.epic.dm`)

| Area | Routes |
|---|---|
| Auth | `/login`, `/forgot-password`, `/reset-password`, `/verify-email`, `/sso/entry`, `/setup-company` |
| Main | `/plaza` (agent marketplace), `/dashboard`, `/agents/new`, `/agents/:id`, `/messages` |
| Admin | `/enterprise` (LLM models + members), `/invitations`, `/admin/platform-settings` |

### The 11-tab agent workspace (`AgentDetail.tsx`, 5,917 lines)

| Tab | What it shows |
|---|---|
| **Status** | Token usage, last-activity, model config, quick actions |
| **Aware** | Pulse engine: list + edit cron / interval / poll / on_message triggers |
| **Mind** | `soul.md` editor + `memory.md` editor + reflection viewer |
| **Tools** | Enabled-tool catalog (MCP + built-in) per-agent, per-tool config |
| **Skills** | File-level skill browser — add / edit / remove SKILL.md files |
| **Relationships** | A2A graph: which agents this one can message, org scope |
| **Workspace** | Full file browser of `agent/<id>/` directory (uploads, memory, skills, soul, todo, state) |
| **Chat** | WebSocket streaming chat with typewriter output + tool-call viz + thinking trail |
| **Activity Log** | Granular auto-refreshing event stream (every LLM call, every tool, every trigger, every approval) |
| **Approvals** | L3 autonomy request queue — owner approves / rejects high-stakes actions here |
| **Settings** | Autonomy policy, expiry, token caps, context window, heartbeat |

### Strengths worth porting forward
- The 11-tab workspace is **6–12 months of work** to recreate from scratch
- Streaming chat + tool-call viz is product-grade
- Approvals queue operationalizes L3 autonomy (without it, L3 is a setting nobody sees)
- Activity log is the "is the agent actually working" answer in one view
- Trigger editor (Aware tab) is what turns passive agents into proactive ones
- Plaza marketplace is credible Team/Suite-tier growth material
- Zustand + react-query state management is clean

### Weaknesses that justify dropping or hiding for the SMB buyer
- Terminology assumes a technical operator: *autonomy_policy, context_window_size, A2A relationships*
- No marketing funnel / no vertical positioning / no Caribbean presence
- No business-ops surfaces (no catalog, no bookings, no contacts, no hours)
- Invitation-gated signup assumes enterprise adoption model
- "Enterprise Settings" exposes LLM model config to every platform-admin (wrong for multi-tenant)
- Plaza marketplace is premature for MVP
- Chinese strings leak through in ~7% of UI text
- Dead code still in repo: `OpenClawSettings.tsx`, `AgentBayLivePanel.tsx`

---

## 3. Feature matrix

### Where they overlap — pick the better one

| Feature | apps/isola | Clawith | Merged product wins |
|---|---|---|---|
| Register / login | Better-Auth, clean | Enterprise email flow | **apps/isola** (better UX) |
| Agent create | Quiz, vertical-aware | 5-step wizard, generic | **apps/isola** — wire to `/api/agents/provision-vertical` |
| Chat UI | Basic | Streaming + tool-call viz | **Clawith** (port to apps/isola) |
| Agent settings | Keywords / owner / probation | Autonomy / tokens / expiry | **Combined** — Isola fields + Clawith fields on one page |

### Where only Isola has it (preserve)

Marketing site + vertical pages · 6-step HR onboarding · Drafts-on-probation · Catalog · Bookings · Contacts · Hours · Knowledge · Outbound campaigns · Insights · Ema (EPIC assistant) · QA-bot admin · Channels config · Billing

### Where only Clawith has it (port in priority order)

| Capability | Priority | Why | Rough effort |
|---|:---:|---|:---:|
| Agent Status card (tokens, activity, model) | **High** | Visible on every agent page | 1-2d |
| Streaming chat + tool-call viz | **High** | Trust-building: "what did my agent do?" | 3-5d |
| Activity Log | **High** | Operator answer to "is it working?" | 2-3d |
| Approvals queue | **High** | Operationalizes L3 autonomy | 2-3d |
| Soul / Memory editor | **Medium** | Power users + agencies love it | 2-3d |
| Triggers editor | **Medium** | Proactive agents (morning briefings, review asks, follow-ups) | 3-4d |
| Workspace file browser | **Medium** | Deep debugging + knowledge edit | 2-3d |
| Per-agent Skills management | **Medium** | Skill tuning | 2-3d |
| Per-agent Tools management | **Medium** | Autonomy tuning | 2-3d |
| A2A Relationships graph | **Low** | Multi-agent orgs (Team/Suite tier) | 3-4d |
| Plaza marketplace | **Medium** | MVP-scope — surfaces the 30 role×vertical templates as a "browse + hire" catalog; tenants find agents beyond onboarding | 5-7d |
| Enterprise Settings (LLM models) | **Low** | Platform-admin only; CLI is fine for v1 | 1-2d |

### What to drop outright

`OpenClawSettings.tsx` (replaced by Runtime Mode setting) · `AgentBayLivePanel.tsx` (Alibaba sandbox, deleted in A.1b) · Invitation-only signup gate · Chinese default locale

---

## 3a. Explicit audit: every Clawith page, ruled in or out

Eric's directive: *"expose all from Clawith OR indicate which will not be in the Isola UI."* This is the complete list. Nothing is quietly dropped.

### ✅ Ported into Isola UI (behind Advanced toggle where applicable)

| Clawith surface | Lands in Isola as | Notes |
|---|---|---|
| Dashboard | `/dashboard` (apps/isola) — already exists | apps/isola wins, Clawith version deprecated |
| AgentDetail **Status** tab | `/dashboard/agent/[agentId]` Overview tab | Default-visible |
| AgentDetail **Inbox** (chat history) | `/dashboard/agent/[agentId]` Inbox tab | Default-visible |
| AgentDetail **Settings** | `/dashboard/agent/[agentId]` Settings tab | Default-visible; Isola fields + Clawith fields merged |
| AgentDetail **Activity Log** | Same page, Advanced | Advanced toggle — E.2 |
| AgentDetail **Chat Stream** | Same page, Advanced | Advanced toggle — E.2 (typewriter + tool-call viz) |
| AgentDetail **Approvals** | Same page, Advanced | Advanced toggle — E.2 (L3 autonomy queue) |
| AgentDetail **Soul & Memory (Mind)** | Same page, Advanced | Advanced toggle — E.3 |
| AgentDetail **Triggers (Aware)** | Same page, Advanced | Advanced toggle — E.3 |
| AgentDetail **Tools** | Same page, Advanced | Advanced toggle — E.3 |
| AgentDetail **Skills** | Same page, Advanced | Advanced toggle — E.3 |
| AgentDetail **Workspace** (file browser) | Same page, Advanced | Advanced toggle — E.3 |
| AgentDetail **Relationships** (A2A) | Same page, Advanced | Advanced toggle — E.4 |
| Plaza (marketplace) | `/dashboard/marketplace` | **MVP-scope** per decision #1 |
| Messages (cross-agent inbox) | `/dashboard/inbox` (apps/isola) | apps/isola version is better |
| CompanySetup | Replaced by apps/isola's 6-step onboarding | apps/isola wins |
| AgentCreate wizard | Replaced by apps/isola `/dashboard/agents/new` + `provision-vertical` | apps/isola wins |

### 🔁 Replaced by Isola-native equivalents (same capability, better-branded)

| Clawith surface | Isola equivalent |
|---|---|
| Login / Register | `/auth/[pathname]` (Better-Auth) |
| ForgotPassword | Better-Auth flow |
| ResetPassword | Better-Auth flow |
| VerifyEmail | Better-Auth flow |
| OpenClawSettings page | **Runtime Mode** section in `/dashboard/settings` (Hosted / Edge toggle — see §5) |

### ⏸️ Deferred (not MVP — surfaces later when a tenant asks)

| Clawith surface | Why deferred | When |
|---|---|---|
| **SSO Entry** (Feishu/enterprise SAML) | Isola MVP is self-serve. SSO matters when a Caribbean bank / credit union asks for it. | Phase I (enterprise-tier feature) |
| **Invitation Codes** page | MVP is single-operator-per-tenant. Team invites are a Team/Suite tier feature. | Team/Suite tier (post-MVP) |
| **User Management** page (invite teammates) | Same reason — tenant-side team invites land with Team tier. | Team/Suite tier (post-MVP) |

### 🧬 Partial port — useful fields extracted, integrated natively into apps/isola

Some Clawith pages have **good options inside them** even if the page as a whole is the wrong shape for SMB tenants. Those fields get lifted and re-implemented in the corresponding apps/isola settings surface, styled natively.

| Clawith surface | Fields/options worth keeping | Where they land in apps/isola |
|---|---|---|
| **Enterprise Settings** (tenant-admin page) | Company timezone · logo upload · token-spend cap (per-agent + tenant-wide) · agent-default autonomy policy · retention period for chat history · heartbeat schedule defaults · operator language preference | `/dashboard/settings` — new "Company" section alongside existing Business / Channels / Billing subsections |
| **User Management** (team roles) | Role names · per-role permissions matrix (future Team tier) | `/dashboard/team` — merges with existing Team surface when Team tier opens |
| **Platform Dashboard** (ops metrics per tenant) | Per-agent status badges · global token usage graph · active-session count | Integrated into `/dashboard` landing as an owner-facing "Operations" card (MVP surface, not a separate page) |
| **Invitation Codes** (operator onboarding) | Deferred to Team tier per §3a; but the *concept* of an invite link stays (apps/isola already has a lighter version for the QA-bot flow) | Reuse apps/isola's existing invite-link pattern when Team tier opens |

### ❌ Dropped — will NOT surface in Isola UI ever

| Clawith surface | Why |
|---|---|
| **AgentBay Live Panel** (browser sandbox) | Deleted in Phase A.1b. Alibaba Cloud paid service; no Caribbean SMB use case. If we ever need browser automation, it goes via Playwright or an MCP server — not AgentBay. |
| **Enterprise Settings — LLM model management UI** (only this specific field, not the whole page — see Partial Port above) | Platform-level concern, NOT tenant-facing. LLM models managed centrally by EPIC via CLI / env. Exposing this to tenants is a footgun. |
| **Admin Companies** (cross-tenant admin) | EPIC-internal. Multi-tenant admin doesn't belong in a single tenant's UI. |
| **Chinese default locale** | English default everywhere. `zh.json` stays in repo for tenant toggle, but not the default; not marketed as multi-lingual in MVP. |
| **Invitation-only signup gate** | Isola MVP is open self-serve. Anyone with a payment method registers. |

---

## 3b. Integration principle — port capabilities, not components

**The rule: no tenant should be able to tell which features came from Clawith versus built from scratch in Isola.**

Concretely:

- **Visual:** Every ported tab rebuilds in apps/isola's existing stack: **shadcn/ui + Tailwind + lucide-react icons + Sonner toasts**. We do NOT import Clawith's CSS or its `@tabler/icons-react` or its handwritten modal components. Components match whatever's already in `apps/isola/src/components/ui/`.
- **Voice:** HR metaphor everywhere — *hire, team, probation, draft, on-shift, review*. Clawith's tabs get renamed: "Aware" → **Triggers**, "Mind" → **Soul & Memory**, "Relationships" → **Team Relationships**, "Activity Log" → **Timeline**, "Approvals" → **Approvals** (already neutral).
- **Layout:** apps/isola's existing agent page shell (breadcrumb, title, tab bar, sidebar). New tabs slot into the existing structure, not a Clawith-style layout imported wholesale.
- **Color / typography:** Inter font (already used by both) stays. Color palette is apps/isola's — not Clawith's dark navy. Dark mode optional; light default for SMB owners.
- **Endpoints:** Clawith's backend routes on `runtime.epic.dm` stay the same shape — the UI just calls them. No wrapper layer translating "Clawith schema" → "Isola schema"; apps/isola speaks the runtime's API directly.

What this means for the E.2–E.4 ports: each tab is a **rewrite**, sized at roughly 100–400 lines of Next.js + shadcn components, not a component copy. The Clawith source file is the **specification** (what data, what interactions, what state), not the implementation template.

---

### Where the EPIC team's admin needs go

Everything under ❌ "Dropped" above that EPIC might still need operationally (AdminCompanies, PlatformDashboard, LLM model management) lives on a **separate EPIC-only surface** — NOT in the tenant UI. For MVP, that surface is:

- CLI scripts in the `isola-runtime` repo (`backend/scripts/` — managed via `docker exec`)
- Direct DB access for one-offs
- Optionally: an internal `admin.epic.dm` URL later with the Clawith admin pages intact, locked to EPIC staff only (separate decision; not blocking MVP)

Tenant UI = tenant concerns only. Platform UI = EPIC-internal, separate URL.

---

## 4. The merged product — architecture

### Domains + services (after Phase E)

```
isola.epic.dm   (Vercel)          apps/isola — the single UI
       │
       │  calls …
       ▼
runtime.epic.dm (deepseek)         isola-runtime — single backend
  /api/auth/*        auth + tenants
  /api/agents/*      agents + templates + settings
  /api/channel/*     WhatsApp (and Slack/Discord/Teams for future)
  /api/gateway/*     OpenClaw edge protocol (restored)
  /ws/chat/*         streaming chat
  /api/triggers/*    pulse engine
  /api/approvals/*   L3 queue
  …
```

No separate admin URL. No `staging.isola.epic.dm`. Just the two domains above.

### Agent page structure (Audience C)

```
/dashboard/agent/[agentId]/

┌─ Default tabs (always visible to the owner):
│   ├─ Overview   (status card + last 10 activity lines + quick actions)
│   ├─ Inbox      (per-agent WhatsApp threads)
│   ├─ Drafts     (probation queue, if agent is on probation)
│   └─ Settings   (keywords + owner phone + probation + autonomy + quotas)
│
├─ [●──○] Show Advanced  ← toggle, persisted in localStorage per user
│
└─ When Advanced is ON, reveal 9 more tabs:
    ├─ Activity Log     (full auto-refreshing event stream)
    ├─ Chat Stream      (WebSocket chat with tool-call viz)
    ├─ Soul & Memory    (markdown editors for soul.md + memory.md)
    ├─ Triggers         (cron / interval / poll editor)
    ├─ Tools            (per-agent tool enable + config)
    ├─ Skills           (SKILL.md file browser)
    ├─ Workspace        (file tree of agent's workspace dir)
    ├─ Relationships    (A2A graph — Team/Suite tier surfaces this fully)
    └─ Approvals        (L3 request queue)
```

The Caribbean restaurant owner defaults to 4 tabs, never finds the toggle, ships their agents, collects payments. The agency operator flips the toggle once and lives in 13 tabs. Same UI.

### Dashboard sidebar stays apps/isola

No change to what's in the sidebar today:

- Your Team (multi-agent overview) — renames to "Team" in team mode
- Inbox (cross-agent)
- Drafts
- Catalog / Bookings / Contacts / Hours / Knowledge
- Outbound / Insights
- Channels / Integrations / Billing / Settings
- Ema
- Marketplace — Plaza, available from MVP. Browse + "hire" additional agents from the 30 role×vertical templates

### What we build new on the apps/isola side

Each of the 9 new tabs is a **Next.js route** that reads/writes to isola-runtime. Most endpoints already exist — we're surfacing them.

| Tab | Runtime endpoint |
|---|---|
| Overview | `GET /api/agents/{id}` + `GET /api/activity-log/{id}?limit=10` |
| Activity Log | `GET /api/activity-log/{id}` (streaming SSE) |
| Chat Stream | `WS /ws/chat/{agent_id}` |
| Soul & Memory | `GET/PUT /api/agents/{id}/files?path=soul.md` |
| Triggers | `GET/POST/DELETE /api/triggers?agent_id={id}` |
| Tools | `GET/POST /api/agents/{id}/tools` |
| Skills | `GET/POST/DELETE /api/agents/{id}/files?path=skills/` |
| Workspace | `GET /api/agents/{id}/files` (list + download) |
| Relationships | `GET /api/relationships?agent_id={id}` |
| Approvals | `GET/POST /api/agents/{id}/approvals` |

Port the component shapes from Clawith into apps/isola's shadcn/ui + Tailwind conventions. **Not a lift-and-shift — a rewrite.** Each tab is maybe 100-400 lines of Next.js.

---

## 5. OpenClaw — the privacy tier

### What it is (refresher)

OpenClaw is Clawith's **edge-runtime protocol**. A tenant installs a small daemon on their own hardware (Mac / NAS / Raspberry Pi / on-prem VM). Flow:

1. Daemon polls `isola-runtime`'s `/api/gateway/poll` endpoint for pending customer messages
2. Daemon runs the LLM locally (Ollama, local Claude, etc.) using the tenant's keys
3. Daemon POSTs the result to `/api/gateway/report`
4. isola-runtime forwards the reply to Meta → customer receives on WhatsApp as normal

Customer data (message content, context, business-system reads) **never leaves the tenant's hardware**. Only the message envelope (from, to, timestamp) crosses our network.

### Why it matters for Isola

Three segments pay premium for this:

1. **Clinics (privacy tier).** *"Your patient data never leaves your clinic computer."* Compliance-adjacent argument even without US HIPAA. Caribbean regulators (Cayman, BVI, Trinidad) are catching up on AI data residency.
2. **Credit unions + banks + law firms.** Client confidentiality + regulator scrutiny. One law firm on this tier covers 20 restaurant tenants economically.
3. **Cost-conscious high-volume.** A 500-message/day restaurant might prefer Ollama on a $50 VPS over $300/mo of GPT tokens.

### How it surfaces in the merged UI

Tenant-level **Runtime Mode** setting in `/dashboard/settings`:

```
Runtime
  ⦿ Hosted (default)
    Isola runs your agents on our infrastructure. Fast, no setup.
    Per-message metered.
  ○ Edge — Bring Your Own Runtime
    Run agents on your own hardware. Customer messages never leave
    your premises. Requires a $49/mo box or better.
    [Download the Edge daemon]   [Your API key: oc-xxxx…]
```

Flipping to Edge generates an `api_key_hash`, shows the tenant the download + setup docs. The customer-facing WhatsApp flow is identical — only the LLM-compute hop is different. Sticker pricing: add **EC$200/mo** to the tenant's tier for Edge mode.

### Restoration plan — Phase E.1

Branch `phase-e-openclaw-restore`:

1. Alembic migration: re-add `agent_type`, `api_key_hash`, `openclaw_last_seen`, `container_id`, `container_port` columns (inverse of B.1's `drop_openclaw_columns`)
2. `git checkout clawith-pristine -- backend/app/api/gateway.py backend/app/models/gateway_message.py`
3. Re-register gateway_router in `main.py`
4. Re-add `import app.models.gateway_message` in `entrypoint.sh` create_all
5. Restore Agent model fields from `clawith-pristine`
6. Restore `schemas.py` AgentCreate.agent_type + AgentOut openclaw fields
7. Restore `/agents/{id}/start`, `/stop`, `/api-key`, `/gateway-messages` endpoints
8. Restore `websocket.py` openclaw routing branch (if `agent_type='openclaw'`, queue in gateway_messages instead of calling LLM)
9. Restore `agent_tools.py` `_send_message_to_agent` openclaw queue branch
10. Restore `agent_manager.py` container lifecycle methods (fire only when `agent_type='openclaw'`)
11. New test `_phase_e1_openclaw_gateway` in `test_phase_b.py`: create openclaw-type agent, POST signed WA webhook → message queues in gateway_messages, GET `/gateway/poll` with API key returns the message, POST `/gateway/report` with a reply → mock Meta observes customer reply
12. UI-side (later): Runtime Mode setting in `/dashboard/settings`

Net: ~1,100 LoC restored, one Alembic migration, one new integration test. Effort estimate: **1 day.**

---

## 6. Rollout

### Phase E — merge + OpenClaw

| Sub-phase | Scope | Repo | Estimate |
|---|---|---|---|
| E.1 | OpenClaw restore | isola-runtime | 1 day |
| E.2 | Port 4 high-priority tabs: Status, Activity Log, Chat Stream, Approvals | isola-mvp | 8-12 days |
| E.3 | Port 5 medium-priority tabs: Soul & Memory, Triggers, Tools, Skills, Workspace | isola-mvp | 10-15 days |
| E.4 | Port 2 remaining tabs: Relationships, Plaza (MVP-scope marketplace) | isola-mvp | 8-10 days |
| E.5 | Runtime Mode UI in settings | isola-mvp | 1-2 days |
| E.6 | Retire `staging.isola.epic.dm` | ops | ~30 min |

**Total Phase E ≈ 4–6 weeks of focused work** split across two repos.

### Phase F — real-tenant UAT

Pick a pilot tenant (likely a restaurant). Provision a real Meta WABA. Point Meta webhook at `runtime.epic.dm`. Run two weeks. Iterate on what breaks.

### Phase G — flip BFF v2 traffic

After F passes, decommission BFF v2's Meta webhook. DNS / nginx change. BFF v2 stays alive for its non-WA routes (billing, onboarding helpers) until Phase H.

### Phase H — retire BFF v2 entirely

When isola-runtime covers everything BFF v2 did.

---

## 7. Decisions finalized

All 6 decisions confirmed by Eric 2026-04-24. No open questions remaining.

| # | Question | Answer |
|:--:|---|---|
| 1 | Plaza in MVP or Team/Suite only? | **MVP** — surfaces the 30 role×vertical templates as a browse-and-hire catalog. Available in Starter tier from day one. |
| 2 | Advanced toggle — per-agent or global? | **Per-user global.** localStorage-persisted. Agency operators flip once; SMB owners never find it. |
| 3 | Runtime Mode — tenant-level or per-agent? | **Tenant-level for v1.** Simpler onboarding. Per-agent later if a tenant asks. |
| 4 | Retire `staging.isola.epic.dm` after E.2-E.4? | **Yes.** Kill when apps/isola has the 11 tabs. Until then, keep as dev reference. |
| 5 | Rebuild the Clawith frontend container against a temp admin rebrand *or* leave it as-is? | **Leave as-is.** Effort wasted if we kill it in E.6 anyway. Current English + register-first + no-Google-Translate state is fine for eval. |
| 6 | Rebrand "OpenClaw" → "Edge" in user-facing text? | **Yes.** Keep `/api/gateway/*` as the internal route name (less churn). Everything user-facing says "Edge." |

---

## 8. Net summary

- **One UI** at isola.epic.dm. apps/isola's buyer funnel + business-ops dashboard **stay**. Clawith's 11-tab depth **gets ported in** behind an Advanced toggle.
- **OpenClaw comes back** as Phase E.1, surfaces as a "Runtime Mode: Edge" tenant setting for privacy-tier customers (clinics / law / banks / cost-conscious high-volume).
- **`staging.isola.epic.dm` retires** once E.2-E.4 ship the ported tabs.
- **BFF v2 starts sunset** after Phase F UAT passes on isola-runtime + the merged UI.
- **Audience C is served by one UI**: SMB owner sees 4 tabs, agency operator toggles to 13, both work on the same app.

The merged product is apps/isola's buyer-facing clarity wrapped around Clawith's operator depth, with OpenClaw as a premium privacy tier. Nothing's thrown away. Everything's on one domain.
