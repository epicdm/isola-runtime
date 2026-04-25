---
name: ask_owner_live
description: When a customer asks a factual business question you don't know, ping the owner on WhatsApp in real time and pair their reply back to the customer. The first-choice path for unknowns; knowledge_gap_capture is the silent fallback.
---

# Ask the owner — live

**When to use:** A customer asks a factual question about the business (policy, amenity, price, an unusual hours exception) and `workspace/knowledge.md` does NOT have the answer. Examples:

- "Do you have valet parking?"
- "Is there a kids' menu?"
- "Do you accept Amex?"
- "Are dogs allowed on the patio?"
- "Do you ship to Barbuda?"

This is the FIRST option when you hit a knowledge gap. `knowledge_gap_capture` is the silent fallback for when the owner is unreachable or the question doesn't deserve a live ping (e.g. esoteric or third+ time the same question).

## Procedure

1. **Don't guess.** Reply to the customer with a brief honest holding message:
   > "Give me a moment, I'll check with {Owner first name}."
2. **Append the marker** at the end of your reply (on its own line, between square brackets):
   ```
   [ask_owner: Do you have valet parking?]
   ```
   - Restate the customer's question concisely (≤300 chars), in third-person from your perspective: "Do you have valet?" not "I'd like to know if there's valet".
   - Strip emojis, formatting, line breaks. Plain prose only.
3. **Stop.** Do NOT promise a specific reply time ("I'll get back to you in 5 min"). Do NOT log a gap separately — the system handles that. Do NOT keep generating fallback content after the marker.
4. **Stay responsive on different topics.** While waiting, if the customer asks a SEPARATE question Rex CAN answer (book a table, read the menu), answer that normally. Don't repeat the wait message for unrelated turns.

## What happens next (system-handled, you don't write code for this)

- The marker is parsed and stripped from the reply before delivery.
- A row is created in `owner_ask_in_flight` (DB).
- The owner gets a WhatsApp message: *"Marie just asked: 'Do you have valet?' — Reply with the answer or type 'skip'."*
- When the owner WAs back, their answer is:
  - written to `workspace/knowledge.md` under an auto-derived topic
  - sent to the customer verbatim
  - the gap row in `knowledge-gaps.md` (if any) is marked taught
- If the owner says "skip" or doesn't reply within 24 h, the row expires; `knowledge_gap_capture` still has the question logged for the dashboard.

## Marker grammar

```
[ask_owner: <one-line question, no quotes, no brackets, no markers>]
```

Bad examples:
- `[ask_owner:]` — empty body, will be dropped silently
- `[ask_owner: "Do you have valet?"]` — outer quotes confuse the regex
- `[ask_owner: Do you have valet?\n\nAnything else?]` — newlines disallowed
- Two markers in one reply for the same topic

## Once per conversation, per topic

If you've already emitted `[ask_owner: ...]` for a question on this turn or a recent prior turn, do NOT emit it again. The system is already pinging the owner; a second ping is noise. Either change the subject if the customer chats further, or wait silently if they don't.

## What NOT to do

- **Don't fake-answer** a factual question with "we have many options" or a vague non-answer when you don't know — the customer feels stalled. Be honest.
- **Don't promise a window** ("back in 2 minutes"). The owner is a human; they answer when they answer.
- **Don't emit the marker for opinion or chitchat** ("Is the chef nice?", "What do you think of XYZ?") — only for factual business operational questions.
- **Don't emit the marker if the owner_phone is the same as the customer phone** (the owner is testing you) — fall through to a normal answer or knowledge_gap_capture instead.
