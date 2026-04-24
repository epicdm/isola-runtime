# Phase E — Product Synthesis: merging Clawith's depth with Isola's clarity

**Decision output for Audience C** (everyone — SMB owner + operator + agency + EPIC team). Hybrid architecture. Progressive disclosure. OpenClaw as a premium privacy tier. Gate at ~30 paying tenants to decide whether to unify further.

## TL;DR

Isola's `apps/isola` is a tight customer-facing funnel (marketing → 6-step HR-metaphor onboarding → business dashboard) but has a weak agent-workspace that can't tell the operator what the agent is doing. Clawith's UI is an 11-tab operator cockpit (soul, memory, triggers, tools, skills, A2A, workspace, chat-stream, activity log, approvals, settings) with no buyer funnel and no business-ops surfaces.

**Architecture: Option C — Hybrid, two stacks under one brand.**

- `isola.epic.dm/` = apps/isola (Next.js on Vercel) serves marketing, auth, onboarding, top-level dashboard (catalog, bookings, contacts, inbox, billing).
- `isola.epic.dm/dashboard/agents/*` = Clawith UI served via reverse proxy, untouched — the 11-tab agent workspace + Plaza marketplace come through as-is.
- Design tokens (colors, typography, nav chrome) aligned so the seam is invisible to users.
- `admin.epic.dm` = EPIC staff ops console (Clawith's admin pages rehomed).
- `staging.isola.epic.dm` = Clawith pristine frontend, permanent reference (not retiring).
- `runtime.epic.dm` = single backend serving both UIs.

**Gate at ~30 paying tenants.** At that milestone, decide whether to unify into one stack (rewrite Clawith tabs natively into apps/isola) or keep the hybrid long-term. Data drives the call: if the seam bothers users, unify; if nobody notices, keep shipping.

**OpenClaw returns** as "Edge" runtime mode — premium privacy-tier differentiator for clinics / law / banks / cost-conscious high-volume tenants.

**Clawith's capabilities are Isola's offer.** Don't hold back — expose them all:
- **Marketplace** (Plaza) for browsing + hiring agents from 30 role×vertical templates
- **Edge** (BYO OpenClaw) for tenants running LLM on their own hardware
- **Approvals queue** for L3-autonomy high-stakes actions
- **Per-agent Tools** catalog — MCP + built-in, per-agent configurable
- **Per-agent Skills** — file-level SKILL.md management
- **Multi-channel adapters** — WhatsApp (priority), Slack, Discord, Teams, per-agent enable + config
- **Soul & Memory editors** — markdown editors for agent personality + memory
- **Triggers** — cron / interval / poll / on-message pulse engine
- **Streaming chat** with tool-call viz for agent training

**Token-credit economy replaces message-count pricing.** Tenants pick their model (Haiku / Sonnet / Opus / Gemini / GPT-4o) and burn credits at model-specific rates. BYO LLM keys available at Business tier.

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

### Weaknesses to fix (via Clawith integration)
- **Agent workspace is shallow.** 5 tabs (ported from a Lovable prototype). Owner can edit settings but can't see what the agent is doing or thinking.
- **No chat transparency.** No streaming, no tool-call viz. Owner doesn't know what the LLM did between receiving and replying.
- **No soul / memory editor.** Agent personality is baked at onboarding; can't be directly edited without SQL.
- **No trigger editor.** Can't add "send review request 2h after reservation" or "morning briefing at 9am" without engineer help.
- **No activity log.** "Did the agent actually send that reply?" requires opening Chatwoot or BFF logs.
- **No approvals queue.** L3 actions from the autonomy policy exist in the backend but are invisible to operators.

→ All of these are solved by routing `/dashboard/agents/*` to Clawith's workspace under Option C. No rewrite required.

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

### Strengths that come through via reverse proxy (Option C)
- The 11-tab workspace is **6–12 months of work** to recreate from scratch — and under Option C we don't.
- Streaming chat + tool-call viz is product-grade — arrives intact.
- Approvals queue operationalizes L3 autonomy (without it, L3 is a setting nobody sees).
- Activity log is the "is the agent actually working" answer in one view.
- Trigger editor (Aware tab) is what turns passive agents into proactive ones.
- Plaza marketplace is credible Team/Suite-tier growth material.
- Zustand + react-query state management is clean.

### Weaknesses — handled in Option C by hide/gate/rename, not rewrite
- Technical terminology (*autonomy_policy, context_window_size*) — rename via i18n en.json overrides.
- Chinese strings in ~7% of UI — override en.json.
- Invitation-gated signup — signup funnel lives in apps/isola Next.js, Clawith's `/login` is unreachable for tenants.
- Enterprise Settings LLM management — gate behind staff role OR tier (see §3a + §9).
- Plaza premature — kept at MVP per decision #1.
- Dead code (`AgentBayLivePanel.tsx`) — route hidden, file stays in repo.

---

## 3. Feature matrix

### Where they overlap — pick the better one

| Feature | apps/isola | Clawith | Merged product wins |
|---|---|---|---|
| Register / login | Better-Auth, clean | Enterprise email flow | **apps/isola** (Clawith's auth routes hidden) |
| Agent create | Quiz, vertical-aware | 5-step wizard, generic | **Clawith wizard reachable at `/dashboard/agents/new`** — faster than rewriting Isola's weak version. Provision-vertical endpoint powers both. |
| Chat UI | Basic | Streaming + tool-call viz | **Clawith** (proxied intact) |
| Agent settings | Keywords / owner / probation | Autonomy / tokens / expiry | **Clawith's page** — Isola fields migrated into it via Settings tab extension |
| Agent list | Minimal | Rich card layout | **Clawith** (proxied) |

### Where only Isola has it (preserve in apps/isola)

Marketing site + vertical pages · 6-step HR onboarding · Drafts-on-probation · Catalog · Bookings · Contacts · Hours · Knowledge · Outbound campaigns · Insights · Ema (EPIC assistant) · QA-bot admin · Channels config · Billing

### Where only Clawith has it (comes through via proxy)

| Capability | Status in Option C |
|---|---|
| Agent Status card (tokens, activity, model) | ✅ proxied |
| Streaming chat + tool-call viz | ✅ proxied |
| Activity Log | ✅ proxied |
| Approvals queue | ✅ proxied |
| Soul / Memory editor | ✅ proxied |
| Triggers editor | ✅ proxied |
| Workspace file browser | ✅ proxied |
| Per-agent Skills management | ✅ proxied |
| Per-agent Tools management | ✅ proxied |
| A2A Relationships graph | ✅ proxied |
| Plaza marketplace | ✅ proxied at `/dashboard/marketplace` |

**Net build effort saved vs current synthesis: ~26-37 days.** Previous plan was rewriting each tab as shadcn/Next.js (100-400 LoC × 11 tabs + Plaza). Option C delivers the same capabilities in ~1 week of token-alignment + proxy + gating work.

---

## 3a. Explicit audit: every Clawith page, ruled in or out

Eric's directive: *"expose all from Clawith OR indicate which will not be in the Isola UI."* This is the complete list. Nothing is quietly dropped.

### ✅ Reachable in Isola UI (proxied under isola.epic.dm/dashboard/*)

| Clawith surface | Lands at | Notes |
|---|---|---|
| Agent list (dashboard main) | `/dashboard/agents` | Proxied, nav chrome replaced with apps/isola's |
| AgentDetail **Status** tab | `/dashboard/agents/:id` (default tab) | Proxied intact |
| AgentDetail **Aware** (Triggers) tab | `/dashboard/agents/:id/aware` | Proxied, i18n rename to "Triggers" |
| AgentDetail **Mind** (Soul + Memory) tab | `/dashboard/agents/:id/mind` | Proxied, i18n rename to "Soul & Memory" |
| AgentDetail **Tools** tab | `/dashboard/agents/:id/tools` | Proxied |
| AgentDetail **Skills** tab | `/dashboard/agents/:id/skills` | Proxied |
| AgentDetail **Relationships** tab | `/dashboard/agents/:id/relationships` | Proxied, i18n rename to "Team Relationships" |
| AgentDetail **Workspace** tab | `/dashboard/agents/:id/workspace` | Proxied |
| AgentDetail **Chat** tab | `/dashboard/agents/:id/chat` | Proxied, i18n rename to **"Train"** (see §11) |
| AgentDetail **Activity Log** tab | `/dashboard/agents/:id/activity` | Proxied, i18n rename to "Timeline" |
| AgentDetail **Approvals** tab | `/dashboard/agents/:id/approvals` | Proxied |
| AgentDetail **Settings** tab | `/dashboard/agents/:id/settings` | Proxied; Isola fields lifted into same page |
| **Plaza / Marketplace** | `/dashboard/marketplace` | **Flagship feature.** Browse + hire from the 30 role×vertical templates. Surfaced in the sidebar from day one — not a "future" card. |
| Agent create wizard | `/dashboard/agents/new` | Clawith wizard + provision-vertical endpoint |
| **Channel adapters** (per-agent enable + config) | Part of AgentDetail → Channels subsection | WhatsApp, Slack, Discord, Teams — per-agent toggle + credential config. WhatsApp lit up in MVP; others reachable when tenant asks. |
| Messages (cross-agent) | — | apps/isola's `/dashboard/inbox` wins, Clawith route unreachable |
| LLM provider management | `/dashboard/settings/models` | See §9 — tier-gated + BYO logic layered on top. This is NOT dropped — it's refined. |

### 🔁 Replaced by Isola-native equivalents (same capability, better-branded)

| Clawith surface | Isola equivalent |
|---|---|
| Login / Register | `/auth/[pathname]` (Better-Auth) — Clawith login routes hidden |
| ForgotPassword | Better-Auth flow |
| ResetPassword | Better-Auth flow |
| VerifyEmail | Better-Auth flow |
| CompanySetup | Replaced by apps/isola's 6-step onboarding |
| OpenClawSettings page | **Runtime Mode** section in `/dashboard/settings` (Hosted / Edge toggle — see §5) |

### 🏢 Rehomed to EPIC admin console (admin.epic.dm, staff-only)

These are kept intact but moved out of the tenant UI to a separate staff-gated surface. See §10.

| Clawith surface | Why rehomed |
|---|---|
| **Admin Companies** (cross-tenant admin) | EPIC operational need — seed for the Tenants list at admin.epic.dm. Must not appear in tenant UI. |
| **Platform Dashboard** (cross-tenant metrics) | Feeds admin.epic.dm's Cost dashboard. |
| **Enterprise Settings — model routing** (per-tier allow-list, default models, fallback chains) | EPIC operational lever. Tenants see only their choices; EPIC sees the routing policy. |

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
| **Enterprise Settings** (tenant fields only) | Company timezone · logo upload · retention period for chat history · operator language preference | `/dashboard/settings` — new "Company" section alongside existing Business / Channels / Billing subsections |
| **Token / autonomy defaults** | Per-agent token cap default · autonomy policy default · heartbeat schedule default | Rolled into `/dashboard/settings` Company subsection — these are tenant-level defaults that cascade to new agents |

### ❌ Dropped — will NOT surface in Isola UI ever

| Clawith surface | Why |
|---|---|
| **AgentBay Live Panel** (browser sandbox) | Deleted in Phase A.1b. Alibaba Cloud paid service; no Caribbean SMB use case. If we ever need browser automation, it goes via Playwright or an MCP server — not AgentBay. |
| **Chinese default locale** | English default everywhere. `zh.json` stays in repo for tenant toggle, but not the default; not marketed as multi-lingual in MVP. |
| **Invitation-only signup gate** | Isola MVP is open self-serve. Anyone with a payment method registers. Clawith's `/login` and `/setup-company` are unreachable from the tenant funnel. |

### Where the EPIC team's admin needs go

Everything under 🏢 Rehomed above goes to **admin.epic.dm** — see §10 for full scope. No platform/ops admin surfaces appear inside a tenant UI, ever.

---

## 3b. Integration principle — make the seam invisible

**The rule: no tenant should be able to tell which features came from Clawith versus Isola.**

Under Option C (hybrid proxy), this is a **design-token alignment problem**, not a rewrite problem. What changes in the Clawith UI:

- **Color palette:** override Clawith's CSS variables to match apps/isola's Tailwind theme (primary, secondary, accent, surface). Light default; dark optional.
- **Typography:** Inter everywhere (both stacks already use it — no change).
- **Nav chrome:** apps/isola's topbar + sidebar render as the outer shell. Clawith's UI drops its own top nav when served under isola.epic.dm; we intercept at the proxy layer or use a Clawith build flag (`VITE_EMBEDDED=true`) to hide its nav.
- **Voice (via en.json overrides):** HR metaphor terms. "Aware" → **Triggers**. "Mind" → **Soul & Memory**. "Relationships" → **Team Relationships**. "Activity Log" → **Timeline**. "Chat" (agent workspace) → **Train**. "Plaza" → **Marketplace**.
- **Icons:** Clawith uses `@tabler/icons-react`, apps/isola uses `lucide-react`. We don't swap Tabler out wholesale (too invasive) — we curate the 10-15 most-visible icons (sidebar, tab bar, header actions) and swap to lucide via a small icon shim. Deeper surfaces keep Tabler.
- **Toasts:** Clawith's toasts align to Sonner styling via CSS.
- **Endpoints:** both UIs call `runtime.epic.dm` directly. No translation layer.

**What doesn't change:** Clawith's tab layout, its streaming chat pane, its component architecture. We align the chrome; the depth stays as-is.

**Effort:** ~3-5 days of token + CSS + i18n work, vs 4-6 weeks to rewrite 11 tabs natively.

**The "unify or don't" decision at 30 tenants** reopens this: if users genuinely can't tell, keep hybrid. If the seam bleeds through (different font weights, different spacing rhythms, different toast styles), unify by rewriting tabs natively into apps/isola.

---

## 4. The merged product — architecture

### Domains + services (after Phase E)

```
isola.epic.dm (Vercel — Next.js)
  /                      → marketing
  /for/{clinics,...}     → vertical pages
  /auth/*                → Better-Auth
  /onboarding            → 6-step HR quiz
  /pricing · /privacy · /terms
  /dashboard             → business-ops landing (owner metrics, recent activity)
  /dashboard/catalog · /bookings · /contacts · /hours · /knowledge · /outbound · /insights
  /dashboard/inbox       → Chatwoot iframe (customer conversations)
  /dashboard/channels · /integrations · /billing · /settings
  /dashboard/ema · /ema/reports · /ema/settings
  /dashboard/agents/*    → REVERSE PROXY to Clawith frontend container
                           (agent list, 11-tab workspace, Plaza marketplace)

runtime.epic.dm (deepseek — FastAPI)
  /api/auth/*            tenants + users (shared session with Better-Auth)
  /api/agents/*          agents + templates + settings + provision-vertical
  /api/channel/*         WhatsApp, future Slack/Discord/Teams
  /api/gateway/*         OpenClaw edge protocol (restored in E.1)
  /ws/chat/*             streaming chat
  /api/triggers/*        pulse engine
  /api/approvals/*       L3 queue
  /api/activity-log/*    event stream
  /api/billing/*         credit balance + top-up
  /api/models/*          per-tenant model choice + BYO keys
  …

admin.epic.dm (Clawith admin, rehomed)
  Staff-only, 2FA.
  Tenants list · Tenant drill-down · Impersonate · Credit grants
  Model routing · Provider health · Cost dashboard
  Feature flags · Audit log · Meta number pool

staging.isola.epic.dm (permanent — NOT retiring)
  Clawith pristine frontend for side-by-side reference as we iterate.
  Cheap to keep; valuable for eval.

inbox.epic.dm (Chatwoot)
  Customer inbox — embedded at isola.epic.dm/dashboard/inbox via iframe.
```

### Nginx reverse-proxy sketch (isola.epic.dm)

```nginx
# Most routes → Next.js (Vercel)
location / {
    proxy_pass https://isola-mvp-prod.vercel.app;
    # normal Next.js proxy headers
}

# Agent workspace paths → Clawith frontend on deepseek
location /dashboard/agents {
    proxy_pass http://66.118.37.12:3308;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}

location /dashboard/marketplace {
    proxy_pass http://66.118.37.12:3308/plaza;
    # same headers
}

# Auth cookie is set for .isola.epic.dm — both sides see the session
```

Clawith's frontend container is configured at build time with `VITE_BASE_PATH=/dashboard/agents` and `VITE_EMBEDDED=true` so its router and asset URLs work under the sub-path and its top chrome is hidden.

### Agent page structure (what the tenant sees)

Owner clicks "Agents" in the apps/isola sidebar. URL becomes `isola.epic.dm/dashboard/agents`. Under the hood it's Clawith rendering, but inside apps/isola's outer shell:

```
┌─ apps/isola topbar + sidebar (always) ────────────────────┐
│                                                           │
│  Agents                                 [+ Hire new agent] │  ← Clawith's header, restyled
│  ┌────────────────────────────────────────────────────┐  │
│  │  Rex (Front Desk) — Active — 12 convos today       │  │
│  │  Cash (Billing)    — Probation — 3 drafts pending  │  │
│  │  …                                                  │  │
│  └────────────────────────────────────────────────────┘  │
│                                                           │
└───────────────────────────────────────────────────────────┘
```

Click a row → `/dashboard/agents/:id` → 11-tab Clawith workspace, same outer shell.

### Auth handoff

Both stacks share the same backend. Better-Auth writes a session cookie for `.isola.epic.dm`; Clawith reads it on first load via `/api/auth/me`. No separate login screen, no token translation.

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
3. **Cost-conscious high-volume.** A 500-message/day restaurant might prefer Ollama on a $50 VPS over burning credits on OpenAI.

### How it surfaces in the merged UI

Tenant-level **Runtime Mode** setting in `/dashboard/settings`:

```
Runtime
  ⦿ Hosted (default)
    Isola runs your agents on our infrastructure. Fast, no setup.
    Credit-metered.
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

### Phase E — proxy + OpenClaw + token economy + admin

| Sub-phase | Scope | Repo / surface | Estimate |
|---|---|---|---|
| **E.1** | OpenClaw restore (backend) | isola-runtime | 1 day |
| **E.2** | Nginx reverse-proxy setup: isola.epic.dm/dashboard/agents/* → Clawith frontend. Clawith rebuilt with VITE_BASE_PATH + VITE_EMBEDDED | ops + isola-runtime | 2-3 days |
| **E.3** | Design token alignment: CSS variable overrides, hide Clawith nav in embedded mode, curated icon swap (10-15 icons), Sonner toast style | isola-runtime frontend | 3-5 days |
| **E.4** | i18n rename pass: Aware→Triggers, Mind→Soul & Memory, Chat→Train, Plaza→Marketplace, Activity Log→Timeline, Relationships→Team Relationships. Verify no Chinese leakage | isola-runtime | 1 day |
| **E.5** | Hide/gate dropped surfaces: AgentBay route 404, Admin Companies 403 for non-staff, invitation-only signup bypassed (Next.js owns signup) | isola-runtime | 1 day |
| **E.6** | Runtime Mode UI (Hosted / Edge toggle) in `/dashboard/settings` | isola-mvp | 1-2 days |
| **E.7** | Token-credit economy backend: credit ledger, model consumption tracking, top-up endpoint (§9) | isola-runtime | 4-6 days |
| **E.8** | Token-credit economy UI: credits meter in topbar, billing page top-up button, model picker with multiplier | isola-mvp + runtime frontend | 3-4 days |
| **E.9** | BYO LLM keys: encrypted key storage per-tenant, provider fallback chain, Business-tier gating | isola-runtime | 3-4 days |
| **E.10** | EPIC admin console at admin.epic.dm (MVP scope from §10: tenants list, drill-down, credit grants, impersonation) | isola-runtime + ops | 5-7 days |

**Total Phase E ≈ 4-6 weeks** (significantly less than the 4-6 weeks of native-rewrite work in the previous plan, while also delivering the token economy and admin console that the previous plan deferred).

### Phase F — real-tenant UAT

Pick a pilot tenant (likely a restaurant). Provision a real Meta WABA. Point Meta webhook at `runtime.epic.dm`. Run two weeks. Iterate on what breaks.

### Phase G — flip BFF v2 traffic

After F passes, decommission BFF v2's Meta webhook. DNS / nginx change. BFF v2 stays alive for its non-WA routes (billing, onboarding helpers) until Phase H.

### Phase H — retire BFF v2 entirely

When isola-runtime covers everything BFF v2 did.

### Phase I — 30-tenant gate check

At ~30 paying tenants, review UX telemetry + user feedback:

- **Signal: keep hybrid.** Seam is invisible, no tickets about "weird navigation", token alignment held up through tenant adds.
- **Signal: unify.** Tenants comment on style breaks, stack-specific bugs (Clawith-side issues vs Next.js-side issues), design-debt from maintaining two frontends is slowing feature work.

If unify: Phase I.1-I.N rewrites Clawith tabs natively into apps/isola (the original synthesis plan). Effort estimate at that time: 4-6 weeks, but with real user data to prioritize which tabs get which polish.

If keep hybrid: set a new gate (60 tenants? 100?).

---

## 7. Decisions finalized

All decisions confirmed by Eric 2026-04-24.

| # | Question | Answer |
|:--:|---|---|
| 1 | Plaza in MVP or Team/Suite only? | **MVP** — surfaces the 30 role×vertical templates as a browse-and-hire catalog. Available in Starter tier from day one. |
| 2 | Advanced toggle — per-agent or global? | **Deferred.** Option C serves all tabs intact via proxy; no progressive disclosure needed at MVP. Revisit at 30-tenant gate if tabs overwhelm SMB owners. |
| 3 | Runtime Mode — tenant-level or per-agent? | **Tenant-level for v1.** Simpler onboarding. Per-agent later if a tenant asks. |
| 4 | Retire `staging.isola.epic.dm`? | **NO.** Kept permanently as pristine Clawith reference for side-by-side eval. Cost is trivial. |
| 5 | Rebuild the Clawith frontend container against a temp admin rebrand? | **Yes, once** — for VITE_BASE_PATH + VITE_EMBEDDED flags in E.2. Pristine staging.isola.epic.dm keeps its own build. |
| 6 | Rebrand "OpenClaw" → "Edge" in user-facing text? | **Yes.** Keep `/api/gateway/*` as the internal route name (less churn). Everything user-facing says "Edge." |
| 7 | Architecture direction: apps/isola primary + port Clawith IN, or Clawith primary + port Isola ON, or hybrid? | **Option C — Hybrid.** Reverse proxy, align design tokens. Revisit at 30-tenant gate. |
| 8 | LLM model choice for tenants? | **Yes — per-tier curated dropdown + BYO at Business tier.** See §9. |
| 9 | Token-credit economy vs message-count pricing? | **Credit economy.** Model-specific multipliers, top-ups, tier-level allowances. See §9. |
| 10 | Where do EPIC cross-tenant ops surfaces live? | **admin.epic.dm**, staff-only. Rehomed from Clawith admin pages. See §10. |

---

## 8. Net summary

- **One brand at isola.epic.dm, two stacks under the hood.** apps/isola owns the buyer funnel, auth, onboarding, and business-ops surfaces (catalog / bookings / contacts / hours / inbox / billing). Clawith serves the agent workspace + Plaza under `/dashboard/agents/*` via reverse proxy.
- **Design tokens aligned** so the seam is invisible to tenants. HR-metaphor i18n renames applied.
- **OpenClaw returns** (Phase E.1) as "Edge" runtime mode — privacy tier for clinics / law / banks.
- **Token-credit economy** replaces message-count ceilings. Customers pick their model; higher models burn faster; top-ups + BYO keys available. EPIC's margin made visible in the admin cost dashboard.
- **admin.epic.dm** hosts EPIC staff ops — tenants list, impersonation, credit grants, model routing, cost dashboard — gated 2FA, staff-only. Seeded from Clawith's admin pages.
- **staging.isola.epic.dm stays** as permanent pristine reference.
- **BFF v2 starts sunset** after Phase F UAT passes on isola-runtime + the merged UI.
- **30-tenant gate** decides whether to unify stacks or keep the hybrid long-term.

---

## 9. Token-credit economy

Replaces the current "X messages per month" ceiling. Customer picks their model; cost expressed in a single universal credit unit. EPIC's inference economics stay hidden behind the credit layer.

### Credit rates (multiplier vs Haiku baseline)

| Model | Credit rate | Rough messages per 100k credits* | When to pick |
|---|:---:|:---:|---|
| Gemini 2.5 Flash | 0.5× | ~1,000 msgs | High volume, simple intents |
| **Haiku 4.5** (default) | **1×** | **~500 msgs** | Standard front-desk work |
| GPT-4o | 3× | ~165 msgs | Complex reasoning on a budget |
| Sonnet 4.6 | 4× | ~125 msgs | Complex conversations, light reasoning |
| Opus 4.7 | 20× | ~25 msgs | Deep reasoning, low volume, concierge service |

*assuming ~200 tokens in + 200 tokens out per WhatsApp turn

### Tier → credit bucket

| Tier | EC$/mo | Credits/mo | Haiku equiv | Sonnet equiv | Opus equiv |
|---|:---:|:---:|:---:|:---:|:---:|
| Starter | 149 | 100k | 500 msgs | 125 msgs | 25 msgs |
| Pro | 249 | 500k | 2,500 msgs | 625 msgs | 125 msgs |
| Business | 449 | 2M | 10,000 msgs | 2,500 msgs | 500 msgs |

**Top-up:** EC$50 per 100k credits, any tier, instant.

### The design insight

A low-volume small restaurant (20 customer questions/month) can run **Opus on Starter** and stay within plan — they get concierge-quality replies for EC$149/mo.

A busy clinic on **Haiku** gets 500 msgs on the same EC$149 — same plan, different model strategy.

Neither customer needs to upgrade until their volume grows past their model choice.

### Growth path when credits run low

1. **Buy credits** — EC$50 per 100k, instant, no commitment.
2. **Upgrade tier** — Starter → Pro (5× credits for 1.67× price); recommended at sustained overflow.
3. **Downgrade model** — same credits last longer; recommended when conversations are simple.
4. **Auto-downgrade toggle** — "when credits run out, fall back to Haiku instead of pausing agent." Default on for Starter/Pro (service never stops); opt-in for Business (might prefer pause + notify over degraded model).

### BYO LLM keys (Business tier only)

A separate track for tenants who want direct provider relationships:

- Tenant pastes own OpenAI / Anthropic / Google / DeepSeek keys in `/dashboard/settings/models`
- Inference billed directly by provider to the tenant
- Platform fee reduced (sticker: **EC$99/mo flat** for Business BYO, vs EC$449 for Business Hosted inference)
- **Business tier only** — keeps Starter/Pro simple, BYO needs knobs Starter customers shouldn't touch
- Hard monthly cap required (tenant sets EC$ equivalent), alert at 80%, hard-stop at 100% to prevent runaway loops
- Credits don't apply to BYO — their spend lives at the provider
- Per-agent model override still supported (agent A on GPT-4o, agent B on Claude), as long as both providers are wired

### Tenant-side UX

- **Topbar meter:** `34,500 / 100,000 credits — 12 days left at current rate`
- **Model picker** on Agent → Settings → Model: dropdown, each option shows multiplier in parentheses (`Sonnet (4×)`). On change, modal warns: "Rex will use credits ~4× faster — you have ~12 days at current rate, will drop to ~3 days."
- **Billing page** (`/dashboard/billing`): current balance, burn rate graph, projected run-out date, `[Buy 100k credits — EC$50]` button, usage history (date · agent · model · credits used · customer turns).
- **Auto-downgrade setting:** one toggle in `/dashboard/billing` plus per-agent override.

### EPIC-side (admin dashboard — see §10)

- Per-tenant token burn rate, model mix, credit balance.
- EPIC's actual provider spend vs credits consumed = margin per tenant.
- Alert: tenant burns 3× plan rate in 48h (possible loop, possible abuse, possible legit growth).
- Provider outage triggers fallback routing; credits unaffected.

### Backend schema sketch

```sql
credit_balance (tenant_id, balance_credits, updated_at)
credit_ledger  (id, tenant_id, delta, reason, ref_id, created_at)
  -- delta negative for consumption, positive for top-up / grant / refund
  -- reason: 'consumption', 'topup', 'plan_grant', 'staff_grant', 'refund'
model_usage    (id, tenant_id, agent_id, turn_id, model, input_tokens, output_tokens, credits_consumed, created_at)
byo_keys       (tenant_id, provider, encrypted_key, monthly_cap_ec, spend_this_month_ec, created_at, rotated_at)
  -- encrypted with tenant-scoped KMS envelope key
tenant_model_prefs (tenant_id, default_model, auto_downgrade_enabled)
agent_model_override (agent_id, model)  -- nullable; null means fall through to tenant default
```

---

## 10. EPIC Admin console (admin.epic.dm)

Separate surface, staff-only, **not inside tenant UI**. Seeded from Clawith's admin pages (Admin Companies, Enterprise Settings model routing, Platform Dashboard) — those get rehomed here, not rewritten.

### Auth + access
- Separate hostname: `admin.epic.dm`
- Gate: EPIC staff emails only + hardware 2FA required
- Every action audit-logged (who / when / what / tenant-id / before-after)
- Impersonation mode adds a banner on the tenant side ("EPIC staff is viewing") to satisfy trust expectations

### MVP scope — Phase E.10

| Section | What it shows / does |
|---|---|
| **Tenants list** | All tenants. Columns: plan, status, MRR, credit balance, credits burned this month, last active, signup date, Meta WA number status. Sortable, filterable. |
| **Tenant drill-down** | Usage per agent, per model. Credit ledger (consumption + top-ups). Provisioning state. Health. Stripe/Fiserv status. |
| **Impersonate** | One-click "log in as this tenant" — opens isola.epic.dm in a new tab with staff session → tenant session. Audit-logged. Banner visible on tenant side. |
| **Credit grants** | Manually add credits to a tenant (support refund, comp, makegood). Reason required (free text); ledger row tagged `staff_grant`. |
| **Model routing** | Per-tier model allow-list (what Starter / Pro / Business see in their picker). Default model per tier. Fallback chain (Anthropic down → route to Gemini). Saved as config, hot-reloaded. |
| **Provider health** | Real-time status of Anthropic / OpenAI / Google / DeepSeek. Error rate, p50/p95 latency, rate-limit headroom. Alerts when provider error rate > 5% over 5 min. |
| **Cost dashboard** | EPIC inference spend this month across all tenants. Gross margin per tenant (credits consumed × EC$ per credit) − (actual provider cost). Break-even tenant count. |
| **Meta number pool** | 1767818XXXX pool state. Flag stale WABAs. Quick-provision / quick-retire actions. |
| **Feature flags** | Roll out features to % of tenants, or to a named list. Hot-reloadable from UI. |
| **Audit log** | Every staff action, sortable by staff member / tenant / action type / date. Exportable for compliance. |

### Phase 2 adds (post-MVP, after Phase F UAT)

- Load balancer policy editor (cheapest provider for Starter, fastest for Business, sticky for conversation continuity)
- Auto-throttle on cost overrun (pause a tenant's agents if they exceed 10× plan rate in an hour)
- Per-tenant alerts (Slack/email) on provisioning state changes, credit exhaustion, Meta number drift
- Revenue + COGS graphs over time
- Churn signals dashboard
- Tenant health score (engagement, growth, support load)

### Build effort

**MVP ~5-7 days** (mostly reusing Clawith's admin pages — gate them behind `is_staff`, rehome them at admin.epic.dm, add the observability pieces Clawith doesn't have). Phase 2 adds land incrementally as volume justifies them.

### Hosting

- admin.epic.dm DNS → deepseek
- Same nginx reverse proxy, different vhost, path: `/` → Clawith frontend with admin routes mounted
- Auth middleware checks `is_staff` flag on user session; non-staff → 403
- Separate session cookie (don't share with tenant isola.epic.dm session — even though same backend, admin context should require re-auth)

---

## 11. Chat surfaces — Clawith chat vs Chatwoot (both stay, different jobs)

Two different chat surfaces for two different users. Not alternatives.

| Surface | Who's talking | Purpose | Where it lives |
|---|---|---|---|
| **Clawith chat** (AgentDetail → "Train" tab) | Tenant ↔ their own agent | Train, test, instruct, debug. Stream tokens, inspect tool calls, approve actions. | `isola.epic.dm/dashboard/agents/:id/chat` (proxied from Clawith) |
| **Chatwoot** (customer inbox) | Customer ↔ agent, with tenant observing | See live customer convos, intervene, assign to humans, tag, note, ticket close. Multi-channel inbox. | `inbox.epic.dm` — embedded at `isola.epic.dm/dashboard/inbox` via iframe |

### How they relate

```
Customer (WhatsApp) → Meta → BFF v1 webhook → runtime → Agent → reply → Meta → Customer
                                                 │
                                                 ▼
                                       mirrored into Chatwoot ticket
                                                 │
                                                 ▼
                                   tenant monitors + overrides in /dashboard/inbox

Tenant (Isola UI) → Clawith chat ("Train") → Agent (directly, not via WA)
                                              │
                                              ▼
                                    streaming tokens, tool calls, thinking
                                    — private workspace, no customer involved
```

### Labeling to avoid confusion

- Clawith's Chat tab **renamed "Train"** via en.json override. Subtitle: *"Private chat with Rex — use this to test and tune your agent. Customers can't see this."*
- `/dashboard/inbox` labeled **"Customer Inbox."** Subtitle: *"Live conversations between your agents and customers."*
- Cross-link: agent workspace header shows `[View customer convos in Inbox →]` bridging the two mental models.

### Feature comparison (why neither replaces the other)

**Clawith chat** has: streaming tokens, tool-call traces, thinking-trail inspection, approval prompts inline, memory write visibility, soul context injection. **Purpose-built for agent-building.**

**Chatwoot** has: multi-channel inbox (WA + email + web widget + IG), ticket lifecycle, assignment/routing, contact CRM, canned responses, team collaboration, SLA tracking, tags, notes. **Purpose-built for support inbox.**

If we only shipped Chatwoot, tenants couldn't train agents — they'd have to debug by sending their own WhatsApp messages (terrible UX, pollutes production logs). If we only shipped Clawith chat, tenants couldn't see what customers actually said in production — blind to the real work.

### Future unification (revisit at 30-tenant gate)

Eventually `/dashboard/inbox` could become a native apps/isola UI built on top of Chatwoot's API (replaces the iframe, uses Chatwoot purely as the backend). That's worth doing when:

- iframe breaks (cookie scoping, style mismatches) annoy tenants repeatedly
- we want inbox features that Chatwoot doesn't ship (e.g., agent-specific filtering baked into the UI chrome)
- branded cohesion with the rest of apps/isola becomes the limiting factor

Not now. Iframe works. Ships fast. Revisit at the same 30-tenant gate as the broader stack-unification decision.
