# Comm-Log Send Reconciliation — Merchant 501, Diwali Campaigns, Oct 2026

Finance reported a `target_base` of **22** for merchant 501, Diwali campaigns in October 2026.
I needed to reproduce this number from the raw data and explain the adjustments.

## What Counts as a Qualifying Send?

Based strictly on the provided README, schema, data dictionary, and actual data, a send qualifies for `target_base` if and only if:

- **Merchant eligibility**: The communication belongs to merchant 501 (`merchant_id = 501`).
- **Diwali campaign eligibility**: The communication is of type 2 (`communication_type = '2'`), which in this dataset corresponds to Diwali campaigns. (No explicit campaign-type column exists; this is the available proxy.)
- **October 2026 date scope**: The communication was sent in October 2026 (`sent_time >= '2026-10-01' AND sent_time < '2026-11-01'`).
- **Campaign status / approval state**: The parent campaign must be in a finalized state (`creation_status IN ('approved', 'aborted', 'resumed', 'stopped')`) and processed (`processing_status = 'processed'`). Sends from campaigns awaiting approval (`approval_awaiting`) are not reportable per the data dictionary.
- **Communication-log records**: Each row in `communication_log` represents a send attempt.
- **Delivery status**: Only successful deliveries count (`delivery_status = 900`). Failed attempts (`delivery_status = 1100`) are excluded, even if later retried and delivered.
- **Failed sends**: A failed attempt is not a qualifying send; only the eventual successful delivery in a retry chain may count (subject to chain deduplication rules below).
- **Retry / parent-child communication chains**: Sends can be retried, forming a parent-child chain via `campaign.parent_id`. Within a chain, multiple attempts for the same customer represent a single underlying communication opportunity. Therefore, we deduplicate by customer *within each chain* to avoid overcounting retries.
- **Multiple legitimate sends to the same customer**: 
  - Within a single retry chain, only one successful send per customer counts (after deduplication).
  - For standalone campaigns (no retries), each delivered send counts separately, even if the same customer receives multiple sends in different standalone campaigns. Example: Customer C20 received two standalone sends under campaign 9101 (Oct 10 and Oct 20); both are real, separate sends.
- **Why `COUNT(DISTINCT customer_id)` is not automatically correct**: A global distinct-customer count incorrectly merges sends from different chains or standalone campaigns for the same customer. The correct rule is to deduplicate only within a retry chain, not across the whole merchant. In this dataset, a global distinct count yields 21 (incorrect) because it merges C20's two standalone sends.

This definition directly informs the SQL logic: filters for merchant, date, communication type, campaign status, processing status, and delivery status; then the recursive CTE groups campaigns by retry chain; finally, we count one send per distinct customer per chain (for chains with ≥2 campaigns) and every send individually for standalone chains (single-campaign chains).

### Why a recursive CTE?
Communication attempts can form parent-child retry chains. The recursive CTE traverses these chains so that related attempts can be evaluated together rather than treating every log row as an independent send. A simple `COUNT(*)` can be misleading when retry/parent-child communication records exist because it would count every retry attempt separately, overcounting the true number of underlying communication opportunities. The chain-aware query correctly collapses retries within a chain while preserving distinct sends from standalone campaigns.

## 1. Reconciliation bridge

I started with the most obvious query and then applied adjustments based on the data dictionary and business rules.

| Step | Description | Result | Reason |
|------|-------------|--------|--------|
| 0 | Naive `COUNT(*)` on `communication_log`, scoped to merchant 501 / Oct 2026 / `communication_type='2'` | **30** | Starting point — all rows in the log already belong to merchant 501 and are Diwali-themed (as seen in the campaign data), so no extra filters were needed for this baseline. |
| 1 | Join to `campaign` and keep only finalized campaigns (`creation_status` in `approved`, `aborted`, `resumed`, `stopped`) | **26** | Campaign 9004 (`Diwali Cart Recovery - Retry C (pending)`) has `creation_status = approval_awaiting`. Although its messages were delivered, the data dictionary states that sends from campaigns awaiting approval are not reportable. Dropped 4 rows (one per customer C11–C14). |
| 2 | Keep only `delivery_status = 900` (successful delivery) | **22** | Four rows had `delivery_status = 1100` (soft failures): C2 and C3 in campaign 9001, C3 in 9002, and D1 in 9201. Each of these was retried and eventually delivered elsewhere in the chain. A failed attempt is not a qualifying send. This brought the count to 22, matching Finance's number. |

### Why not `COUNT(DISTINCT customer_id)`?

At Step 2, it might seem reasonable to deduplicate by customer globally. Doing so gives **21**, which is incorrect.

The data dictionary defines `target_base` per **underlying communication** (a campaign and everything chained via `parent_id`), not per customer globally.

- Within a **retry chain** (e.g., 9001 → 9002 → 9003), the same customer retried across chain links counts as one underlying communication — deduplicate by customer *within that chain*.
- A **standalone campaign** with no retries (e.g., 9101, "Diwali Flash Sale") has no chain to deduplicate against — every delivered send is its own event, even if it's the same customer twice. Customer C20 received two standalone sends under campaign 9101 (Oct 10 and Oct 20); both are real, separate sends.

A global distinct-customer count incorrectly merges C20's two standalone sends into one, undercounting by 1 (→ 21). The correct rule is "deduplicate within a chain, not across the whole merchant."

### Note on why Step 2's row count (22) matches the chain-aware answer here

In this dataset, no customer was delivered more than once within the same retry chain — every retried customer failed until exactly one successful delivery. Therefore, `COUNT(rows)` and `COUNT(DISTINCT customer per chain)` happen to agree. This is a property of this specific dataset, not a guarantee. The chain-aware query in `sql/reconciliation_query.sql` implements the correct rule directly and remains valid even if a future dataset has a customer delivered twice within one retry chain.

## 2. SQL

Two queries in `sql/reconciliation_query.sql` both return **22** against `data/comm_log.db`:

- **Query 1 (row-count / operational)**: Filters to eligible campaigns and delivered sends, then counts rows. It is correct on this dataset because of the coincidence noted above.
- **Query 2 (retry-chain-aware)**: Uses a recursive CTE to walk `parent_id` chains, groups campaigns under their chain's root, then deduplicates customers within each chain while counting every send individually for standalone (single-campaign) chains. This directly encodes Finance's actual rule.

Run either with:
```bash
sqlite3 data/comm_log.db < sql/reconciliation_query.sql
```
or run the verification script to see the full bridge:
```bash
python scripts/verify_reconciliation.py
```

### Why a recursive CTE? (Expanded)
As noted in the qualifying sends section, communication attempts can form parent-child retry chains. The recursive CTE is essential to traverse these chains so that related attempts are evaluated together. Without it, each log row would be treated as an independent send, leading to overcount when retries exist. The CTE groups campaigns by their chain's root, enabling within-chain deduplication of customers while preserving counts for standalone campaigns.

## 3. What surprised me

- **The Diwali campaign filter was redundant in this dataset**: Every campaign for merchant 501 in the data is Diwali-themed, so the "Diwali campaigns" clause didn't actually filter anything out. I called this out explicitly because a real-world query would need it.
- **The send pipeline can run ahead of approval**: Campaign 9004 had fully delivered messages despite never clearing approval (`approval_awaiting`). This shows a real operational gap between "message sent" and "message reportable" — the log alone can't tell you a send is disqualified; you need the campaign's lifecycle state.
- **The row-count and chain-aware queries agree only by luck**: I initially thought a plain filtered `COUNT(*)` was sufficient after excluding `approval_awaiting` and `1100` failures, since it landed on 22. But it agrees with the theoretically correct chain-aware count only because no chain in this dataset had a customer delivered twice within it. If that ever happened, the two queries would diverge, and only the chain-aware one would remain correct — so I kept both in the final SQL.
- **`processing_status` never excludes anything here**: Every row is `processed`. I kept the filter in the query (the data dictionary says it's part of the eligibility rule), but it doesn't affect the bridge numbers for this dataset.

## Repo structure

```
.
├── README.md
├── data/
│   ├── campaign.csv
│   ├── comm_log.db
│   └── communication_log.csv
├── sql/
│   └── reconciliation_query.sql
└── scripts/
    └── verify_reconciliation.py
```