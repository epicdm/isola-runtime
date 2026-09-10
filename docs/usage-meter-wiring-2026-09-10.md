# Wiring the enforced usage meter into the two live call paths

This PR ships the engine, the migration and the proofs. The two call-site
edits below are the last step; they are stated as exact diffs rather than
applied blind, because `heartbeat.py` (485 lines) and `websocket.py` (678
lines) are large files whose deployed content has drifted from the
`feat/s5-customer-odoo-answer-handoff` worktree on deepseek -- the running
image matches `main`, the checked-out tree does not. Apply against `main`.

**Nothing in this PR takes effect until these two edits are made and the
image is rebuilt. Merging the PR alone changes no behaviour.**

---

## 1. `backend/app/services/heartbeat.py` -- the bypass

This is the path that matters. `_execute_heartbeat()` calls
`client.complete()` inside `for round_i in range(20)` and never touches
`quota_guard`. 278 agents wake on a 240-minute heartbeat; none of those
calls is checked or counted.

```diff
@@ async def _execute_heartbeat(agent_id):
         try:
             client = create_llm_client(
                 provider=model_provider,
                 api_key=model_api_key,
                 model=model_model,
                 base_url=model_base_url,
                 timeout=float(model_request_timeout or 120.0),
             )
+            from app.services.llm_metered import MeteredLLMClient
+            client = MeteredLLMClient(
+                client,
+                agent_id=agent_id,
+                tenant_id=agent_tenant_id,
+                provider=model_provider,
+                model=model_model,
+                model_id=model_id,
+                intent="heartbeat",
+            )
         except Exception as e:
             logger.error(f"Failed to create LLM client: {e}")
             return
```

and the round loop must stop cleanly when budget runs out rather than
raising into the generic handler:

```diff
         for round_i in range(20):
             try:
                 response = await client.complete(...)
+            except UsageLimitExceeded as e:
+                logger.info(f"Heartbeat for {agent_name} stopped: {e.message}")
+                reply = ""
+                break
             except LLMError as e:
```

with `from app.services.usage_meter import UsageLimitExceeded` added to the
import block already present at that point in the function.

`agent_tenant_id` and `model_id` are read in Phase 1 of the same function
alongside `model_provider` / `model_model`; bind them there if not already
in scope.

## 2. `backend/app/api/websocket.py` -- the guard that is imported and never called

Line ~353 imports `check_agent_llm_quota` and then does not call it. The
counter is incremented at line ~602 against a ceiling nothing reads.

```diff
                 await check_conversation_quota(user_id)
                 await check_agent_expired(agent_id)
+                await check_agent_llm_quota(agent_id)
```

That one line makes the legacy guard do what its name says. The stronger
fix is to wrap this path's client in `MeteredLLMClient` as well and delete
the `increment_agent_llm_usage` call at ~602, since the wrapper reserves
atomically and the read-then-write increment can lose updates under
concurrent sessions on the same agent.

## 3. Schedule the reset job

`usage_meter.reset_daily_counters()` is the job `last_daily_reset` never
had. Register it next to the heartbeat loop in the FastAPI startup:

```python
async def start_usage_reset():
    while True:
        async with async_session() as db:
            await reset_daily_counters(db)
        await asyncio.sleep(600)
```

Reservation does its own lazy per-row rollover, so enforcement is correct
even if this job never runs. The job exists so `tokens_used_today` stops
being a lifetime counter and daily reporting means something.

## Verification after deploy (not before)

1. `SELECT SUM(llm_calls_today) FROM agents;` -- today this reads **2**
   across 444 agents. After wiring it should track the real call volume.
2. `SELECT COUNT(*), MAX(created_at) FROM llm_call_telemetry;` -- today
   **266 rows, last 2026-07-09**. `MAX(created_at)` must advance.
3. Set one non-customer agent's `max_llm_calls_per_day` to 0 and confirm its
   next heartbeat window produces no `llm_call_telemetry` rows for it and no
   `agent_activity_logs` `tool_call` rows -- refusal observed in the data,
   not inferred from an API response.
