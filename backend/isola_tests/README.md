# Isola tenant-isolation matrix

`tenant_isolation_matrix.py` drives the live HTTP API of a running Clawith stack
and asserts the tenant-isolation guards on the agent/tool routes in
`app/api/tools.py` (def-clawith-tools-cross-tenant-write-2026-07-29).

It contains no secrets; supply configuration via environment:

```
export ISOLA_BASE=http://127.0.0.1:8891/api
export A_TOKEN=<bearer for an admin in tenant A (agent owner)>
export B_TOKEN=<bearer for an admin in a DIFFERENT tenant B (attacker)>
export A_AGENT_ID=<an agent owned by tenant A>
python3 backend/isola_tests/tenant_isolation_matrix.py   # exit 0 iff all pass
```

Guard logic that is not wire-reachable (e.g. `update_mcp_server`, whose PUT route
is shadowed by `PUT /{tool_id}`) is proven directly in
`backend/tests/test_tools_tenant_guards.py`.
