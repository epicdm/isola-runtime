---
name: share_menu_or_catalog
description: Send the menu, price list, service list, or product catalog as a document or photo the customer can browse in WhatsApp.
---

# Share menu / catalog

**When to use:** Customer asks "can I see the menu", "what do you have", "send me your prices", "do you sell X", "what services", "what's on tonight". Any ask for the item/service list.

## Source of truth

Menu / catalog files live in `workspace/catalog/` — could be:
- `menu.pdf` (restaurant, hotel)
- `services.md` (salon, clinic — text list with prices)
- `products.md` (retail)
- Images: `menu-drinks.jpg`, `services-treatments.png`

Use `list_files` on `workspace/catalog/` to see what's actually available for THIS tenant.

## How to share

**PDFs / images** — the channel layer will deliver these as WA attachments if you reference them by path. Use:
```
[attach:workspace/catalog/menu.pdf]
Here's our full menu, {Name} — let me know what you'd like.
```

**Text catalogs (services.md)** — read and reply with a compact summary, not the raw file.
- Group by category (e.g. "Haircuts / Color / Treatments")
- Max 8 items in the reply; if more, say "full list when you're ready — just ask for 'all services'"
- Include prices if they're in the file

## Follow-up

After sharing, offer next step:
> "Want me to book something?" → leads into `book_reservation`
> "Want me to hold X for pickup?" → leads into `take_delivery_order`

## Edge cases

- **No catalog in workspace** → say "we haven't uploaded our menu yet — let me take your question directly" and escalate.
- **Customer asks for a specific item not in the catalog** → say "I don't see that on today's menu, but I can check with the kitchen" → escalate.

## What NOT to do

- Don't dump a huge text list in one message. Summarize.
- Don't send the same menu twice in one conversation — they have it.
