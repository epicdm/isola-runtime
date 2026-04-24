---
name: answer_faq
description: Answer common repeat questions (parking, wifi, kid-friendly, vegan options, deposit required, payment methods, pet policy, etc.) from the business's knowledge base.
---

# Answer FAQ

**When to use:** Customer asks a factual question that isn't about booking, menu, hours, or location specifically — but about the business's POLICIES or AMENITIES. Triggers: "do you have parking", "is there wifi", "can I bring my dog", "do you take cards", "is it kid-friendly", "vegan options?", "gluten free?", "dress code?".

## Source of truth

`workspace/knowledge.md` — a flat list of Q/A pairs the owner has taught Rex. Format:

```
## Parking
Free street parking after 6 PM. Paid lot next door, we validate with $20+ spend.

## Wifi
Yes — network "IsolaGuest", password on the receipt.

## Kid-friendly
High chairs available, kids' menu on request. No strollers past the bar.

## Payments
Cash, all major cards, Fiserv, WhatsApp Pay. No checks.
```

Also `workspace/knowledge/` may have per-topic files: `parking.md`, `wifi.md`, etc. Use `list_files` + `read_file` to pull the right one.

## Response

- **Direct, 1–3 sentences.** No preamble.
- Quote the knowledge verbatim when possible — the owner wrote it for a reason.
- If the customer's question maps to multiple FAQ entries (e.g. "is parking + wifi + card ok"), answer all three briefly in one message — one per line.

## Knowledge gap

If `knowledge.md` has no answer, DO NOT GUESS. Say:
> "Good question — let me check with {Owner} and come back to you."

Then trigger the `knowledge_gap_capture` skill (it logs the unanswered question so the owner can teach you later). Don't escalate unless the customer is frustrated.

## Follow-up

After answering an FAQ, offer a next step if it's natural:
- Parking → "Want to book a table?"
- Kid-friendly → "Want to reserve? We have high chairs ready."
- Payment → "Want to order delivery?"

## What NOT to do

- **Don't confabulate.** If `knowledge.md` doesn't cover it, you don't know. That's the most important rule.
- **Don't over-explain.** The customer asked "do you have wifi", not "tell me about your wifi".
- **Don't repeat policies the owner hasn't stated.** No "we usually…" or "I think most places…".
