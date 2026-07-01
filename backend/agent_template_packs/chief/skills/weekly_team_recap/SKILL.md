---
name: weekly_team_recap
description: Produce the Friday team performance summary — agent-by-agent metrics, skill gaps, token spend, and carry-forwards for next week.
---

# Weekly Team Recap

**When to use:** Friday cadence, or when owner asks for a weekly review. Triggers: "weekly recap", "how was this week?", "Friday summary", "team report", any Friday check-in with Chief.

This skill covers **agent team performance** — how well each agent did its job. It is distinct from Brief's weekly_recap, which covers business outcomes (revenue, customer metrics). Don't conflate the two.

## What to include

1. **Per-agent scorecard** for each active agent this week:
   - Volume: messages / tasks handled
   - Escalation rate: % escalated vs self-resolved
   - Error rate: errors this week
   - Owner approvals required (L1/L2 agents only)
   - One notable win if identifiable (handled a complex situation cleanly)
   - One notable miss if identifiable (should have handled; failed to respond; over-escalated)

2. **Cross-agent coordination** — did agents hand off effectively? Any gap in workflow coverage?

3. **Skill coverage gaps** — customer requests or owner questions no agent handled well this week.

4. **Token spend summary** — weekly total, per-agent breakdown.

5. **Carry-forwards for next week** — specific improvements, re-prompting needs, or config changes worth discussing with Eric.

## Output format

```
Weekly Team Recap — week of {date}

REX:
  Handled: {N} | Escalated: {N} ({%}) | Errors: {N}
  Win:  {one-line or "none"}
  Miss: {one-line or "none"}

{repeat per agent}

Cross-agent:
  {observation or "no gaps this week"}

Skill gaps:
  {gap or "none identified"}

Token spend (week):
  Total: {N} | Rex: {N} | Chief: {N} | {others if present}

Carry-forwards:
  - {item}
  (none if clean week)
```

## What NOT to do

- Don't include business revenue or customer satisfaction scores — those belong in Brief's weekly_recap.
- Don't skip carry-forwards when genuine gaps exist. Eric uses these to tune agents.
- Don't produce this report outside of Friday unless explicitly asked.
