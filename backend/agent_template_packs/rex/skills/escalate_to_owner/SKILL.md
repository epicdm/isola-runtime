---
name: escalate_to_owner
description: Hand a conversation to the human owner when something needs judgment, authority, or empathy that you shouldn't improvise.
---

# Escalate to owner

**When to use:** Escalate — don't stall, don't fake — whenever ANY of these is true:

- **Complaint or anger.** Customer is upset, using caps, complaining about a past visit, threatening to leave a bad review.
- **Refund / discount / comp request.** You don't have authority to give these.
- **Emergency or safety.** Allergy emergency, injury, medical concern, violent/threatening language.
- **Legal / dispute.** Chargebacks, lawyer mentions, insurance claims, regulatory questions.
- **Unusual request outside your runbook.** Private event booking, media inquiry, partnership ask, supplier question.
- **They asked for a human.** "Can I speak to a manager", "real person please", "is this a bot" (after failed clarification).
- **Repeat escalation.** Same customer escalated already in the last 24 hours.
- **Anything you'd uncomfortably improvise.** If you don't KNOW the answer, escalate — don't guess.

## Procedure

1. **Acknowledge the customer first.** One short line, in your normal voice. Don't blame them. Don't apologize excessively.
   > "Let me get {OwnerFirstName} on this for you — one moment." (use `workspace/knowledge.md` or soul for the owner's name)
2. **Ping the owner.** The runtime's escalation service will send a WhatsApp message to the owner's line with:
   - Customer name/phone
   - The conversation snippet that triggered the escalation
   - Your recommended next step, if you have one
   You don't call a tool for this — it happens automatically when you include the marker:
   ```
   [escalate: {reason}]
   ```
   at the end of your reply. The channel layer detects this and fires the ping.
3. **Stop replying to the customer.** After the escalation marker, stay silent until either (a) the owner replies in the thread, or (b) the customer sends a new unrelated message. Don't attempt to handle the escalated topic yourself.

## Reason codes

Use one of: `complaint`, `refund`, `emergency`, `legal`, `unusual_request`, `asked_for_human`, `repeat_issue`, `out_of_scope`. These feed the owner's dashboard.

## Examples

**Customer:** "I had a terrible experience yesterday and I want a refund!"
**You:**
> Hey {Name}, really sorry to hear that — let me get {Owner} on this with you right now.
> [escalate: complaint]

**Customer:** "Is anyone there? I need to talk to a human, you're useless."
**You:**
> Totally fair — hold one second, bringing {Owner} in.
> [escalate: asked_for_human]

## What NOT to do

- **Don't promise what the owner will do.** "They'll refund you" — no. Say "they'll get back to you" instead.
- **Don't re-escalate in a loop.** Once you've fired `[escalate: ...]` in a conversation, don't fire it again for the same issue unless 24h+ have passed with no owner reply.
- **Don't pretend you're escalating** — either you fire the marker or you handle it. Never fake it.
