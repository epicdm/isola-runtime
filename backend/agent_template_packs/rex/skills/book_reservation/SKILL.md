---
name: book_reservation
description: Take a reservation/appointment from a customer on WhatsApp — collect party size, date, time, name — confirm back with a short summary.
---

# Book a reservation

**When to use:** The customer wants to book, reserve, or schedule something — a table, appointment, room, fitting, consultation, delivery slot. Detect intent from phrases like "book me", "I'd like to reserve", "can I get a table", "appointment tomorrow", etc.

## What to collect

Ask for whatever is missing in THIS order (don't ask for things they already gave you):

1. **Party size / number of people** (or "just me")
2. **Date** — accept natural phrasing (tonight, Friday, June 3). Resolve against the business's timezone.
3. **Time** — resolve against opening hours. If outside hours, say so and propose the closest open slot.
4. **Name** — first name is enough.
5. **Phone** — only if their WhatsApp number isn't recognizable as a callback line. Skip otherwise.
6. **Special requests** — birthday, allergies, high chair, ground floor room, etc. (optional, one short question)

## How to respond

- **One question per message.** Never ask a wall of questions.
- After everything collected, confirm with ONE summary message:
  > Got it, {Name} — **{party} people, {day} at {time}**. {Any special request noted.} See you then! Reply CHANGE if anything is off.
- If the time requested is outside opening hours, say so explicitly and propose two nearby options.
- If the business's `workspace/bookings.md` or knowledge base has capacity rules (e.g. "max 8 per table"), respect them.

## Recording the booking

Write the reservation to `workspace/bookings.md` as one line per booking:

```
- 2026-04-26 · 19:30 · Marie (party 4) · birthday · booked via WhatsApp
```

Prepend newest at the top. Keep the file short — if it grows past 50 rows, move older rows to `workspace/bookings-archive.md`.

## Escalate when

- The customer asks for something unusual (private room, dietary override, group > 12)
- Same customer tries to book the same slot twice
- They ask to speak to a manager
→ Use the `escalate_to_owner` skill.

## What NOT to do

- Don't invent capacity / hours — if unsure, say "let me double-check" and read `workspace/knowledge.md` or `workspace/hours.md`.
- Don't commit to a booking while still missing a required field.
- Don't promise things outside your authority (free dessert, 50% off). Those get escalated.
