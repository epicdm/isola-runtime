---
name: share_directions
description: Send the business's address, a Google Maps link, and a landmark hint when a customer asks where to go.
---

# Share directions / location

**When to use:** "Where are you?", "What's the address?", "How do I get there?", "Can you drop a pin?", "Are you near X?".

## Source of truth

`workspace/location.md` contains:
```
name: {Business Name}
address: {Street, City, Country}
maps_url: https://maps.google.com/?q=... (or https://maps.app.goo.gl/...)
landmark: {Opposite the bank, next to the cathedral, etc.}
parking: {Street parking, paid lot, validated on request}
```

## Response

Send these three lines, in this order:

1. **The address in plain text** (customers screenshot + paste into rideshare apps)
2. **The Google Maps link** (tappable in WhatsApp)
3. **One landmark hint** (saves confused callers)

Example:
> **Isola Bistro**
> 34 Great Marlborough Street, Roseau, Dominica
> https://maps.app.goo.gl/abc123
> We're right across from the fish market — blue door.

## Native location message (when available)

If the WhatsApp channel supports it (Meta Cloud API), send a **location message** with the lat/lon from `location.md`. That drops a pin the customer can tap to open their own maps app. Channel layer handles this; reference via:
```
[location:lat=15.3005,lon=-61.3881,name="Isola Bistro",address="34 Great Marlborough Street"]
```

## Follow-up

After sharing, offer:
> "Want a reservation while you're on the way?" (leads to `book_reservation`)
> "Anything specific you're coming for?" (leads to `share_menu_or_catalog` or `answer_faq`)

## Edge cases

- **Rural / no map pin** — skip the maps_url line, give turn-by-turn if `location.md` has `directions_text`.
- **Multiple locations** — if `workspace/locations/` has multiple files, ask which one first:
  > "We have two — uptown or seafront?"

## What NOT to do

- Don't give GPS coordinates as numbers ("15.30, -61.38") — they're useless to humans.
- Don't repeat the maps link if you already sent it this conversation.
