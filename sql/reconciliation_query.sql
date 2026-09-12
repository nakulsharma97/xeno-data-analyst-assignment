-- Final reconciliation query for Xeno Data Analyst Assignment
-- Calculates target_base for Merchant 501, Diwali campaigns (type=2), October 2026.
-- Correctly handles retry chains: within a chain, deduplicate by customer; 
-- standalone campaigns count each delivered send separately.
-- This query remains correct even if a customer is delivered more than once
-- within the same retry chain (unlike a simple row-count approach).

WITH RECURSIVE chain(campaign_id, root_id) AS (
    -- Anchor: root campaigns (not a retry of anything)
    SELECT id, id
    FROM campaign
    WHERE parent_id IS NULL

    UNION ALL

    -- Recursive step: attach each retry to its chain's root
    SELECT c.id, ch.root_id
    FROM campaign c
    JOIN chain ch ON c.parent_id = ch.campaign_id
),
eligible AS (
    -- Campaigns that are finalized and processed (per data dictionary)
    SELECT c.id AS campaign_id, ch.root_id
    FROM campaign c
    JOIN chain ch ON ch.campaign_id = c.id
    WHERE c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
      AND c.processing_status = 'processed'
),
chain_size AS (
    -- Number of campaigns under each root; determines if it's a chain (>=2) or standalone (=1)
    SELECT root_id, COUNT(*) AS n_campaigns
    FROM chain
    GROUP BY root_id
),
delivered AS (
    -- Delivered sends that meet all filters: merchant, date, type, eligibility, and delivery success
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
-- Final aggregation:
-- For retry chains (2+ campaigns): count distinct customer per chain (one send per customer per chain)
-- For standalone campaigns (1 campaign): count each send_id (each send is a distinct opportunity)
SELECT
    COUNT(DISTINCT CASE WHEN n_campaigns > 1 THEN root_id || '-' || customer_id END)
  + COUNT(CASE WHEN n_campaigns = 1 THEN send_id END)
    AS target_base
FROM delivered;
