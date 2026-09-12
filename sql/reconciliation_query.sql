-- =============================================================================
-- Comm-Log Send Reconciliation
-- Merchant 501, Diwali campaigns, October 2026
-- Finance's reported target_base: 22
-- =============================================================================

-- See Query 2 below for the version that remains correct even if a future dataset has a customer delivered twice within one retry chain.
-- =============================================================================
-- QUERY 1 — Row-count / operational
-- Filters to eligible campaigns + delivered sends, counts rows directly.
-- Correct on THIS dataset (22) because no customer was delivered twice within
-- the same retry chain here — see README Section 1/3 for why that matters.
-- =============================================================================
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


-- =============================================================================
-- QUERY 2 — Retry-chain-aware (recommended)
-- Walks campaign.parent_id to group every campaign under its retry chain's
-- root. Within a chain (2+ campaigns), dedupes by customer. For a standalone
-- campaign (a chain of exactly 1), every delivered send counts individually,
-- since it has no retry relationship to collapse against.
-- =============================================================================
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
    JOIN eligible e ON e.campaign_id = l.communication_id
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