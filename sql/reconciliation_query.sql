-- Reconciliation query for Xeno Data Analyst Assignment (SQLite).
-- target_base for Merchant 501, Diwali campaigns (type='2'), October 2026.
--
-- Rule: one qualifying send per customer per retry chain (2+ campaigns linked
-- by parent_id). Standalone campaigns (1 campaign) count every delivered send
-- individually, including repeat sends to the same customer (e.g. C20 in 9101).
--
-- A naive delivered-row count also gives 22 here because no customer got two
-- deliveries within the same chain, but this query is the correct general case.

WITH RECURSIVE chain(campaign_id, root_id, depth) AS (
    -- Anchor: root campaigns (not a retry of anything)
    SELECT id, id, 0
    FROM campaign
    WHERE parent_id IS NULL

    UNION ALL

    -- Recursive step: attach each retry to its chain's root
    SELECT c.id, ch.root_id, ch.depth + 1
    FROM campaign c
    JOIN chain ch ON c.parent_id = ch.campaign_id
    WHERE ch.depth < 50  -- defensive against malformed cyclic parent_id graphs
),
eligible AS (
    -- Campaigns that are finalized (creation_status approved, aborted, etc.)
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
-- Aggregation:
-- * Chains (>1 campaign): count distinct (root_id, customer_id) pairs — one
--   qualifying send per customer per chain.
-- * Standalones (=1 campaign): count every send_id — repeat sends to the
--   same customer both count (e.g. C20 in 9101).
SELECT
    COUNT(DISTINCT CASE WHEN n_campaigns > 1 THEN root_id || '-' || customer_id END)
  + COUNT(CASE WHEN n_campaigns = 1 THEN send_id END)
    AS target_base
FROM delivered;
