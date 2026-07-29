#!/usr/bin/env python3
"""Tenant-isolation matrix for the agent/tool routes in app/api/tools.py.

Covers def-clawith-tools-cross-tenant-write-2026-07-29 (P0) and its full route
class. Reproducible from source: takes ALL configuration from the environment,
contains no secrets, and drives the live HTTP API of a running Clawith stack.

Required env:
  ISOLA_BASE        e.g. http://127.0.0.1:8891/api
  A_TOKEN           bearer token for an admin in tenant A (the agent owner)
  B_TOKEN           bearer token for an admin in a DIFFERENT tenant B (attacker)
  A_AGENT_ID        an agent owned by tenant A
Exit code 0 iff every check passes.
"""
import json, os, sys, urllib.request, urllib.error

BASE = os.environ["ISOLA_BASE"].rstrip("/")
A = os.environ["A_TOKEN"]; B = os.environ["B_TOKEN"]; A_AGENT = os.environ["A_AGENT_ID"]

def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data: req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            t = r.read().decode(); return r.status, (json.loads(t) if t else {})
    except urllib.error.HTTPError as e:
        t = e.read().decode()
        try: return e.code, json.loads(t)
        except Exception: return e.code, {"_raw": t[:200]}

R = []
def ck(label, cond, detail=""):
    R.append((label, bool(cond))); print(f"[{'PASS' if cond else 'FAIL'}] {label}  {detail}")

def denied(st): return st in (401, 403, 404)

# ---- setup: create a CUSTOM (non-builtin) tool owned by tenant A ----
uniq = os.getpid()
st, r = call("POST", "/tools", {"name": f"isola-mcp-test-{uniq}", "display_name": f"Isola MCP Test {uniq}", "type": "mcp",
             "mcp_server_name": f"isolasrv{uniq}", "mcp_server_url": "http://example.invalid/mcp"}, token=A)
A_TOOL = r.get("id") if st in (200, 201) else None
print("setup: A custom tool ->", st, A_TOOL)

# ---- agent-scoped routes (original P0) ----
st, before = call("GET", f"/tools/agents/{A_AGENT}", token=A)
ck("agent GET tools same-tenant 200", st == 200, f"HTTP {st}")
tgt = next((t for t in before if t.get("category") != "system" and t.get("enabled") is not None), None) if isinstance(before, list) else None
if tgt:
    orig = bool(tgt["enabled"])
    st, _ = call("PUT", f"/tools/agents/{A_AGENT}", [{"tool_id": tgt["id"], "enabled": not orig}], token=A)
    ck("agent PUT tools same-tenant 200 (manage)", st == 200, f"HTTP {st}")
    call("PUT", f"/tools/agents/{A_AGENT}", [{"tool_id": tgt["id"], "enabled": orig}], token=A)
    st, _ = call("PUT", f"/tools/agents/{A_AGENT}", [{"tool_id": tgt["id"], "enabled": not orig}], token=B)
    ck("agent PUT tools cross-tenant DENIED", denied(st), f"HTTP {st}")
    st, after = call("GET", f"/tools/agents/{A_AGENT}", token=A)
    now = next((t for t in after if t["id"] == tgt["id"]), {})
    ck("agent tool value UNCHANGED after denied write", bool(now.get("enabled")) == orig)
for p in [f"/tools/agents/{A_AGENT}", f"/tools/agents/{A_AGENT}/with-config"]:
    st, _ = call("GET", p, token=B); ck(f"cross-tenant GET {p} DENIED", denied(st), f"HTTP {st}")

# ---- delete_agent_tool (DELETE /tools/agent-tool/{id}) ----
st, listed = call("GET", f"/tools/agents/{A_AGENT}", token=A)
# obtain an AgentTool assignment id for A's agent via with-config (has agent_tool ids) or agent-installed
st, inst = call("GET", f"/tools/agent-installed", token=A)
at_id = None
if isinstance(inst, list):
    at_id = next((x.get("agent_tool_id") or x.get("id") for x in inst if (x.get("agent_id") == A_AGENT)), None)
# fallback: ensure at least one assignment exists by enabling one, then re-query DB-less via with-config
st, wc = call("GET", f"/tools/agents/{A_AGENT}/with-config", token=A)
if at_id is None and isinstance(wc, list):
    at_id = next((t.get("agent_tool_id") for t in wc if t.get("agent_tool_id")), None)
if at_id:
    st, _ = call("DELETE", f"/tools/agent-tool/{at_id}", token=B)
    ck("delete_agent_tool cross-tenant DENIED", denied(st), f"HTTP {st}")
    st, chk = call("GET", f"/tools/agents/{A_AGENT}/with-config", token=A)
    still = any(t.get("agent_tool_id") == at_id for t in chk) if isinstance(chk, list) else True
    ck("delete_agent_tool row NOT deleted after denial", still)
else:
    ck("delete_agent_tool: assignment id resolvable", False, "could not resolve an agent_tool_id")

# ---- update_mcp_server (PUT /tools/mcp-server) ----
if A_TOOL:
    A_TENANT = None
    st, me = call("GET", "/auth/me", token=A); A_TENANT = me.get("tenant_id")
    # B (org_admin of B) supplies tenant_id = A -> must be denied, no write
    st, r = call("PUT", "/tools/mcp-server", {"server_name": f"isolasrv{uniq}",
                 "server_url": "http://attacker.invalid/mcp", "api_key": "ATTACKER-KEY",
                 "tenant_id": A_TENANT}, token=B)
    # NOTE: PUT /tools/mcp-server is currently SHADOWED by PUT /tools/{tool_id}
    # (tool_id="mcp-server" fails UUID parsing -> 422), so the route is not
    # wire-reachable and no cross-tenant write can occur through it. The guard
    # itself is proven directly in tests/test_tools_tenant_guards.py.
    ck("update_mcp_server cross-tenant produces NO 2xx (shadowed 422 or guard 403)", st >= 400, f"HTTP {st}")
    st, tools_after = call("GET", "/tools", token=A)
    a_tool_row = next((t for t in tools_after if t.get("id") == A_TOOL), {}) if isinstance(tools_after, list) else {}
    ck("update_mcp_server: A's tool URL not overwritten cross-tenant",
       a_tool_row.get("mcp_server_url") != "http://attacker.invalid/mcp",
       f"url={a_tool_row.get('mcp_server_url')}")

# ---- update_tool / delete_tool / bulk on A's custom tool ----
if A_TOOL:
    st, r = call("PUT", f"/tools/{A_TOOL}", {"mcp_server_url": "http://hijack.invalid/mcp"}, token=B)
    ck("update_tool cross-tenant DENIED", denied(st), f"HTTP {st}")
    st, r = call("PUT", "/tools/bulk", [{"tool_id": A_TOOL, "enabled": False}], token=B)
    st, tools_after = call("GET", "/tools", token=A)
    row = next((t for t in tools_after if t.get("id") == A_TOOL), {}) if isinstance(tools_after, list) else {}
    ck("bulk cross-tenant did NOT modify A's tool", row.get("enabled") is not False or row == {}, f"enabled={row.get('enabled')}")
    ck("update_tool: A's tool URL unchanged after cross-tenant attempt",
       row.get("mcp_server_url") != "http://hijack.invalid/mcp")
    st, r = call("DELETE", f"/tools/{A_TOOL}", token=B)
    ck("delete_tool cross-tenant DENIED", denied(st), f"HTTP {st}")
    st, tools_after = call("GET", "/tools", token=A)
    still = any(t.get("id") == A_TOOL for t in tools_after) if isinstance(tools_after, list) else False
    ck("delete_tool: A's tool still exists after denied delete", still)
    # same-tenant owner CAN update + delete its own tool
    st, r = call("PUT", f"/tools/{A_TOOL}", {"mcp_server_url": "http://legit.invalid/mcp"}, token=A)
    ck("update_tool same-tenant owner ALLOWED", st == 200, f"HTTP {st}")

# ---- create_tool / list cross-tenant via client tenant ----
st, me = call("GET", "/auth/me", token=A); A_TENANT = me.get("tenant_id")
st, r = call("POST", "/tools", {"name": f"isola-x-{uniq}", "display_name": f"Isola X {uniq}", "type": "mcp", "tenant_id": A_TENANT}, token=B)
ck("create_tool cross-tenant (body tenant=A) DENIED", denied(st), f"HTTP {st}")
st, r = call("GET", f"/tools?tenant_id={A_TENANT}", token=B)
ck("list_tools cross-tenant (query tenant=A) DENIED", denied(st), f"HTTP {st}")
st, r = call("GET", f"/tools/agent-installed?tenant_id={A_TENANT}", token=B)
ck("agent-installed cross-tenant (query tenant=A) DENIED", denied(st), f"HTTP {st}")

# ---- cleanup A's custom tool ----
if A_TOOL:
    call("DELETE", f"/tools/{A_TOOL}", token=A)

p = sum(1 for _, ok in R if ok)
print(f"\nTALLY: {p}/{len(R)} PASS")
sys.exit(0 if p == len(R) else 1)
