---
name: cancel_reservation
description: Cancel an existing reservation the customer already has. Find it, confirm the right one, mark it cancelled.
---

# Cancel a reservation

**When to use:** Customer wants to cancel a booking, appointment, or reservation they previously made. Triggers: "cancel my booking", "I can't make it", "please cancel", "something came up".

## Procedure

1. **Look them up.** Read `workspace/bookings.md` and find entries matching their name or phone number. Show the most recent one + date/time.
2. **Confirm which one.** If more than one exists, ask:
   > "I see two reservations — {day1} at {time1} and {day2} at {time2}. Which one should I cancel?"
3. **Confirm the cancellation explicitly.** One short message:
   > "Cancelling {day} at {time}. Reply YES to confirm, or REPLY KEEP to hold it."
4. **On YES:** mark the bookings.md row as cancelled by prepending `~~` to the line and appending `· cancelled {today}`. Example:
   ```
   - ~~2026-04-26 · 19:30 · Marie (party 4) · birthday · booked via WhatsApp~~ · cancelled 2026-04-25
   ```
5. Send a warm confirmation: "Done — your {time} is cancelled. Hope to see you another time, {Name}."

## Escalate when

- The booking falls in a late-cancellation window the business charges for (check `workspace/cancellation-policy.md` if it exists)
- The customer is cancelling within 2 hours of the time — owner may want to know
- They ask to dispute a charge or deposit

## What NOT to do

- Never cancel without explicit YES — "I want to cancel" is an intent, not a confirmation.
- Don't delete the row; keep it marked cancelled so the owner has an audit trail.
- Don't volunteer refunds or credits unless `workspace/knowledge.md` explicitly authorizes it.
