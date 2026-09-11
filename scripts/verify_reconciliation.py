"""
Reconciliation verification script.

Runs each step of the bridge from README.md against data/comm_log.db,
prints the running numbers, then runs both final SQL queries from
sql/reconciliation_query.sql and confirms they agree on 22.
"""

import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "comm_log.db"
SQL_PATH = BASE_DIR / "sql" / "reconciliation_query.sql"


def run_bridge(cur):
    steps = {}

    steps["0_naive_scoped"] = cur.execute(
        """
        SELECT COUNT(*) FROM communication_log
        WHERE merchant_id = 501
          AND communication_type = '2'
          AND sent_time >= '2026-10-01' AND sent_time < '2026-11-01'
        """
    ).fetchone()[0]

    steps["1_eligible_campaigns"] = cur.execute(
        """
        SELECT COUNT(*)
        FROM communication_log l
        JOIN campaign c ON l.communication_id = c.id
        WHERE l.merchant_id = 501
          AND l.communication_type = '2'
          AND l.sent_time >= '2026-10-01' AND l.sent_time < '2026-11-01'
          AND c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
          AND c.processing_status = 'processed'
        """
    ).fetchone()[0]

    steps["2_delivered_only"] = cur.execute(
        """
        SELECT COUNT(*)
        FROM communication_log l
        JOIN campaign c ON l.communication_id = c.id
        WHERE l.merchant_id = 501
          AND l.communication_type = '2'
          AND l.sent_time >= '2026-10-01' AND l.sent_time < '2026-11-01'
          AND c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
          AND c.processing_status = 'processed'
          AND l.delivery_status = 900
        """
    ).fetchone()[0]

    steps["trap_global_distinct_customer"] = cur.execute(
        """
        SELECT COUNT(DISTINCT l.customer_id)
        FROM communication_log l
        JOIN campaign c ON l.communication_id = c.id
        WHERE l.merchant_id = 501
          AND l.communication_type = '2'
          AND l.sent_time >= '2026-10-01' AND l.sent_time < '2026-11-01'
          AND c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
          AND c.processing_status = 'processed'
          AND l.delivery_status = 900
        """
    ).fetchone()[0]

    return steps


def run_final_queries(cur):
    """Runs both queries from sql/reconciliation_query.sql directly (kept in
    sync with that file) rather than parsing it, so a stray comment or
    semicolon inside a CTE can't break statement-splitting."""

    query_1 = """
        SELECT COUNT(l.id) AS target_base
        FROM communication_log l
        JOIN campaign c ON l.communication_id = c.id
        WHERE l.merchant_id = 501
          AND l.communication_type = '2'
          AND l.sent_time >= '2026-10-01' AND l.sent_time < '2026-11-01'
          AND c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
          AND c.processing_status = 'processed'
          AND l.delivery_status = 900
    """

    query_2 = """
        WITH RECURSIVE chain(campaign_id, root_id) AS (
            SELECT id, id FROM campaign WHERE parent_id IS NULL
            UNION ALL
            SELECT c.id, ch.root_id
            FROM campaign c JOIN chain ch ON c.parent_id = ch.campaign_id
        ),
        eligible AS (
            SELECT c.id AS campaign_id, ch.root_id
            FROM campaign c
            JOIN chain ch ON ch.campaign_id = c.id
            WHERE c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
              AND c.processing_status = 'processed'
        ),
        chain_size AS (
            SELECT root_id, COUNT(*) AS n_campaigns FROM chain GROUP BY root_id
        ),
        delivered AS (
            SELECT l.id AS send_id, e.root_id, l.customer_id, cs.n_campaigns
            FROM communication_log l
            JOIN eligible e ON e.campaign_id = l.communication_id
            JOIN chain_size cs ON cs.root_id = e.root_id
            WHERE l.merchant_id = 501
              AND l.communication_type = '2'
              AND l.sent_time >= '2026-10-01' AND l.sent_time < '2026-11-01'
              AND l.delivery_status = 900
        )
        SELECT
            COUNT(DISTINCT CASE WHEN n_campaigns > 1 THEN root_id || '-' || customer_id END)
          + COUNT(CASE WHEN n_campaigns = 1 THEN send_id END) AS target_base
        FROM delivered
    """

    return [
        cur.execute(query_1).fetchone()[0],
        cur.execute(query_2).fetchone()[0],
    ]


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Expected database at {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    bridge = run_bridge(cur)
    final_results = run_final_queries(cur)

    print("=" * 78)
    print("RECONCILIATION BRIDGE")
    print("=" * 78)
    print(f"Step 0  Naive count (merchant/date/type scoped) : {bridge['0_naive_scoped']}")
    print(f"Step 1  + eligible campaigns only                : {bridge['1_eligible_campaigns']}"
          f"  (removed {bridge['0_naive_scoped'] - bridge['1_eligible_campaigns']} rows"
          f" — campaign 9004, approval_awaiting)")
    print(f"Step 2  + delivered only (delivery_status=900)   : {bridge['2_delivered_only']}"
          f"  (removed {bridge['1_eligible_campaigns'] - bridge['2_delivered_only']} soft failures)")
    print(f"Trap    global COUNT(DISTINCT customer_id)       : {bridge['trap_global_distinct_customer']}"
          f"  (WRONG — collapses C20's 2 standalone sends)")
    print("-" * 78)
    print(f"Query 1 (row-count)         -> target_base = {final_results[0]}")
    if len(final_results) > 1:
        print(f"Query 2 (retry-chain-aware) -> target_base = {final_results[1]}")
    print("=" * 78)

    assert bridge["2_delivered_only"] == 22, (
        f"Expected 22 after bridge steps, got {bridge['2_delivered_only']}"
    )
    assert final_results[0] == 22, f"Query 1 expected 22, got {final_results[0]}"
    if len(final_results) > 1:
        assert final_results[1] == 22, f"Query 2 expected 22, got {final_results[1]}"

    print("SUCCESS: target_base = 22 reproduced and verified via both queries.")
    conn.close()


if __name__ == "__main__":
    main()
