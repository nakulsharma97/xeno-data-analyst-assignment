# Comm-Log Send Reconciliation — Merchant 501, Diwali Campaigns, Oct 2026

Finance reported a `target_base` of **22** for merchant 501, Diwali campaigns in October 2026. A naive query of the communication log yields **30** sends. The discrepancy arises from ineligible campaigns, failed deliveries, and retry chain deduplication rules.

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

## Reconciliation Bridge

I started with the most obvious query and then applied adjustments based on the data dictionary and business rules.

| Step | Description | Result | Reason |
|------|-------------|--------|--------|
| 0 | Naive `COUNT(*)` on `communication_log`, scoped to merchant 501 / Oct 2026 / `communication_type='2'` | **30** | Starting point — all rows in the log already belong to merchant 501 and are Diwali-themed (as seen in the campaign data), so no extra filters were needed for this baseline. |
| 1 | Join to `campaign` and keep only finalized campaigns (`creation_status` in `approved`, `aborted`, `resumed`, `stopped`) | **26** | Campaign 9004 (`Diwali Cart Recovery - Retry C (pending)`) has `creation_status = approval_awaiting`. Although its messages were delivered, the data dictionary states that sends from campaigns awaiting approval are not reportable. Dropped 4 rows (one per customer C11–C14). |
| 2 | Keep only `delivery_status = 900` (successful delivery) | **22** | Four rows had `delivery_status = 1100` (soft failures): C2 and C3 in campaign 9001, C3 in 9002, and D1 in 9201. Each of these was retried and eventually delivered elsewhere in the chain. A failed attempt is not a qualifying send. This brought the count to 22, matching Finance's number. |

## Primary Reconciliation Query (SQL Query 1)

-- See Query 2 below for the version that remains correct even if a future dataset has a customer delivered twice within one retry chain.
SELECT
    COUNT(l.id) AS target_base
FROM communication_log l
JOIN campaign c
    ON l.communication_id = c.id
WHERE l.merchant_id = 501
  AND l.communication_type = '2'
  AND l.sent_time >= '2026-10-01'
  AND l.sent_time <  '2026-11-01'
  AND c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
  AND c.processing_status = 'processed'
  AND l.delivery_status = 900;

## Retry Chain Investigation

- Family A: 9001 -> 9002 -> 9003 ("Diwali Cart Recovery - Wave 1")
- Family B: 9201 -> 9202 ("Diwali Wave 2")
- Ineligible Branch: 9004 ("Diwali Cart Recovery - Retry C (pending)")

## Structural Recursive CTE (Query 2)

WITH RECURSIVE chain(campaign_id, root_id) AS (
    -- anchor: root campaigns (not a retry of anything)
    SELECT id, id
    FROM campaign
    WHERE parent_id IS NULL

    UNION ALL

    -- recursive step: attach each retry to its chain's root
    SELECT c.id, ch.root_id
    FROM campaign c
    JOIN chain ch ON c.parent_id = ch.campaign_id
),
eligible AS (
    SELECT c.id AS campaign_id, ch.root_id
    FROM campaign c
    JOIN chain ch ON ch.campaign_id = c.id
    WHERE c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
      AND c.processing_status = 'processed'
),
chain_size AS (
    -- number of campaigns under each root; 1 = standalone, 2+ = retry chain
    SELECT root_id, COUNT(*) AS n_campaigns
    FROM chain
    GROUP BY root_id
),
delivered AS (
    SELECT
        l.id AS send_id,
        e.root_id,
        l.customer_id,
        cs.n_campaigns
    FROM communication_log l
    JOIN eligible e   ON e.campaign_id = l.communication_id
    JOIN chain_size cs ON cs.root_id = e.root_id
    WHERE l.merchant_id = 501
      AND l.communication_type = '2'
      AND l.sent_time >= '2026-10-01'
      AND l.sent_time <  '2026-11-01'
      AND l.delivery_status = 900
)
SELECT
    -- retry chains (2+ campaigns): one qualifying send per distinct customer per chain
    COUNT(DISTINCT CASE WHEN n_campaigns > 1 THEN root_id || '-' || customer_id END)
    -- standalone campaigns (1 campaign): every delivered send counts on its own
  + COUNT(CASE WHEN n_campaigns = 1 THEN send_id END)
    AS target_base
FROM delivered;

### Why a recursive CTE?
Communication attempts can form parent-child retry chains. The recursive CTE traverses these chains so that related attempts can be evaluated together rather than treating every log row as an independent send. A simple `COUNT(*)` can be misleading when retry/parent-child communication records exist because it would count every retry attempt separately, overcounting the true number of underlying communication opportunities. The chain-aware query correctly collapses retries within a chain while preserving distinct sends from standalone campaigns.

### Note on why Step 2's row count (22) matches the chain-aware answer here
In this dataset, no customer was delivered more than once within the same retry chain — every retried customer failed until exactly one successful delivery. Therefore, `COUNT(rows)` and `COUNT(DISTINCT customer per chain)` happen to agree. This is a property of this specific dataset, not a guarantee. The chain-aware query in `sql/reconciliation_query.sql` implements the correct rule directly and remains valid even if a future dataset has a customer delivered twice within one retry chain.

## Data Surprises & Analytical Insights

> Campaign 9004 had fully delivered messages despite never clearing approval (`approval_awaiting`). This shows a real operational gap between "message sent" and "message reportable" — the log alone can't tell you a send is disqualified; you need the campaign's lifecycle state.

- **The Diwali campaign filter was redundant in this dataset**: Every campaign for merchant 501 in the data is Diwali-themed, so the "Diwali campaigns" clause didn't actually filter anything out. I called this out explicitly because a real-world query would need it.
- **The row-count and chain-aware queries agree only by luck**: I initially thought a plain filtered `COUNT(*)` was sufficient after excluding `approval_awaiting` and `1100` failures, since it landed on 22. But it agrees with the theoretically correct chain-aware count only because no chain in this dataset had a customer delivered twice within it. If that ever happened, the two queries would diverge, and only the chain-aware one would remain correct — so I kept both in the final SQL.
- **`processing_status` never excludes anything here**: Every row is `processed`. I kept the filter in the query (the data dictionary says it's part of the eligibility rule), but it doesn't affect the bridge numbers for this dataset.

## How to Run / Reproduce Results

Run either query directly with SQLite:
```bash
sqlite3 data/comm_log.db < sql/reconciliation_query.sql
```
or run the verification script to see the full bridge:
```bash
python scripts/verify_reconciliation.py
```

## Repository Structure

```
.
├── README.md          # This file
├── data/
│   ├── campaign.csv          # Campaign metadata
│   ├── comm_log.db           # SQLite database
│   └── communication_log.csv # Raw communication log
├── sql/
│   └── reconciliation_query.sql # SQL queries for reconciliation
└── scripts/
    └── verify_reconciliation.py # Verification script
```