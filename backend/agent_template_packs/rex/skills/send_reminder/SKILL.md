---
name: send_reminder
description: Send a pre-visit reminder (24h before) or post-visit follow-up using an approved WhatsApp template — the only way to message a customer outside the 24-hour session window.
---

# Send reminder / follow-up via template

**When to use:** Schedule a reminder for a confirmed booking or send a post-visit thank-you/review request. CRITICAL — once the 24-hour WhatsApp customer session closes, you **cannot send free-form text**. Only pre-approved Meta templates work.

## EPIC has Meta Tech Provider status

This means approved templates in the shared EPIC WABA are available to every tenant without per-tenant Meta approval. Use these template names (exact, case-sensitive):

- `reminder_booking_24h` — "Hi {{1}}, just a friendly reminder — you have a booking tomorrow at {{2}}. Reply STOP if you need to cancel or change."
- `reminder_booking_2h` — "Hi {{1}}, see you in 2 hours! We're ready. Address in our last message."
- `post_visit_thanks` — "Thanks for coming in today, {{1}}! If we earned it, would you leave us a quick Google review? {{2}}"
- `order_ready_pickup` — "Hi {{1}}, your order is ready for pickup at {{2}}."

## Procedure

1. **Confirm there's something to remind about.** Read `workspace/bookings.md` or `workspace/orders.md`. Don't send reminders for cancelled rows.
2. **Schedule with `set_trigger`.** Don't send now — schedule for the right moment.
   - `reminder_booking_24h` → fire 24h BEFORE the booking time
   - `reminder_booking_2h` → fire 2h before
   - `post_visit_thanks` → fire 3h AFTER the booking end-time
   - `order_ready_pickup` → fire when status is flipped to ready (owner does this; you don't predict it)
3. **The trigger payload** is a template send:
   ```
   {
     "type": "whatsapp_template",
     "to": "{customer_phone}",
     "template_name": "reminder_booking_24h",
     "template_language": "en",
     "components": [{"type":"body","parameters":[{"type":"text","text":"{{Name}}"},{"type":"text","text":"{{time}}"}]}]
   }
   ```

## Unsubscribe handling

Every reminder template ends with "Reply STOP to opt out". If a customer replies STOP, the channel layer writes their phone to `workspace/opt-outs.md`. Before scheduling ANY future reminder for them, check that file. Never resend if opted out.

## What NOT to do

- **Never send reminders in free text after the 24h window** — Meta will flag the number for spam and the tenant's quality rating drops. Only templates.
- **Don't stack reminders.** Max 2 per booking (24h + 2h is fine; 24h + 18h + 6h + 2h is spam).
- **Don't send post-visit thanks to cancelled bookings.**
- **Don't schedule reminders without a confirmed booking.** Intent ≠ confirmation.
