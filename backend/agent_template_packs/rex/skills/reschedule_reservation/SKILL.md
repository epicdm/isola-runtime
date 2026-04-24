---
name: reschedule_reservation
description: Move an existing reservation to a different date/time. Find the booking, propose available slots, confirm the new one.
---

# Reschedule a reservation

**When to use:** Customer wants to change the date or time of an existing booking. Triggers: "can I move my booking", "push back to", "can we switch to Friday instead", "different time".

## Procedure

1. **Find the existing booking** in `workspace/bookings.md` (same approach as cancel_reservation — look up by name/phone).
2. **Confirm which one** if multiple.
3. **Ask for the new preference.** One question: "What day and time would work better?"
4. **Check availability.** Cross-reference `workspace/bookings.md` for same-slot conflicts + `workspace/hours.md` for opening hours.
5. **Propose:**
   - If the requested slot is open → "That works. Moving you to {newDay} at {newTime}. Reply YES to confirm."
   - If it's not available → "That slot's taken. I have {option1} or {option2} — either of those work?"
6. **On YES:** edit the bookings.md row: strike the old datetime, append `→ {newDay} {newTime}`, and add `· rescheduled {today}`.
   ```
   - ~~2026-04-26 · 19:30~~ → 2026-04-27 · 20:00 · Marie (party 4) · birthday · rescheduled 2026-04-25
   ```

## Combine with

If the business allows, offer to send a reminder the day before via the `send_reminder` skill.

## Escalate when

- Same customer has rescheduled 3+ times on the same booking
- They ask for a slot that's booked solid and you can't offer an alternative within the week

## What NOT to do

- Don't leave the original slot marked as booked — always mark it rescheduled so the owner sees the audit trail.
- Don't over-propose: max 2 alternative slots per message.
