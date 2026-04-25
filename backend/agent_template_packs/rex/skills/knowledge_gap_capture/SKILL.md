---
name: knowledge_gap_capture
description: When you don't know the answer to a customer's question, log it as a knowledge gap so the owner can teach you later. This is how Rex gets smarter over time.
---

# Capture a knowledge gap

**This is the silent fallback.** First-choice for an unknown factual question is `ask_owner_live` — emit `[ask_owner: ...]` so the owner answers in real time. Use THIS skill only when ask_owner_live doesn't apply: the owner has no `owner_phone` configured, the question is the third+ time today, or it's clearly low-priority chitchat.

**When to use:** A customer asks a factual question about the business — a policy, an amenity, a product, a service — and `workspace/knowledge.md` does not have the answer, AND `ask_owner_live` is not appropriate. Examples:

- "Do you have a lactation room?"
- "Is there valet?"
- "What's your kitchen closing time on holidays?"
- "Can I book a private dining room?"
- "Do you ship to Barbuda?"

Triggered from inside `answer_faq` when the lookup returns nothing relevant.

## Procedure

1. **Don't guess.** Say to the customer:
   > "Good question — let me check with {Owner} and come back to you."
2. **Log the gap** to `workspace/knowledge-gaps.md` (create it if missing). Prepend newest:
   ```
   - 2026-04-26 19:42 · +17678183742 · asked: "Do you have a lactation room?" · not yet taught
   ```
3. **Count repeats.** If this same question (normalized) has been asked 3+ times in the last 7 days, upgrade it:
   - Prepend a `!` to the line:
     ```
     - ! 2026-04-26 19:42 · +17678183742 · asked: "Do you have a lactation room?" · 3rd time this week
     ```
   - Fire an escalation with reason `knowledge_gap_repeated` so the owner knows to update `knowledge.md`.
4. **Stay in conversation.** The customer is waiting for a human answer — stay responsive if they ask a DIFFERENT question (book, menu, hours), but don't fake-answer the gap you just logged.

## Weekly digest

On Sundays 9 AM (schedule via `set_trigger` during agent onboarding), email/WA the owner a digest:
> "This week Rex didn't know: 12 things. Top 3 most-asked:
>  1. Lactation room (4×)
>  2. Valet parking (3×)
>  3. Holiday hours (3×)
> Teach me at {link to knowledge editor}."

## When answer arrives

When the owner adds a new entry to `knowledge.md` (they do this via the Isola dashboard), the gap rows matching that topic are auto-marked `· taught`. Check your gap file for any `· taught` rows next time the same customer messages — proactively:
> "By the way, you asked about lactation rooms last week — yes, we have one near the lounge, just ask at reception."

This is the self-writing knowledge loop: gap → capture → teach → proactive answer.

## What NOT to do

- **Don't escalate on the first gap** — only on repeats (3+) or if the customer is getting impatient.
- **Don't promise a specific follow-up time.** "I'll check" is honest; "I'll answer in 5 minutes" is a lie.
- **Don't hide the gap.** Sending a vague non-answer ("we have many amenities!") is worse than "let me check". Customers know when you're stalling.
