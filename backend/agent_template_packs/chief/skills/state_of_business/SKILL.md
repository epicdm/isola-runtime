---
name: state_of_business
description: Produce an on-demand status brief for the owner — revenue snapshot, pipeline, customer activity, agent health, and items needing attention.
---

# State of Business

**When to use:** Owner asks for a status update. Triggers: "how's the business doing?", "give me a summary", "what happened today?", "quick update", "status report", or any open-ended performance question with no specific agent or topic named.

## What to include

Compile in this order:

1. **Revenue snapshot** — confirmed bookings/sales today, open invoices if retrievable. Pull from ERP via MCP if connected; note "ERP not queried" if unavailable.
2. **Pipeline summary** — active leads count, leads closed today, next follow-up due. Source: CRM agent (query Joey if present; fall back to direct ERP query).
3. **Customer activity** — inbound messages handled today, escalations triggered, satisfaction signals. Source: Rex conversation log.
4. **Agent health** — errors, token spend anomalies, or HALT states in last 24h. Source: oversee_agents.
5. **Owner attention items** — unresolved escalations, pending approvals, anything flagged by any agent.

## Output format

Use this structure every time:

```
Business snapshot — {date}

Revenue:   {amount or "not retrieved"}
Pipeline:  {N leads active, N closed today}
Activity:  {N customer messages, N escalations}
Agents:    {all green / N warnings / N halted}

Needs your attention:
  - {item}
  (none if clean)
```

One number per line. No narrative padding.

## What NOT to do

- Don't produce a report with zero data — surface "not retrieved" per section and name which queries failed.
- Don't include customer names or phone numbers — owner drills down if needed.
- Don't pull from memory alone; always query live sources when available.
