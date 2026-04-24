---
name: request_review
description: After a successful visit/order, send a review request with the Google Business Profile link — template-based so it works outside the 24h session.
---

# Request a review

**When to use:** After a confirmed positive interaction — completed reservation, delivered order, resolved issue where the customer seemed happy. NEVER send if there was a complaint, escalation, or cancellation.

## Prerequisites

- `workspace/knowledge.md` or `workspace/review-links.md` has the Google Business Profile review URL. Looks like `https://g.page/r/CXYZ123/review`.
- Customer's booking/order is marked completed in `workspace/bookings.md` or `workspace/orders.md`.
- No prior review request sent to this customer in the last 90 days (check `workspace/review-requests.md`).

## Procedure

1. **Check eligibility.** All three prerequisites must be met. If not, skip silently.
2. **Schedule, don't send immediately.** Use `set_trigger` for ~3 hours after the booking end-time. Customers are more likely to leave a review when the experience is still vivid but they're home, not mid-meal.
3. **Use the template.** Fire `post_visit_thanks` with these params:
   - `{{1}}` = customer first name
   - `{{2}}` = review link
4. **Log it.** Append to `workspace/review-requests.md`:
   ```
   - 2026-04-26 · Marie · +17678183742 · requested post-visit · via template
   ```

## Follow-up

If a customer replies positively after the review request ("thanks, I did it!"), send ONE warm thanks message in your normal voice — don't over-engineer. If they mention a problem in their reply, escalate immediately — they were about to leave a bad review; catch it first.

## Batch review requests

During quiet hours (check `workspace/hours.md`), you can proactively scan `bookings.md` for yesterday's completed rows that haven't had a request yet, and schedule batch review requests. Rate-limit: max 10 per day per tenant to avoid being flagged as spam.

## What NOT to do

- **Don't request reviews in free text.** Template only. Meta will throttle you otherwise.
- **Don't send to unhappy customers.** A 1-star review is worse than no review.
- **Don't request twice in 90 days.** Even if the first one went unanswered.
- **Don't mention stars or specific ratings.** "If we earned it" is the right tone. Never "please leave a 5-star review".

## Why this matters (for your own alignment)

Reviews compound: restaurants with 100+ reviews outrank ones with 10, even at the same star count. This skill is literally revenue growth disguised as a polite message. But it only works if the quality is there — fake or coerced reviews get the tenant's GBP suspended, which is catastrophic.
