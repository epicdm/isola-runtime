---
name: summarize_receivables
description: One-glance AR snapshot for the owner. Total receivables, top-N debtors, days-sales-outstanding signal. Read-only.
---

# Summarize receivables

**When to use:** Owner asks "how's AR?" / "what's outstanding?" / "show me receivables" — wants a one-line health check, not a full aging table.

Also: Chief's `state_of_business` calls this for the AR snapshot row.

Read-only. No drafts, no sends.

## Procedure

1. **Query `account.move` via MCP** where:
   - `move_type = 'out_invoice'`
   - `payment_state in ['not_paid', 'partial']`
   - `state = 'posted'`

   Pull `amount_residual`, `invoice_date`, `invoice_date_due`, `partner_id`.

2. **Compute the three numbers** owner wants:
   - **Total outstanding** = sum of amount_residual
   - **Overdue portion** = sum of amount_residual where invoice_date_due < today
   - **Top debtor** = partner with largest amount_residual

3. **Compute a DSO signal** if recent paid invoices are available:
   - Days Sales Outstanding rough = (avg amount_residual / avg daily revenue) — only if revenue context is reachable; else omit.

4. **Output as one block** for owner:

```
AR snapshot — {today}

Outstanding:  ${total}        ({N invoices})
Overdue:      ${overdue}      ({M overdue / N total})
Top debtor:   {partner_name}  (${their_residual})
DSO signal:   {days or "n/a"}

{one-line risk callout if 90+ days bucket has any entries}
```

One number per line. No padding prose. If MCP fails: return "Cash couldn't reach the ledger — try again."

## Combined with

- For Chief's `state_of_business`: return just "Outstanding: $X ({N invoices})" line, not the full block.
- After this, owner often follows up "show me the aging" → chain to `detect_overdue_invoices`.
- If owner says "chase the top debtor" → chain to `draft_chase_message` with their largest invoice.

## What NOT to do

- Don't include payment-method PII (saved cards, bank routing).
- Don't include phone numbers — owner drills down if they want contact.
- Don't recommend write-offs — L3 financial_operations.
- Don't fabricate DSO if revenue context unavailable; omit the line.
- For clinic: never mention visit reasons / diagnosis / patient details. Account number + amount only.

## Vertical voice

Output table is universal. Per-vertical risk callouts differ:

- **Restaurant:** flag catering customers 60+ days late.
- **Hotel:** flag group-booking balance unresolved past departure.
- **Clinic:** flag "in insurance" status (different from overdue).
- **Retail:** flag wholesale customer past NET-30 terms.
- **Service:** flag retainer client missed monthly cycle.
