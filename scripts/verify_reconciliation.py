#!/usr/bin/env python3
"""
Reconciliation verification for the Xeno Data Analyst assignment.

Two independent code paths compute the same bridge values:
  - Pure Python loops over campaign.csv / communication_log.csv
  - SQL queries against data/comm_log.db
Every bridge step and per-chain count is cross-checked between the two.
A mismatch fails even if both disagree with the documented constants.

The SQL and Python paths are independently implemented, but they encode the
same business-rule assumptions (same eligible-status list, same month scope,
same delivery-status definition, same standalone-vs-chain counting rule). If
a business-rule assumption itself is wrong, both paths will agree and still be
wrong -- the cross-check catches implementation bugs, not business-logic
misinterpretations. That has to be validated separately against the assignment's
intended definition, not by this script.

Sections:
  1. Data-quality checks (integrity + status values)
  2. Reconciliation bridge (SQL vs Python cross-check)
  3. Per-chain breakdown (SQL vs Python cross-check)
  4. Final SQL file (executes reconciliation_query.sql, confirms 22)
  5. Adversarial test (insert duplicate send, prove chain dedup holds)

Exit code 0 = all passed, 1 = at least one failure.
Standard library only.
"""

import csv
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "comm_log.db"
SQL_PATH = BASE_DIR / "sql" / "reconciliation_query.sql"
CAMPAIGN_CSV = BASE_DIR / "data" / "campaign.csv"
COMM_LOG_CSV = BASE_DIR / "data" / "communication_log.csv"

# Assignment scope.
MERCHANT_ID = 501
COMM_TYPE = "2"  # communication_type is TEXT in the schema; '2' = Diwali
MONTH_START, MONTH_END = "2026-10-01", "2026-11-01"
DELIVERED = 900  # delivery_status 900 = delivered, 1100 = soft failure

# Documented expectations (for human readers, not used for assertions).
# All pass/fail logic is driven by the SQL-vs-Python cross-checks below.
# DOCUMENTED_FINAL = 22
# DOCUMENTED_BRIDGE = {"naive": 30, "eligible": 26, "delivered": 22, "trap": 21}
# DOCUMENTED_CHAINS = {9001: 10, 9101: 7, 9201: 5}

SCOPE = (
    f"l.merchant_id = {MERCHANT_ID} "
    f"AND l.communication_type = '{COMM_TYPE}' "
    f"AND l.sent_time >= '{MONTH_START}' AND l.sent_time < '{MONTH_END}'"
)

# Retry-chain CTE stack shared by the audit queries below.
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


# --------------------------------------------------------------------------- #
#  Pure-Python bridge computation (from CSVs, no SQL)                         #
# --------------------------------------------------------------------------- #

def load_csv_data():
    """Read campaign.csv and communication_log.csv into plain dicts/lists."""
    campaigns = {}
    with open(CAMPAIGN_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            campaigns[int(r["id"])] = {
                "id": int(r["id"]),
                "parent_id": int(r["parent_id"]) if r["parent_id"] else None,
                "creation_status": r["creation_status"],
                "processing_status": r["processing_status"],
            }

    comm_log = []
    with open(COMM_LOG_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            comm_log.append({
                "id": int(r["id"]),
                "merchant_id": r["merchant_id"],
                "communication_id": int(r["communication_id"]),
                "customer_id": r["customer_id"],
                "communication_type": r["communication_type"],
                "delivery_status": int(r["delivery_status"]),
                "sent_time": r["sent_time"],
            })
    return campaigns, comm_log


def build_chains(campaigns):
    """Walk parent_id links to assign every campaign to its chain root.

    Returns (root_of, chain_size) where:
      root_of[campaign_id]  = id of the chain's root campaign (its own id)
      chain_size[root_id]   = number of campaigns in that chain
    """
    root_of = {}
    # Seed: root campaigns (parent_id is None) are their own root.
    for cid, c in campaigns.items():
        if c["parent_id"] is None:
            root_of[cid] = cid
    for cid in campaigns:
        if cid in root_of:
            continue
        path = []
        node = cid
        while node not in root_of:
            path.append(node)
            node = campaigns[node]["parent_id"]
        root = root_of[node]
        for c in path:
            root_of[c] = root

    chain_size = {}
    for cid, root in root_of.items():
        chain_size[root] = chain_size.get(root, 0) + 1
    return root_of, chain_size


def compute_bridge_from_csv(campaigns, comm_log, root_of, chain_size):
    """Derive every bridge value in pure Python loops. Returns a dict."""
    eligible_ids = set()
    for cid, c in campaigns.items():
        if (c["creation_status"] in ("approved", "aborted", "resumed", "stopped")
                and c["processing_status"] == "processed"):
            eligible_ids.add(cid)

    # Step 1: naive scoped count — all rows matching merchant / type / date.
    in_scope = []
    for row in comm_log:
        if (row["merchant_id"] == str(MERCHANT_ID)
                and row["communication_type"] == COMM_TYPE
                and row["sent_time"] >= MONTH_START
                and row["sent_time"] < MONTH_END):
            in_scope.append(row)
    naive = len(in_scope)

    # Step 2: eligible campaigns only.
    eligible = [r for r in in_scope if r["communication_id"] in eligible_ids]

    # Step 3: delivered only.
    delivered = [r for r in eligible if r["delivery_status"] == DELIVERED]
    delivered_count = len(delivered)

    # Trap: global distinct-customer count (wrong for this assignment).
    trap = len({r["customer_id"] for r in delivered})

    # Per-chain breakdown using the same formula as the SQL query:
    #   chains (>1 campaign):  distinct (root_id, customer_id) pairs
    #   standalones (=1 campaign):  every send_id counts
    chains = {}
    for r in delivered:
        cid = r["communication_id"]
        root = root_of[cid]
        nc = chain_size[root]
        key = root if root is not None else cid
        chains.setdefault(key, {"n_campaigns": nc, "key_set": set(), "row_count": 0})
        chains[key]["row_count"] += 1
        if nc > 1:
            chains[key]["key_set"].add((root, r["customer_id"]))

    chain_totals = {}
    for root, info in chains.items():
        if info["n_campaigns"] > 1:
            chain_totals[root] = len(info["key_set"])
        else:
            chain_totals[root] = info["row_count"]

    return {
        "naive": naive,
        "eligible": len(eligible),
        "delivered": delivered_count,
        "trap": trap,
        "chain_totals": chain_totals,
        "final": sum(chain_totals.values()),
    }


# --------------------------------------------------------------------------- #
#  SQL helpers                                                                #
# --------------------------------------------------------------------------- #

def header(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def execute_sql_file(conn, path):
    """Execute the actual SQL file. Returns (columns, rows) from the last
    result set."""
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


# --------------------------------------------------------------------------- #
#  Main                                                                       #
# --------------------------------------------------------------------------- #

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

    # ---- compute expected values from CSVs (no SQL) ----
    csv_campaigns, csv_log = load_csv_data()
    csv_root_of, csv_chain_size = build_chains(csv_campaigns)
    csv_bridge = compute_bridge_from_csv(
        csv_campaigns, csv_log, csv_root_of, csv_chain_size
    )

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
    header("3. RECONCILIATION BRIDGE (SQL vs Python cross-check)")
    print(f"  Python-derived values: naive={csv_bridge['naive']}, "
          f"eligible={csv_bridge['eligible']}, delivered={csv_bridge['delivered']}, "
          f"trap={csv_bridge['trap']}")
    print()

    # Step 1: naive scoped count.
    sql_naive = cur.execute(
        f"SELECT COUNT(*) FROM communication_log l WHERE {SCOPE}"
    ).fetchone()[0]
    check(
        "Step 1  naive scoped count",
        sql_naive == csv_bridge["naive"],
        f"SQL={sql_naive}, Python={csv_bridge['naive']}",
    )

    # Step 2: eligible campaigns only.
    sql_eligible = cur.execute(
        f"""SELECT COUNT(*)
            FROM communication_log l
            JOIN campaign c ON l.communication_id = c.id
            WHERE {SCOPE}
              AND c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
              AND c.processing_status = 'processed'"""
    ).fetchone()[0]
    check(
        "Step 2  + reportable campaigns only",
        sql_eligible == csv_bridge["eligible"],
        f"SQL={sql_eligible}, Python={csv_bridge['eligible']}",
    )

    # Step 3: delivered only.
    sql_delivered = cur.execute(
        f"""SELECT COUNT(*)
            FROM communication_log l
            JOIN campaign c ON l.communication_id = c.id
            WHERE {SCOPE}
              AND c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
              AND c.processing_status = 'processed'
              AND l.delivery_status = {DELIVERED}"""
    ).fetchone()[0]
    check(
        "Step 3  + delivered only",
        sql_delivered == csv_bridge["delivered"],
        f"SQL={sql_delivered}, Python={csv_bridge['delivered']}",
    )

    # Trap: global distinct customer count.
    sql_trap = cur.execute(
        f"""SELECT COUNT(DISTINCT l.customer_id)
            FROM communication_log l
            JOIN campaign c ON l.communication_id = c.id
            WHERE {SCOPE}
              AND c.creation_status IN ('approved', 'aborted', 'resumed', 'stopped')
              AND c.processing_status = 'processed'
              AND l.delivery_status = {DELIVERED}"""
    ).fetchone()[0]
    check(
        "Trap    global DISTINCT customer count",
        sql_trap == csv_bridge["trap"],
        f"SQL={sql_trap}, Python={csv_bridge['trap']}",
    )

    # ------------------------------------------------------------------ #
    header("4. PER-CHAIN BREAKDOWN (SQL vs Python cross-check)")
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

    sql_chain_map = dict((r[0], r[2]) for r in chain_rows)
    py_chain_map = csv_bridge["chain_totals"]

    all_chains_ok = True
    all_roots = sorted(set(sql_chain_map) | set(py_chain_map),
                       key=lambda x: (x is None, x or 0))
    for root in all_roots:
        sql_val = sql_chain_map.get(root, 0)
        py_val = py_chain_map.get(root, 0)
        ok = sql_val == py_val
        all_chains_ok = all_chains_ok and ok
        nc = next((r[1] for r in chain_rows if r[0] == root), 0)
        kind = "chain  " if nc > 1 else "single "
        label = f"root {root or '(none)'} {kind}({nc} campaign(s))" if root is None else f"root {root} {kind}({nc} campaign(s))"
        check(
            label,
            ok,
            f"SQL={sql_val}, Python={py_val}",
        )

    check(
        "Chain totals sum = delivered count",
        all_chains_ok and sum(sql_chain_map.values()) == sql_delivered,
        "SQL=" + " + ".join(str(sql_chain_map.get(r, 0))
                            for r in sorted(sql_chain_map))
              + f" = {sum(sql_chain_map.values())}"
              + f" (delivered={sql_delivered})",
    )

    # ------------------------------------------------------------------ #
    header("5. FINAL QUERY (executes sql/reconciliation_query.sql)")
    columns, rows = execute_sql_file(conn, SQL_PATH)
    print(f"  Columns: {columns}")
    if "target_base" not in columns or not rows:
        check("Query returns a target_base column", False, f"rows={rows}")
    else:
        value = rows[0][columns.index("target_base")]
        check(
            "target_base = delivered count",
            value == csv_bridge["delivered"],
            f"SQL file={value}, delivered={csv_bridge['delivered']}",
        )

    # ------------------------------------------------------------------ #
    header("6. ADVERSARIAL TEST: chain dedup holds under mutation")
    print("  Scope: proves chain-level dedup handles a duplicate delivered send")
    print("  within a single chain. Does not cover: duplicate sends within a")
    print("  standalone campaign, customers across different chains, multi-level")
    print("  retry depth beyond current data, cyclic parents, or eligibility edges.")
    print()
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
            "  After inserting a duplicate delivered send for C1 in chain 9001:"
        )
        check(
            "Naive row count over-counts",
            naive_val == csv_bridge["delivered"] + 1,
            f"SQL={naive_val} (expected {csv_bridge['delivered'] + 1})",
        )
        check(
            "Chain-aware query still returns correct count",
            chain_val == csv_bridge["delivered"],
            f"SQL file={chain_val}, original={csv_bridge['delivered']}",
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
        f"  SUCCESS: SQL and Python independently agree — "
        f"bridge {csv_bridge['naive']} -> {csv_bridge['eligible']} -> "
        f"{csv_bridge['delivered']}, target_base = {csv_bridge['delivered']}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
