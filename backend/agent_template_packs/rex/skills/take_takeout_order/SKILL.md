---
name: take_takeout_order
description: Take a takeout / pickup / delivery order inside WhatsApp — item list, quantities, pickup time, customer contact, payment method. Summary + pay link at the end.
---

# Take a takeout / pickup / delivery order

**When to use:** Customer wants to order food/goods for pickup or delivery. Triggers: "can I order", "delivery?", "pickup please", "I want to order X", "getting takeout".

## Procedure

1. **Confirm pickup or delivery.**
   > "Pickup or delivery?"
2. **If delivery**, get the address. Check `workspace/delivery-area.md` if it exists — if they're outside the zone, say so and pivot to pickup.
3. **Collect items.** Accept natural phrasing ("one pizza, two cokes"). Parse against `workspace/catalog/menu.pdf` or `workspace/catalog/products.md`.
   - If an item isn't on the menu, ask: "Do you mean {closest match}, or something else?"
   - If they want something you're out of (check `workspace/out-of-stock.md` if present), offer a substitute.
4. **Ask pickup time / delivery window.**
   > "When would you like it? Ready in about 25 minutes, or pick a time."
5. **Name + callback.** Usually their WA number is enough. If they've used the service before, skip.
6. **Summarize and quote total.** Use the menu's prices — don't invent totals.
   > ```
   > **Your order — Marie**
   > 2× Margherita pizza — EC$40
   > 3× Coke — EC$9
   > Pickup 19:15
   > Total: EC$49
   > Reply YES to confirm or CHANGE to edit.
   > ```
7. **On YES, write the order** to `workspace/orders.md` (newest first):
   ```
   - 2026-04-26 19:15 · pickup · Marie · 2 margherita, 3 coke · EC$49 · via WhatsApp · confirmed
   ```
8. **Send payment link.** The channel layer has a `[pay:amount=49;ref=order-marie-1930]` marker — it resolves against the tenant's Fiserv (primary) or Stripe (fallback) account.
9. **Final reply:**
   > "Thanks {Name} — pay here when ready: {link}. We'll start when payment lands. See you at {time}."

## Escalate when

- Order total > EC$200 (configurable via `workspace/order-limits.md`)
- Delivery address outside stated zone
- Allergy, special dietary request, or custom modification not on the menu
- Payment link fails twice

## What NOT to do

- **Don't take an order without prices.** Always quote total.
- **Don't promise a time you can't hit** — check `workspace/kitchen-backlog.md` if it exists (owner may update this during rushes).
- **Don't send the pay link before YES.** No one pays before confirming.

## Integration with other skills

- After send, set a trigger via `set_trigger` to check back in 20 min if they haven't picked up yet → "Your order's ready, still coming?" (no spam — ONE follow-up only).
