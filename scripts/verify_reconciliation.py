#!/usr/bin/env python3
"""
Reconciliation verification for the Xeno Data Analyst assignment.

Runs against data/comm_log.db (paths are relative to the repository root,
so the script works from any fresh clone):

  1. Data-quality checks   - integrity violations (hard failures) and
                             unexpected status values (warnings only, since
                             the schema does not constrain these).
  2. Reconciliation bridge - 30 -> 26 -> 22, plus the 21 global-DISTINCT trap
                             and the per-chain intermediate counts.
  3. Final result          - executes the actual sql/reconciliation_query.sql
                             file (no duplicated SQL) and confirms 22.

Exit code 0 = everything passed, 1 = at least one failure.
Standard library only.
"""

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "comm_log.db"
SQL_PATH = BASE_DIR / "sql" / "reconciliation_query.sql"

# Assignment scope (mirrors the brief / README).
MERCHANT_ID = 501
COMM_TYPE = "2"  # communication_type is TEXT in the schema; '2' = Diwali
MONTH_START, MONTH_END = "2026-10-01", "2026-11-01"
DELIVERED = 900  # delivery_status 900 = delivered, 1100 = soft failure

# Expected bridge numbers, verified against the dataset and documented in README.
EXPECTED_BRIDGE = {
    "naive_scoped": 30,
    "eligible_only": 26,
    "delivered_only": 22,
    "trap_global_distinct_customers": 21,
}
EXPECTED_CHAINS = {9001: 10, 9101: 7, 9201: 5}  # root -> qualifying sends
FINAL_TARGET_BASE = 22

SCOPE = (
    f"l.merchant_id = {MERCHANT_ID} "
    f"AND l.communication_type = '{COMM_TYPE}' "
    f"AND l.sent_time >= '{MONTH_START}' AND l.sent_time < '{MONTH_END}'"
)

# Retry-chain CTE stack shared by the audit queries below. A depth guard keeps
# the recursion finite even if malformed data ever contained a parent cycle.
CHAIN_CTES = f"""
WITH RECURSIVE chain(campaign_id, root_id, depth) AS (
    SELECT id, id, 0 FROM campaign WHERE parent_id IS NULL
    UNION ALL
    SELECT c.id, ch.root_id, ch.depth + 1
    FROM campaign c
    JOIN chain ch ON c.parent_id = ch.campaign_id
    WHERE ch.depth < 50
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
    WHERE {SCOPE}
      AND l.delivery_status = {DELIVERED}
)
"""

# Hard integrity checks: each returns violating rows; empty result = pass.
INTEGRITY_CHECKS = [
    (
        "Duplicate campaign IDs",
        "SELECT id, COUNT(*) AS n FROM campaign GROUP BY id HAVING COUNT(*) > 1",
    ),
    (
        "Communication rows referencing a missing campaign (orphan reference)",
        """SELECT l.id AS log_id, l.communication_id
           FROM communication_log l
           LEFT JOIN campaign c ON c.id = l.communication_id
           WHERE c.id IS NULL""",
    ),
    (
        "Campaigns whose parent_id does not exist (invalid parent reference)",
        """SELECT c.id, c.parent_id
           FROM campaign c
           LEFT JOIN campaign p ON p.id = c.parent_id
           WHERE c.parent_id IS NOT NULL AND p.id IS NULL""",
    ),
    (
        "Campaigns that are their own parent",
        "SELECT id FROM campaign WHERE parent_id = id",
    ),
    (
        "Campaigns reachable from more than one root or reached twice "
        "(parent-cycle symptom)",
        CHAIN_CTES
        + """SELECT campaign_id, COUNT(DISTINCT root_id) AS roots, COUNT(*) AS appearances
             FROM chain
             GROUP BY campaign_id
             HAVING COUNT(DISTINCT root_id) <> 1 OR COUNT(*) <> 1""",
    ),
    (
        "Campaigns unreachable from any root (broken parent chain)",
        CHAIN_CTES
        + """SELECT id FROM campaign
             WHERE id NOT IN (SELECT campaign_id FROM chain)""",
    ),
]

# Status-value expectations: values observed in the assignment dataset. The
# schema declares no CHECK constraints, so anything unexpected is a WARNING,
# not a failure (per the README: statuses are documented, not enforced).
STATUS_EXPECTATIONS = [
    (
        "campaign.creation_status",
        "SELECT DISTINCT creation_status FROM campaign",
        {"approved", "approval_awaiting"},
    ),
    (
        "campaign.processing_status",
        "SELECT DISTINCT processing_status FROM campaign",
        {"processed"},
    ),
    (
        "communication_log.delivery_status",
        "SELECT DISTINCT delivery_status FROM communication_log",
        {900, 1100},
    ),
    (
        "communication_log.communication_type",
        "SELECT DISTINCT communication_type FROM communication_log",
        {COMM_TYPE},
    ),
]


def header(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def execute_sql_file(conn, path):
    """Execute the actual SQL file. Handles the normal single-statement case
    directly; if the file ever contains several statements, runs them all and
    returns the last result set."""
    sql = path.read_text(encoding="utf-8")
    try:
        cur = conn.execute(sql)
    except sqlite3.ProgrammingError:
        statement, results = "", []
        for line in sql.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                results.append(conn.execute(statement))
                statement = ""
        cur = results[-1]
    columns = [d[0] for d in cur.description]
    return columns, cur.fetchall()


def main():
    failures = []
    warnings = []

    def check(label, ok, detail=""):
        status = "PASS" if ok else "FAIL"
        line = f"  [{status}] {label}"
        if detail:
            line += f"  ({detail})"
        print(line)
        if not ok:
            failures.append(label)

    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}")
        return 1
    if not SQL_PATH.exists():
        print(f"ERROR: SQL file not found at {SQL_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # ------------------------------------------------------------------ #
    header("1. DATA-QUALITY CHECKS (integrity)")
    for label, query in INTEGRITY_CHECKS:
        rows = cur.execute(query).fetchall()
        check(label, not rows, f"{len(rows)} violating row(s)" if rows else "clean")
        for row in rows:
            print(f"         -> {row}")

    header("2. DATA-QUALITY CHECKS (status values; warnings only)")
    for column, query, expected in STATUS_EXPECTATIONS:
        found = {r[0] for r in cur.execute(query).fetchall()}
        unexpected = found - expected
        check(
            f"{column} values are as documented in README",
            not unexpected,
            f"unexpected: {sorted(unexpected)}" if unexpected else "clean",
        )
        if unexpected:
            warnings.append(column)

    # ------------------------------------------------------------------ #
    header("3. RECONCILIATION BRIDGE")

    naive = cur.execute(
        f"SELECT COUNT(*) FROM communication_log l WHERE {SCOPE}"
    ).fetchone()[0]
    check(
        "Step 1  naive scoped count (merchant / Oct 2026 / type '2')",
        naive == EXPECTED_BRIDGE["naive_scoped"],
        f"got {naive}, expected {EXPECTED_BRIDGE['naive_scoped']}",
    )

    eligible = cur.execute(
        f"""SELECT COUNT(*)
            FROM communication_log l
            JOIN campaign c ON l.communication_id = c.id
            WHERE {SCOPE}
              AND c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
              AND c.processing_status = 'processed'"""
    ).fetchone()[0]
    check(
        "Step 2  + reportable campaigns only (drops 9004, approval_awaiting)",
        eligible == EXPECTED_BRIDGE["eligible_only"],
        f"got {eligible}, expected {EXPECTED_BRIDGE['eligible_only']} "
        f"(-{naive - eligible} rows)",
    )

    delivered = cur.execute(
        f"""SELECT COUNT(*)
            FROM communication_log l
            JOIN campaign c ON l.communication_id = c.id
            WHERE {SCOPE}
              AND c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
              AND c.processing_status = 'processed'
              AND l.delivery_status = {DELIVERED}"""
    ).fetchone()[0]
    check(
        "Step 3  + delivered only (delivery_status = 900)",
        delivered == EXPECTED_BRIDGE["delivered_only"],
        f"got {delivered}, expected {EXPECTED_BRIDGE['delivered_only']} "
        f"(-{eligible - delivered} soft failures)",
    )

    trap = cur.execute(
        f"""SELECT COUNT(DISTINCT l.customer_id)
            FROM communication_log l
            JOIN campaign c ON l.communication_id = c.id
            WHERE {SCOPE}
              AND c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
              AND c.processing_status = 'processed'
              AND l.delivery_status = {DELIVERED}"""
    ).fetchone()[0]
    check(
        "Trap    global COUNT(DISTINCT customer_id) is 21, not 22",
        trap == EXPECTED_BRIDGE["trap_global_distinct_customers"],
        f"got {trap}: collapses C20's two standalone sends in campaign 9101",
    )

    print()
    print("  Per-chain breakdown (root -> qualifying sends):")
    chain_rows = cur.execute(
        CHAIN_CTES
        + """SELECT root_id, n_campaigns,
                    COUNT(DISTINCT CASE WHEN n_campaigns > 1
                                        THEN root_id || '-' || customer_id END)
                  + COUNT(CASE WHEN n_campaigns = 1 THEN send_id END) AS counted
             FROM delivered
             GROUP BY root_id, n_campaigns
             ORDER BY root_id"""
    ).fetchall()
    chain_map = dict((r[0], r[2]) for r in chain_rows)
    all_chain_expected = True
    for root, n_campaigns, counted in chain_rows:
        kind = "chain  " if n_campaigns > 1 else "single "
        expected = EXPECTED_CHAINS.get(root)
        ok = counted == expected
        all_chain_expected = all_chain_expected and ok
        detail = f"{kind} ({n_campaigns} campaign(s)) -> {counted}"
        if not ok:
            detail += f", expected {expected}"
        check(f"root {root}", ok, detail)
    check(
        "Chain counts sum to the final result",
        all_chain_expected and sum(chain_map.values()) == delivered,
        " + ".join(str(v) for v in chain_map.values())
        + f" = {sum(chain_map.values())}",
    )

    # ------------------------------------------------------------------ #
    header("4. FINAL QUERY (executes sql/reconciliation_query.sql)")
    columns, rows = execute_sql_file(conn, SQL_PATH)
    print(f"  Columns: {columns}")
    if "target_base" not in columns or not rows:
        check("Query returns a target_base column", False, f"rows={rows}")
    else:
        value = rows[0][columns.index("target_base")]
        check(
            "target_base equals 22",
            value == FINAL_TARGET_BASE,
            f"got {value}",
        )

    # ------------------------------------------------------------------ #
    header("5. ADVERSARIAL TEST: chain dedup holds under mutation")
    print("  Copying DB to temp file and inserting a duplicate delivered send...")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        shutil.copy2(DB_PATH, tmp.name)
        tmp_conn = sqlite3.connect(tmp.name)
        tmp_conn.execute(
            "INSERT INTO communication_log"
            " (id, merchant_id, communication_id, customer_id, communication_type,"
            "  delivery_status, sent_time, scheduled_time, credit_used, channel)"
            " VALUES"
            " (100, 501, 9002, 'C1', '2', 900, '2026-10-04 12:00:00',"
            "  '2026-10-04 12:00:00', 1, 'sms')"
        )
        tmp_conn.commit()

        chain_val = execute_sql_file(tmp_conn, SQL_PATH)[1][0][0]
        naive_val = tmp_conn.execute(
            f"""SELECT COUNT(*)
               FROM communication_log l
               JOIN campaign c ON l.communication_id = c.id
               WHERE {SCOPE}
                 AND c.creation_status IN ('approved','aborted','resumed','stopped')
                 AND c.processing_status = 'processed'
                 AND l.delivery_status = {DELIVERED}"""
        ).fetchone()[0]

        print(
            f"  After inserting a duplicate delivered send for C1 in chain 9001:"
        )
        check(
            "Naive row count over-counts (23 != 22)",
            naive_val == 23,
            f"got {naive_val}",
        )
        check(
            "Chain-aware query still returns 22",
            chain_val == 22,
            f"got {chain_val}",
        )
    finally:
        tmp_conn.close()
        Path(tmp.name).unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    conn.close()
    header("RESULT")
    if warnings:
        for w in warnings:
            print(f"  WARNING: unexpected values in {w} (informational only)")
    if failures:
        print(f"  FAILURE: {len(failures)} check(s) failed:")
        for f in failures:
            print(f"    - {f}")
        return 1
    print(
        f"  SUCCESS: data quality clean, bridge "
        f"{naive} -> {eligible} -> {delivered} reproduced, "
        f"target_base = {FINAL_TARGET_BASE} confirmed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
