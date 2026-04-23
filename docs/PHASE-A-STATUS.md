# isola-runtime — Phase A Status

**Branch:** `phase-a-strip`
**Upstream baseline:** `clawith-pristine` tag at `ed1c422` (fork point from [dataelement/clawith](https://github.com/dataelement/clawith))
**Period:** 2026-04-23 (single-day push)
**Decision that kicked this off:** OD-49 — fork Clawith as Isola's agent runtime, supersedes BFF v2 orchestrator.

## Commit chain

| SHA | Tag | Net LoC |
|---|---|---|
| `0e81648` | A.1a — extract shared channel plumbing to `channel_common.py` | +215 / −4 |
| `e2a87f4` | A.1b — delete Chinese/enterprise channels (Feishu/WeCom/DingTalk/Atlassian/AgentBay) | +14 / −6,449 |
| `a3cc708` | A.2 — delete OpenClaw edge gateway | +0 / −748 |
| `bf7d99f` | A.4 — rebrand clawith → isola-runtime | +86 / −45 |
| `df8abb3` | A.1b-follow — Alembic migration narrows `channel_type_enum` to (slack, discord, microsoft_teams) | +91 / −0 |
| `1f85d54` | A.2-follow — purge dead OpenClaw field + code threading | +25 / −369 |
| `a05dafa` | A.4-prep — strip 28 dead tool schemas from the LLM catalog + delete orphaned agentbay services | +1 / −1,826 |
| **total** | | **~+432 / −9,441** |

Every commit is boot-verified: `docker compose restart backend` → `GET /api/health` 200 → zero errors or tracebacks in the startup log.

## What the fork ships with today

### Kept (the load-bearing runtime primitives)
- **WebSocket tool-calling loop** (`websocket.py`) — up to 50 iterations per turn with an 80%-threshold SystemMessage warning so the model saves progress before it runs out.
- **Agent Pulse Engine** (`trigger_daemon.py` + `AgentTrigger` model) — cron / interval / poll / on_message triggers that inject `SystemMessage` as if a human typed it.
- **A2A collaboration** with relationship-enforcement — `AgentAgentRelationship` blocks prompt-injection-driven inter-agent spam. Critical for multi-agent Isola.
- **Participant model** — solves the "which bubble side" problem when two agents chat ("I always render on the right, regardless of DB role").
- **Autonomy policy** (L1 / L2 / L3) with approval workflows.
- **Plaza** — agent marketplace scaffolding (reversed the initial strip plan; Isola will ship a `/marketplace` surface).
- **OrgDepartment / OrgMember / AgentRelationship hierarchy** (kept for Team / Suite tier expansion).
- **Template seeder, Skill seeder, MCP client.**
- Channels: **Slack, Discord, Microsoft Teams** (all routing through the new shared `channel_common.py`).

### Stripped (what the fork no longer has)
- **Feishu / WeCom / DingTalk / Atlassian Rovo / AgentBay channels** — ~5,700 LoC across 9 files.
- **OpenClaw edge gateway** (`/gateway/*` routes + `GatewayMessage` model + `agent_manager` container lifecycle) — ~1,100 LoC.
- **28 tool schemas** that backed the deleted channels (Feishu docs / calendar / drive / approval / bitable + AgentBay browser / code exec / file transfer + `send_channel_message` multiplexer).
- **Agent model fields:** `agent_type`, `api_key_hash`, `openclaw_last_seen`, `has_api_key` property.
- **Dead route endpoints:** `/agents/{id}/start`, `/agents/{id}/stop`, `/agents/{id}/api-key`, `/agents/{id}/gateway-messages`, `/enterprise/org/wecom-verify/{provider_id}`, `/enterprise/org/wecom-callback/{token}`.
- **Dead service files:** `agentbay_client.py` (940 LoC), `agentbay_live.py` (104 LoC).
- **i18n debt:** Chinese user-facing strings in `channel_common.py` translated to English; `soul.md` template rewritten in English for a Caribbean SMB front-desk persona.

### Rebranded
- `pyproject.toml` — `clawith-backend` → `isola-runtime-backend`
- `README.md` — Isola-focused header (upstream README kept below a divider for reference)
- `docker-compose.yml` — `POSTGRES_{USER,PASSWORD,DB}` now `isolaruntime`; network `isola_runtime_network`
- `.env.example` — dropped Feishu OAuth block
- `agent_template/soul.md` — warm Caribbean SMB front-desk voice (not Chinese enterprise-assistant)

### Known technical debt (punt to later phases)
1. **~1,500 LOC of orphaned handler bodies in `agent_tools.py`** for the 28 stripped tools. Unreachable (schemas are gone from `AGENT_TOOLS`), but the function defs still exist and still lazy-import deleted modules. Cosmetic only.
2. **DB columns still exist** for removed Agent fields: `agent_type`, `api_key_hash`, `openclaw_last_seen`, `container_id`, `container_port`. SQLAlchemy ignores unmapped columns on SELECT, and `agent_type`'s `NOT NULL DEFAULT 'native'` keeps INSERTs working. Cleanup: add an Alembic `DROP COLUMN` migration.
3. **Pre-existing upstream bug fixed in passing:** stray `)` at line 115 of `supervision_reminder.py` (file was unimported so the syntax error never surfaced). Could be upstreamed to `dataelement/clawith`.
4. **Frontend still fully Clawith-branded.** Phase D swaps the UI layer for `apps/isola` (Next.js at `isola.epic.dm`); the Vite/React frontend in this fork is reference-only.
5. **`agent_manager.py` still has `container_id` / `container_port` dead columns assigned in `initialize_agent_files`** indirectly (Agent model still has the columns). Tied to #2.

## Phase B scope — WhatsApp channel (4–6 weeks)

Copy `slack.py`'s shape as a reference adapter. Add WhatsApp to:
- `backend/app/api/whatsapp.py` (new, ~1,200 LOC estimate) — Meta Cloud API webhook handler, event dispatch, card rendering
- `backend/app/services/whatsapp_service.py` (new, ~800 LOC) — Meta Graph API client, template sends, media upload
- `ChannelConfig.channel_type_enum` — add `whatsapp` value (Alembic migration)
- `im_provider_enum` already has `whatsapp` slot (added in A.4 for forward-compat)
- `main.py` — register `whatsapp_router`
- `channel_common._call_agent_llm` — no change required (already channel-neutral)

Reference: Clawith's `feishu.py` was the biggest adapter and is already sketched in `channel_common.py`'s commit history (it was the _ source_ of the extracted `_call_agent_llm`).

## Phase C — vertical AgentTemplates

After Phase B lands WhatsApp, `template_seeder.py` gets our Isola roles:
- Rex / Mara / Joey / Cash / Brief / Tech × restaurant / hotel / clinic / retail / service = 30 default AgentTemplates.
- Quiz-driven provisioning: a new tenant picks a vertical + roles, gets pre-configured agents with role-appropriate soul + skills + autonomy policy.

## Phase D — UI swap

`apps/isola` (Next.js, deployed to `isola.epic.dm`) becomes the operator-facing UI; the upstream Vite/React frontend is retired. Phase D is ongoing in the `epicdm/isola-mvp` repo, not this one.

## Phase E — connector MCPs

Reuses the Clawith-fork MCP client: Odoo-MCP, Chatwoot-MCP, LiveKit-MCP, Fiserv-MCP. Each is a separate repo/service that this runtime's agents can talk to over MCP.

## Verification at close of Phase A

- **Stack up:** `docker compose -p isolaruntime up -d postgres redis backend` → healthy.
- **`/api/health`** → `{"status":"ok","version":"1.8.3-beta.2"}` in ~12 ms.
- **Alembic head** at `narrow_phase_a_enums`.
- **Postgres enum:** `channel_type_enum = (slack, discord, microsoft_teams)`.
- **Seeders:** 2 agent templates, 4 templates in DB (Project Manager / Designer / Product Intern / Market Researcher — upstream defaults; Phase C replaces with Isola verticals), 9 skills.
- **Background tasks:** `trigger_daemon + discord_gw` (was 5 pre-strip: removed `feishu_ws`, `dingtalk_stream`, `wecom_stream`).
- **User registration + JWT + protected endpoints** verified via curl smoke test.

## Merge plan

`phase-a-strip` → `main` via PR. Single squash merge preferred (or merge commit — either preserves the linear narrative via the commit messages).
