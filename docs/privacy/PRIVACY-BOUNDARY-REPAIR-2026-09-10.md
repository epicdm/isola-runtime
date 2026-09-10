# Privacy boundary repair — measured findings and remaining call-site hunks

Lane PRIVACY, 2026-09-10. Branch `fix/privacy-principal-scoped-sessions-2026-09-10`.
**Not for merge and not for deploy.** Review artefact.

## What was measured, not assumed

The claim under test: *"`isolaruntime.chat_sessions` has no privacy boundary —
owner and staff sessions sit in one table keyed by `agent_id`."*

That claim is **wrong about the schema and right about the consequence.**
`chat_sessions` is keyed by `(agent_id, user_id)`; `user_id` is `NOT NULL` and
indexed, and the web chat path (`app/api/websocket.py`) loads history strictly by
`conversation_id` for a session it has already proved belongs to the connecting
user (websocket.py:229). Retrieval on the web path is principal-scoped today.

The boundary fails in four other places:

1. **`app/api/chat_sessions.py::_can_view_all_agent_chat_sessions`** grants
   `scope=all` — every session on the agent, and every message inside them — to
   any `platform_admin`, `org_admin` or `agent_admin`. On this estate
   **297 of 330 users hold `org_admin`.** `agent_permissions` is never consulted
   for that decision.
2. **`app/api/whatsapp.py:527`** calls
   `_call_agent_llm(..., user_id=agent.creator_id, ...)`. Every inbound WhatsApp
   message runs its tools as the agent's creator, whoever sent it. The sender's
   identity is discarded at the tool boundary.
3. **`app/services/agent_tools.py::execute_tool`** receives `user_id` and never
   consults `AgentPermission` with it. The only gates are the sandbox flag and
   `autonomy_service.check_and_enforce`, which is per-*agent*
   (`agents.autonomy_policy`), not per-*principal*. `user_id` reaches the DB only
   as the log string `requested_by`. Callers pass
   `user_id=user_id or agent_id` (`app/services/llm/caller.py:252`), i.e. the
   agent's own id stands in for a person.
4. **`app/services/trigger_daemon.py:296-310`** — the `on_message` /
   `from_agent_name` branch joins `ChatSession` and then never constrains it. It
   takes the newest assistant message from that participant in **any** session
   and copies 2000 characters into `config["_matched_message"]`, which is
   injected into the triggered agent's context. The `from_user_name` fallback at
   :356 drops the `ChatSession.user_id` filter for the same reason.

## What this branch ships

- `backend/app/core/session_privacy.py` — one enforcement point, built on the
  permission store that already exists (`agent_permissions`). No new roles, no
  second permission system.
- `backend/app/core/permissions.py` — `check_agent_access` now delegates
  resolution to that module, so routes, retrieval and call-time tool
  authorization answer from the same live read. It also honours `users.is_active`
  and resolves **least privilege** across matching rows (never wider than before).
- `backend/app/models/chat_session.py` + Alembic
  `p1_privacy_session_visibility` — `visibility` column, `private` by default,
  conservative backfill (everything existing becomes private; group and
  agent-to-agent sessions become shared because they never had a private
  principal).
- `backend/tests/test_agent_privacy_boundary.py` — 11 tests, synthetic owner and
  synthetic staff, no database and no writes.

## Remaining call-site hunks (specified, not applied blind)

### 1. `backend/app/api/chat_sessions.py`

Replace the module-level helper:

```python
def _can_view_all_agent_chat_sessions(user: User, agent: Agent) -> bool:
    return (
        user.role in ("platform_admin", "org_admin", "agent_admin")
        or str(agent.creator_id) == str(user.id)
    )
```

with a version that takes the resolved level and never reads `user.role`:

```python
from app.core.session_privacy import can_read_session, readable_sessions_clause, MANAGE

def _can_view_all_agent_chat_sessions(access_level: str | None) -> bool:
    return access_level == MANAGE
```

Then:

- `list_sessions`, `scope == "all"` branch: keep the `manage` gate, and add
  `readable_sessions_clause(current_user, access_level)` to the `select()` so a
  manager sees shared sessions and their own, never someone else's private one.
- `rename_session`, `delete_session`, `get_session_messages`: replace
  `if str(session.user_id) != str(current_user.id) and not _can_view_all_agent_chat_sessions(current_user, agent)`
  with `if not can_read_session(current_user, session, access_level)`.
- `create_session`: no change; new sessions are private by column default.

The existing test `tests/test_chat_sessions_api.py::test_org_admin_can_list_all_sessions`
asserts the old behaviour and must be rewritten — it currently pins the leak in place.

### 2. `backend/app/api/whatsapp.py` (~line 527)

```diff
-            user_id=agent.creator_id,
+            user_id=sess.user_id,   # the person who actually sent this message
```

The same substitution applies to the `ChatMessage(...)` rows written around it.

### 3. `backend/app/services/agent_tools.py::execute_tool`

After the sandbox gate, before `ensure_workspace`:

```python
from app.core.session_privacy import PrincipalDenied, assert_tool_allowed
...
    async with async_session() as _pdb:
        try:
            await assert_tool_allowed(_pdb, tool_name, agent_id, user_id)
        except PrincipalDenied as denial:
            logger.info(f"[Privacy] {tool_name} denied for principal {user_id}: {denial.detail}")
            return f"❌ {denial.detail}"
```

And at `backend/app/services/llm/caller.py:252`, stop substituting the agent for
a person: `user_id=user_id` (let the gate refuse a missing principal).

### 4. `backend/app/services/trigger_daemon.py`

In the `from_agent_name` branch, constrain the join that is currently unbounded:

```diff
                     ).where(
+                        ChatSession.agent_id == trigger.agent_id,
+                        ChatSession.visibility == "shared",
                         ChatMessage.participant_id == from_participant,
```

and delete the `else:` fallback at :356 that searches every session on the agent
when the named user cannot be resolved. Failing closed is correct here.

## What this branch does not prove

Nothing here has been run against a live agent. The tests prove the predicates
and the resolver; they do not prove the wiring, because the wiring is the four
hunks above.
