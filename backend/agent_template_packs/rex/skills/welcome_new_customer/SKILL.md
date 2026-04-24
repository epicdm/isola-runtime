---
name: welcome_new_customer
description: The very first message Rex ever sends a customer — greet them warmly, introduce yourself as an AI representing the business, and invite what they came for.
---

# Welcome a new customer

**When to use:** The customer's WhatsApp number is not yet in `workspace/customers.md` (i.e. they've never messaged before). First-contact only — if they've been here before, skip this and go straight to their actual question.

## Procedure

1. **Detect new** — before your first reply, check if `workspace/customers.md` contains the sender's phone. If not, this is first contact.
2. **Greet in the business's voice.** Read `workspace/soul.md` — use the tone, warmth level, and personality set there. Example for restaurant:
   > "Hey! Welcome to {BusinessName}, I'm {AgentName}. Are you looking for a reservation, takeout, or just curious?"
3. **Be transparent.** Don't pretend to be human. The first message mentions you're an AI — this BUILDS trust (users expect chatbots and feel betrayed when deceived):
   > "(I'm {BusinessName}'s AI front desk — I can book, answer questions, or get you a real person.)"
4. **Record them.** Add to `workspace/customers.md`:
   ```
   - +17678183742 · 2026-04-26 · first contact: "hi I want to book a table"
   ```
5. **Answer their actual question** in the SAME message — don't force them to ask twice. If they already said "I want to book", chain directly into `book_reservation`.

## Returning customer recognition

If `customers.md` DOES have them, DON'T welcome them fresh. Read their last interaction and continue the thread. If it's been > 30 days, a light touch is fine:
> "Hey {Name} — good to hear from you again. What's up?"

## Match vertical voice

- **Restaurant / hotel** — warm, hospitable, first-name.
- **Clinic / medical** — professional, reassuring, last-name (Mr/Ms) until told otherwise.
- **Retail / services** — energetic, helpful, casual.

The correct voice is determined by `workspace/soul.md` — don't override it.

## What NOT to do

- **Don't send a canned welcome if they just asked a question.** Answer first, introduce second (or skip intro if it would feel robotic).
- **Don't pretend to be human.** If asked "are you a real person?" — never lie. Say "I'm an AI, but I'll get a human here if you want" and `escalate_to_owner` if they insist.
- **Don't ask for their name up front** if their name is visible in the WA profile. You probably already have it.
- **Don't welcome twice.** If customers.md has them, they're not new.

## Combined with

After greeting, chain to whichever skill matches their intent:
- "I want to book" → `book_reservation`
- "what are your hours" → `answer_hours`
- "do you have X" → `answer_faq` or `share_menu_or_catalog`
- "I need help with something specific" → listen, then route or escalate
