# Audit — Wave-1.5 Issue B: Clawith /api/internal/cross-store/tenants/{id}/agents mirror

**Date:** 2026-05-07
**Class:** BACKEND-ONLY (per `docs/runbooks/cc-pr-merge-policy.md` in apps/isola-mvp)
**Authority:** PM kickoff `cc-kickoff-wave-1-5-clawith-cross-store-mirror.md`
**Branch:** `fix/wave-1-5-clawith-cross-store-internal-mirror`
**Verification:** Apparatus rules per kickoff §"Apparatus rules in force"

---

## Why

BFF Team aggregator (`/opt/bff-v2/app/api/internal/team/list/route.ts`) calls Clawith service-to-service at `/api/internal/cross-store/tenants/{id}/agents`. That endpoint did not exist on Clawith — only the human-facing Bearer-JWT variant at `/api/admin/cross-store/tenants/{id}/agents`. URL family = auth family per memory #28: `/api/admin/*` is human-facing (Bearer JWT, `platform_admin`-gated); `/api/internal/*` is service-to-service (X-Internal-Secret).

This PR adds the missing `/api/internal/*` variant. Mirrors the admin handler, swaps the auth dependency.

---

## Step 0.5 — In-code annotation reads

### Admin variant (`backend/app/api/admin_crossstore.py:328`)

```python
@router.get("/tenants/{tenant_id}/agents")
async def list_agents(
    tenant_id: str,
    include_retired: bool = Query(False, alias="includeRetired"),
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not tenant_id or len(tenant_id) > 100:
        raise HTTPException(status_code=400, detail="tenant_id must be 1-100 chars")
    _, clawith_uuid = await _resolve_clawith_tenant_id(tenant_id)

    q = select(Agent).where(Agent.tenant_id == clawith_uuid)
    if not include_retired:
        q = q.where(Agent.retired_at.is_(None))
    q = q.order_by(Agent.created_at.desc())

    result = await db.execute(q)
    agents = result.scalars().all()
    return {"total": len(agents), "agents": [_agent_view(a) for a in agents]}
```

### Auth decorator (`backend/app/core/internal_auth.py:16`)

```python
async def require_internal_secret(
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
):
    expected = (settings.ISOLA_INTERNAL_SECRET or "").strip()
    if not x_internal_secret or x_internal_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Internal-Secret header")
```

### Internal router (`backend/app/api/internal.py:30-34`)

```python
router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_internal_secret)],
)
```

### `_agent_view` shape (admin_crossstore.py:103)

16 fields per agent: `id, name, agent_type, role_description, welcome_message, tone, status, owner_phone, paperclip_agent_id, paperclip_company_id, container_port, retired_at, retired_by, created_at, updated_at` (+ `_agent_view` returns dict with all of these).

### BFF caller shape

`/opt/bff-v2/app/api/internal/team/list/route.ts:107`:
```ts
const path = `/api/internal/cross-store/tenants/${bridge.ids.clawithTenantId}/agents`
```

BFF passes `clawithTenantId` directly — already resolved via `loadTenantBridgeIds()`. No need for Clawith to roundtrip back to BFF for tenant resolution.

---

## Step 1 — Implementation

### Watch points (per PM kickoff)

- ✅ `require_internal_secret` exists. No HALT.
- ✅ Admin handler has zero role-specific filtering. `current_user` is required by `require_role("platform_admin")` decorator but not used in query. Data shape is purely tenant-scoped. Internal variant returns same shape. No HALT.

### Mount strategy

Added the new endpoint to `backend/app/api/internal.py` (NOT a new file). Precedent: internal.py already houses cross-store-flavored endpoints (L1026 docstring: "X-Internal-Secret variant of S4 admin retire... different auth family per memory #28"). Same pattern fits.

Reused `_agent_view` from admin_crossstore.py via import → guaranteed identical response shape, no divergence risk.

Skipped `_resolve_clawith_tenant_id()` — BFF caller already resolved `bridge.ids.clawithTenantId`, so `tenant_id` arriving here is already a Clawith UUID. Avoids circular Clawith→BFF→Clawith roundtrip. Documented inline.

### Diff

```
backend/app/api/internal.py | 42 +++++++++++++++++++++++++++++++++++++++++-
1 file changed, 41 insertions(+), 1 deletion(-)
```

- Added `Query` to fastapi imports
- Appended `list_cross_store_agents_internal` handler at end of file
- Imports `_agent_view` (aliased `_xstore_agent_view`) from admin_crossstore

---

## Step 2 — Verification (MER pattern)

### Direct curl probe — positive auth

```bash
$ curl -s -H "X-Internal-Secret: $(grep ^ISOLA_INTERNAL_SECRET= /opt/isola-runtime/.env | cut -d= -f2)" \
  "http://localhost:8800/api/internal/cross-store/tenants/47768881-88a3-4f64-8e0f-5cdce0e7237b/agents" \
  | jq '{total, agent_count: (.agents | length), first_agent_keys: (.agents[0] | keys)}'
{
  "total": 2,
  "agent_count": 2,
  "first_agent_keys": [
    "agent_type", "container_port", "created_at", "id", "name",
    "owner_phone", "paperclip_agent_id", "paperclip_company_id",
    "retired_at", "retired_by", "role_description", "status",
    "tone", "updated_at", "welcome_message"
  ]
}
```

Result: 200, total=2 (EPIC has Rex + 1 other), 16-field shape matches `_agent_view` exactly.

### Direct curl probes — negative auth

```bash
$ curl -H "X-Internal-Secret: wrong-secret" -o /dev/null -w "%{http_code}" \
  "http://localhost:8800/api/internal/cross-store/tenants/47768881-.../agents"
401

$ curl -o /dev/null -w "%{http_code}" \
  "http://localhost:8800/api/internal/cross-store/tenants/47768881-.../agents"
401
```

Result: both negative cases return 401 ✅.

### BFF Team aggregator integration probe

```bash
$ curl -s -H "X-Internal-Secret: $(grep ^INTERNAL_SECRET= /opt/bff-v2/.env | cut -d= -f2)" \
  "http://localhost:3005/api/internal/team/list?tenantId=8166ea11-8db0-4f26-879a-e2067be0a018" | jq
{
  "ok": true,
  "error": null,
  "data_count": 2,
  "first_row": {
    "id": "8166ea11-8db0-4f26-879a-e2067be0a018",
    "name": "Rex",
    "character": null,
    "role": null,
    "status": "unknown",
    "autonomy_level": "L1",
    "last_activity_at": null,
    "paperclip_agent_id": "df78b893-0e1a-405f-931a-c2995dc9f4c6"
  },
  "sources": [
    { "status": "ok" },
    { "status": "skipped" }
  ]
}
```

Result: ok:true, data_count=2, Rex resolved with paperclip_agent_id. Pre-fix this returned `{ok:false, error:"clawith-fetch-failed"}`.

---

## Caveats (surfaced for follow-up; NOT blockers)

### Kickoff used stale tenantId

The kickoff probe specifies `tenantId=Y8e7kUFIMp03CSjbksxAbslDZ6X2gHPv`. That value is not present in `tenant_registry` as either `tenantId` OR `shellTenantId`. EPIC's actual active row:

| field | value |
|---|---|
| tenantId | `8166ea11-8db0-4f26-879a-e2067be0a018` |
| shellTenantId | `DH2CK1wTyGqFTdXWxuM9nPj2nD4gSTeG` |
| clawithTenantId | `47768881-88a3-4f64-8e0f-5cdce0e7237b` (matches kickoff Clawith UUID ✅) |
| status | `active` |

Verified empirically post-fix. Not a regression; kickoff data was stale. The fix code is correct. Filed as kickoff-data hygiene observation; the architecture works.

### Kickoff env-var name

Kickoff uses `^INTERNAL_SECRET=` for `/opt/isola-runtime/.env` lookup. Real var name on Clawith side is `ISOLA_INTERNAL_SECRET`. Adjusted in actual probes. Note for kickoff template hygiene; not a code defect.

### `.bak` file pollution

11 untracked `.bak` files in `/opt/isola-runtime` from prior rotation/L5-S1 work. Not bundled into this PR per PM ("file as separate Wave-1.5 hygiene followup; mirror the pattern done on /opt/bff-v2 — extend `.gitignore` for `.bak` patterns").

---

## Acceptance criteria status

| # | Criterion | Status |
|---|---|---|
| 1 | `/api/internal/cross-store/tenants/{tenant_id}/agents` endpoint exists | ✅ |
| 2 | X-Internal-Secret auth required (NOT Bearer JWT) | ✅ |
| 3 | Returns same response shape as admin variant | ✅ (reuses `_agent_view`) |
| 4 | Negative auth tests pass (401 on wrong / missing secret) | ✅ |
| 5 | BFF Team aggregator returns ok:true end-to-end | ✅ |
| 6 | `/dashboard/team` renders real team data on production | ⏳ Eric eyeball |
| 7 | Apparatus rules followed | ✅ |

---

## Apparatus rules applied

- ✅ Working-tree hygiene as Step 0
- ✅ Substrate-state IN-CODE-ANNOTATIONS — read admin handler + auth decorator + BFF caller before implementation
- ✅ URL family = auth family (memory #28)
- ✅ Single-pipeline-redact for INTERNAL_SECRET — value never reaches stdout (only inside curl `-H` arg via `$(grep ... | cut -d= -f2)`)
- ✅ MER pattern for verification — curl probes (positive + negative + integration) captured inline
- ✅ Probe-first; verify before declaring blocker (caught the stale-tenantId-in-kickoff via empirical state probe rather than declaring fix-broken)
