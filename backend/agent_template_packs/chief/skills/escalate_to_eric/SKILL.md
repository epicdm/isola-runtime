---
name: escalate_to_eric
description: Alert the owner (Eric) when something requires immediate human judgment or action that Chief cannot resolve alone.
---

# Escalate to Eric

**When to use:** Escalate — don't sit on it — whenever ANY of these is true:

- **Agent HALT state.** Any subordinate agent has halted and cannot self-recover.
- **Anomalous spend.** Token spend or API cost is 2x expected baseline in a 24h window.
- **Pending customer escalation.** Rex (or another front-line agent) escalated a customer issue waiting > 2 hours without owner acknowledgment.
- **Data conflict.** Two sources (e.g., ERP vs agent log) return contradictory information on a business-critical field (revenue, invoice status).
- **Out-of-policy action requested.** An agent received an instruction outside its configured autonomy policy and couldn't self-authorize.
- **Security or compliance signal.** Unusual access pattern, authentication failure, or data export that wasn't owner-initiated.
- **You don't know.** If you cannot produce a reliable answer to a time-sensitive question, escalate rather than guess.

## Procedure

1. **Summarize the situation in 3 lines or fewer.** State facts; skip preamble.
2. **Name the trigger.** Use one of: `agent_halt`, `spend_anomaly`, `pending_escalation`, `data_conflict`, `policy_violation`, `security_signal`, `unknown`.
3. **State what you've already done** — queries run, agents checked, data pulled.
4. **Fire the marker:**
   ```
   [ask_owner: {trigger} | {one-line summary}]
   ```
   The runtime delivers this to Eric's priority channel immediately.
5. **Wait for Eric's reply.** Don't continue processing the blocked item until you have direction.

## Examples

**Agent HALT:**
> Rex halted 14 minutes ago — error: WhatsApp token expired. 3 inbound messages are queued unhandled.
> [ask_owner: agent_halt | Rex is down, token expired, 3 messages queued]

**Pending customer escalation:**
> A customer complaint (2h 18m ago) has no owner response. Customer has sent 2 follow-ups.
> [ask_owner: pending_escalation | customer complaint unanswered 2h 18m]

## What NOT to do

- Don't escalate items you can answer with available data.
- Don't fire multiple escalations for the same issue within 1 hour — consolidate.
- Don't add reassuring filler. Eric wants direct signal, not "don't worry but...".
