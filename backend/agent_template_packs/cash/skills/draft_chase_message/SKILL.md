---
name: draft_chase_message
description: Compose a polite, on-brand reminder for an overdue invoice. Owner approval required before send.
---

# Draft a chase message

**When to use:** Owner reviews `detect_overdue_invoices` and asks Cash to draft a reminder. Also chains in after morning sweep if Cash is in auto-draft mode.

DRAFTS only. Send is L2 — Cash composes, owner approves, send goes out (via Rex's outbound channel for WhatsApp).

## Procedure

1. **Read invoice context** — number, partner, amount_residual, days overdue, due date. Pull from MCP.

2. **Read `workspace/soul.md`** for tone (warmth, formality, first-name vs last-name).

3. **Choose escalation tier** based on days overdue:
   - **1–14 days:** gentle nudge ("just a friendly reminder")
   - **15–30 days:** firm reminder ("payment was due X days ago")
   - **31–60 days:** direct ask ("please remit by {date}")
   - **60+ days:** flag — pause auto-draft, propose owner phone call

4. **Draft the message** in soul voice. Include:
   - Customer's preferred name (from `res.partner.name`)
   - Invoice number + amount + original due date
   - One concrete payment option (link, bank details, call number)
   - One soft closer (no threats, no late-fee mentions unless owner instructed)

5. **Present to owner for approval** — output as:

```
DRAFT for {partner_name} ({invoice_no} · $X · N days overdue)
─────────────────────────────────────────────────────────────
{drafted message text}
─────────────────────────────────────────────────────────────
Reply "send" to approve, "edit" to revise, or "skip" to pass.
```

6. **On owner approval** — log the action against the invoice (workspace/cash-log.md), then hand off to Rex's outbound for WhatsApp delivery. Cash does NOT send directly from this skill — the L2 boundary is "owner explicitly approved this exact text."

7. **On owner edit** — accept the edit verbatim, present the revised draft for re-approval.

## What NOT to do

- **Never send before owner approval.** Even if confidence is high, even if it's the third reminder. Send is L2 — explicit per-message OK.
- **Never mention legal action / collections agency / credit reporting.** That's L3 escalation territory; route via `escalate_to_owner`.
- **Never add late fees.** Adjustments (positive or negative) are L3 financial_operations.
- **Never include diagnosis / visit details** for clinic vertical.
- **Never copy the message to other recipients** unless owner explicitly says cc.
- **Never use threatening language.** "Please remit" not "you must pay immediately." If customer disputes, hand off to owner.

## Combined with

- After `detect_overdue_invoices`: owner picks which invoices to draft for.
- If customer disputes: escalate via `escalate_to_owner` (or Rex's equivalent). Don't counter-draft.
- If customer replies "paid": acknowledge politely. Do NOT confirm payment in books — recording is L3, needs owner verification.

## Vertical voice

- **Restaurant:** warm + casual — "Hey {Name}! Just a heads-up on the catering invoice from last month..."
- **Hotel:** hospitable + formal — "Mr./Ms. {LastName}, this is a reminder regarding invoice #{N}..."
- **Clinic:** professional + neutral — "This is a balance reminder for account #{N}. Please contact us at..."
- **Retail:** direct + courteous — "Hi {Name}, your invoice #{N} from {date} is past due..."
- **Service:** project-anchored — "Following up on the {project_ref} invoice from {month}..."
