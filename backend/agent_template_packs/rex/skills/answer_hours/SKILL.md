---
name: answer_hours
description: Answer "when are you open?" — return today's hours, or specific-day hours, or the weekly schedule, based on what the customer asks.
---

# Answer hours / opening times

**When to use:** Customer asks "are you open", "what time do you close", "open tomorrow?", "hours today", "when does kitchen stop".

## Source of truth

Always read `workspace/hours.md` first. It looks like:

```
Monday    09:00–22:00
Tuesday   09:00–22:00
...
Sunday    closed
```

Holidays + overrides live in `workspace/holidays.md` if present.

## Response shape

- **Specific day asked** → one-liner.
  > "We're open 9 AM to 10 PM on Friday."
- **"Open now?"** → compute against current time (you have it in your system context).
  > "Yes — we're open for another 3 hours." or "We just closed, but we open at 9 AM tomorrow."
- **"Hours today?"** → today only, one line.
- **"What are your hours?"** with no specific day → give the weekly summary, 7 lines.

## Edge cases

- If today is a holiday override, lead with that:
  > "Today we're closed for the holiday, but tomorrow is normal 9 to 10."
- If a specific day is outside the schedule (some businesses close Sundays), say so clearly: "We're closed Sundays."
- Don't bury the answer. The **first sentence** must contain the answer.

## What NOT to do

- Don't invent hours. If `hours.md` is missing or ambiguous, say "let me double-check and come back to you" and escalate.
- Don't list every day when they asked about one.
