---
name: oversee_agents
description: Run a health check on all active agents in the tenant — status, errors, token spend, and anomalies — and surface a concise report.
---

# Oversee Agents

**When to use:** Owner asks about agent health. Triggers: "are the agents okay?", "how's Rex doing?", "any errors today?", "check on the team", "agent status". Also fires automatically as part of state_of_business and weekly_team_recap.

## What to check

For each active agent in the tenant:

1. **Status** — running / halted / degraded. Source: agent health endpoint.
2. **Error count** — errors in last 24h. Anything > 0 surfaces the last error message (redacted of customer PII).
3. **Token spend** — today's usage vs baseline. Flag if > 2x expected.
4. **Last active** — timestamp of last successful outbound message or action.
5. **Pending queue** — inbound messages or tasks waiting unhandled.

## Alerting thresholds

| Signal | Threshold | Action |
|---|---|---|
| Agent halted | Any halt | Escalate to Eric immediately via escalate_to_eric |
| Error rate | > 3 errors / 24h | Surface in report |
| Token spend | > 2x daily baseline | Surface in report |
| Last active (front-line) | > 4 hours | Surface as warning |
| Pending queue | > 5 items unhandled | Surface as warning |

## Output format

```
Agent health — {timestamp}

{agent_name}:  {status} | {N} errors | {N} tokens today | last active {time ago}
  {warning detail or "clean"}

Summary: {N} agents — {N} green, {N} warning, {N} halted
```

## What NOT to do

- Don't mark an agent healthy if you couldn't retrieve its status — surface "status unavailable".
- Don't suppress warnings. Surface everything above threshold; Eric decides what to act on.
- Don't conflate token count with cost — report tokens; leave cost math to Eric.
