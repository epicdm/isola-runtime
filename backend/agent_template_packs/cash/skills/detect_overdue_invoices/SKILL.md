---
name: detect_overdue_invoices
description: Sweep the ledger for past-due invoices still unpaid or partially paid. Read-only diagnostic.
---

# Detect overdue invoices

**When to use:** Owner asks "what's overdue?" / "show me AR aging" / "any late payers?" Also on scheduled morning sweep, or invoked by Chief during `state_of_business`.

Read-only. No customer messages sent here. Drafting is `draft_chase_message`.

## Procedure

1. **Query `account.move` via MCP** where:
   - `move_type = 'out_invoice'`
   - `payment_state in ['not_paid', 'partial']`
   - `invoice_date_due < today`
   - `state = 'posted'`

2. **Compute aging bucket** per invoice (today − invoice_date_due):
   - 1–30 days = "Recent"
   - 31–60 = "Aging"
   - 61–90 = "Stale"
   - 90+ days = "At risk"

3. **Group by customer** — total residual, invoice count, oldest_due_date, worst_bucket per partner.

4. **Output table** for owner:

```
AR aging — {today}

Customer                  Outstanding   Invoices   Oldest    Bucket
─────────────────────────────────────────────────────────────────
{name}                    ${amount}     N          N days    {bucket}

Total: $X across N invoices.
```

5. **Save snapshot** to `workspace/ar-aging-{date}.md` for owner scrollback.

## What NOT to do

- Don't message customers (read-only skill).
- Don't recommend write-offs (financial_operations is L3, requires owner approval).
- Don't include PHI for clinic vertical — invoice number + amount + days overdue only.
- Don't fabricate when MCP fails — return "Cash couldn't reach the ledger right now."

## Combined with

- After this: chain to `draft_chase_message` per invoice owner asks to chase.
- For Chief's `state_of_business`: return one-line summary, not full table.
- For owner summary without aging breakdown: use `summarize_receivables`.

## Vertical voice

Aging math is universal; tone differences belong in `draft_chase_message` and soul.

- **Restaurant:** small no-show fees may write off aggressively (still L3).
- **Hotel:** group-booking deposits + folio incidentals tracked separately.
- **Clinic:** if balance "in insurance review", Cash skips auto-chase.
- **Retail:** flag past TERMS (NET-15/30), not past invoice date.
- **Service:** retainer cycles span months — bucket math respects the cycle.
