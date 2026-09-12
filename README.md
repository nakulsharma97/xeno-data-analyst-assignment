# Comm-Log Send Reconciliation — Merchant 501, Diwali Campaigns, October 2026

## Project Overview
This project reconciles the reported `target_base` of 22 qualifying sends for merchant 501 during October 2026 Diwali campaigns. A naive count of communication log rows yields 30 sends. The discrepancy arises from ineligible campaigns, failed deliveries, and retry chain deduplication rules. The goal is to apply business rules from the data dictionary to arrive at the correct qualifying send count.

## Business Question
For merchant 501 in October 2026, how many qualifying sends occurred under Diwali campaigns (communication_type = '2'), after applying eligibility filters and correctly handling retry chains?

## Dataset
The project includes two raw CSV files and one SQLite database:

- **`data/campaign.csv`**: Campaign metadata including `id`, `merchant_id`, `parent_id` (for retry chains), `name`, `creation_status`, `processing_status`.
- **`data/communication_log.csv`**: Each row represents a send attempt with fields: `id`, `merchant_id`, `communication_id` (links to campaign), `customer_id`, `communication_type` (2 = Diwali), `delivery_status` (900 = success, 1100 = soft failure), `sent_time`.
- **`data/comm_log.db`**: SQLite database containing the above tables for querying.

### Entity-Relationship Diagram
The following ER diagram illustrates the schema and relationships:

```mermaid
erDiagram
    CAMPAIGN ||..o{ CAMPAIGN : parent-child (retry chains)
    CAMPAIGN ||..o{ COMMUNICATION_LOG : contains
    CAMPAIGN {
        int id PK
        int merchant_id
        int parent_id FK
        varchar name
        varchar creation_status
        varchar processing_status
    }
    COMMUNICATION_LOG {
        int id PK
        int merchant_id
        int communication_id FK
        varchar customer_id
        int communication_type
        int delivery_status
        datetime sent_time
        int credit_used
        varchar channel
        datetime scheduled_time
    }
```

## Investigation Process
We began with the most obvious query and refined it using the data dictionary and observed data:

1. **Naive scoped count**: All rows for merchant 501, communication_type='2', and October 2026 → 30 sends.
2. **Add campaign eligibility**: Exclude campaigns not in a finalized state (`creation_status` not in `approved`, `aborted`, `resumed`, `stopped`) → 26 sends (removed 4 rows from campaign 9004, which was `approval_awaiting`).
3. **Add delivery success**: Keep only `delivery_status = 900` → 22 sends (removed 4 soft failures). This matches Finance's number.
4. **Check for over‑counting**: A global `COUNT(DISTINCT customer_id)` yields 21, which is incorrect because it merges two separate standalone sends to customer C20 (from campaign 9101 on Oct 10 and Oct 20). The correct rule is to deduplicate only within a retry chain, not across the entire merchant.
5. **Validate retry chains**: No customer received more than one successful delivery within the same retry chain in this dataset, so the row‑count and chain‑aware queries agree. However, the chain‑aware query is the correct generalization.

## Reconciliation Bridge
The table below shows each step of the reconciliation, with values derived directly from the data.

| Step | Description | Result | Reason |
|------|-------------|--------|--------|
| 0 | Naive `COUNT(*)` on `communication_log`, scoped to merchant 501 / Oct 2026 / `communication_type='2'` | 30 | The baseline is scoped to merchant 501, October 2026, and communication type `2`. |
| 1 | Join to `campaign` and keep only finalized campaigns (`creation_status` in `approved`, `aborted`, `resumed`, `stopped`) | 26 | Campaign 9004 (`Diwali Cart Recovery - Retry C (pending)`) has `creation_status = approval_awaiting`. Although its messages were delivered, the data dictionary states that sends from campaigns awaiting approval are not reportable. Dropped 4 rows (one per customer C11–C14). |
| 2 | Keep only `delivery_status = 900` (successful delivery) | 22 | Four rows had `delivery_status = 1100` (soft failures): C2 and C3 in campaign 9001, C3 in 9002, and D1 in 9201. Each of these was retried and eventually delivered elsewhere in the chain. A failed attempt is not a qualifying send. This brought the count to 22, matching Finance's number. |
| Trap | Global `COUNT(DISTINCT customer_id)` (incorrect) | 21 | Incorrectly merges C20's two standalone sends under campaign 9101 (Oct 10 and Oct 20). The correct rule is to deduplicate only within a retry chain, not across the whole merchant. |

## Final SQL
The final, correct query is located at `sql/reconciliation_query.sql`. It uses a recursive CTE to traverse retry chains, then:
- For chains with 2+ campaigns (a retry chain): counts one qualifying send per distinct customer per chain.
- For chains with exactly 1 campaign (a standalone send): counts every delivered send individually.
This approach correctly handles duplicate customers across different chains and avoids over‑counting retries within a chain.

**Key features:**
- No hardcoding of 22; the result is derived from the data.
- Preserves correctness even if a future dataset has a customer delivered twice within the same retry chain.
- Includes comments explaining the business logic at each stage.

## How to Run
Run the final query directly with SQLite:
```bash
sqlite3 data/comm_log.db < sql/reconciliation_query.sql
```
or execute the verification script to see the full bridge:
```bash
python scripts/verify_reconciliation.py
```

## Project Structure
```
.
├── README.md          # This file
├── .gitignore         # Git ignore rules (adjusted to track deliverable DB)
│
├── data/
│   ├── campaign.csv          # Campaign metadata
│   ├── communication_log.csv # Raw communication log
│   └── comm_log.db           # SQLite database (queried directly)
│
├── sql/
│   └── reconciliation_query.sql # Correct, chain‑aware query that calculates target_base
│
└── scripts/
    └── verify_reconciliation.py # Verification script that prints the bridge and confirms the query returns 22
```
