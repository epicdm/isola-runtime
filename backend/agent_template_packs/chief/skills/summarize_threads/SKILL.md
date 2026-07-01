---
name: summarize_threads
description: Compress a set of conversations, messages, or records into a structured actionable summary for the owner.
---

# Summarize Threads

**When to use:** Owner wants a high-signal view of noisy data. Triggers: "summarize recent messages", "what did customers say this week?", "compress the Rex logs", "give me the highlights from today's conversations", "TL;DR on the last 48 hours".

## What counts as a thread

- Customer conversations handled by Rex or any front-line agent
- Internal agent activity logs
- ERP record sets (open invoices, lead pipeline, ticket queue)
- Owner-to-agent message history

## Compression rules

1. **Group by topic or outcome.** Cluster by: bookings, complaints, questions, unresolved items. Don't summarize message-by-message.
2. **Lead with numbers.** Quantity first, then pattern, then exception.
   - GOOD: "14 inbound messages: 9 handled cleanly, 3 escalated, 2 pending."
   - BAD: "There were quite a few messages and most were resolved..."
3. **Surface anomalies prominently.** Anything outside normal range goes first.
4. **Include owner-action items** at the end — explicit list, not embedded in prose.
5. **Cut sentiment language.** No "it seems like", "appears to be", "relatively speaking".

## Output format

```
Summary — {source} | {date range}

Volume:  {N items}
Handled: {N clean} | {N escalated} | {N pending}
Topics:  {topic 1 (N)}, {topic 2 (N)}

Notable:
  - {anomaly or highlight}
  (none if clean)

Owner action needed:
  - {item} [urgent / routine]
  (none if clean)
```

## What NOT to do

- Don't summarize if you have fewer than 3 items — list them directly instead.
- Don't include customer names or phone numbers unless the owner specifically asked for a named record.
- Don't pad thin data. If there's nothing notable, say: "No anomalies in this period."
