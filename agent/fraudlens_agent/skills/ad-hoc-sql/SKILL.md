---
name: ad-hoc-sql
description: >-
  Write and run a read-only BigQuery SELECT over the FraudLens tables for
  questions the KPI and segment skills can't answer - filters on a specific
  merchant or amount, distinct counts, maximums, model versions, or custom
  breakdowns. Use only after checking fraud-kpis and risk-segments don't fit.
metadata:
  adk_additional_tools:
    - run_readonly_sql
---

# Ad-hoc SQL

Call `run_readonly_sql(query)` with one GoogleSQL `SELECT` (or `WITH ... SELECT`).
Writes, DDL and multiple statements are rejected. At most 100 rows come back.

## Tables (dataset `fraudlens`; reference them unqualified)

`scored` - one row per scored transaction. Partitioned by `dt`; **always filter on `dt`**.

| column | type | notes |
|---|---|---|
| dt | DATE | batch date (partition) |
| transaction_id | STRING | |
| transaction_amt | FLOAT64 | USD |
| product_cd | STRING | W, C, R, H, S |
| card_type | STRING | credit / debit |
| addr1 | INT64 | billing region code |
| dist1 | FLOAT64 | distance feature, often NULL |
| p_emaildomain | STRING | payer email domain |
| r_emaildomain | STRING | recipient email domain |
| merchant | STRING | merchant name |
| merchant_category | STRING | e.g. grocery, gift_cards, crypto |
| fraud_score | FLOAT64 | model probability 0-1 |
| is_fraud | BOOL | model flag at its tuned threshold |
| model_version | STRING | e.g. xgboost-v3 |
| explanation | STRING | template explanation |
| scored_at | TIMESTAMP | |

`daily_summary` - one row per `dt`: transactions, flagged, fraud_rate, avg_score,
p95_score, flagged_amount, updated_at (all based on `is_fraud`).

High-risk means `fraud_score >= 0.20`.

## Patterns
```sql
-- distinct merchants in a window
SELECT COUNT(DISTINCT merchant) FROM scored WHERE dt BETWEEN '2026-10-07' AND '2026-10-13'

-- one merchant on one day
SELECT COUNT(*) AS n, COUNTIF(fraud_score >= 0.20) AS high_risk
FROM scored WHERE dt = '2026-10-12' AND merchant = 'GiftCard Hub'

-- share of high-risk transactions coming from one payer domain
SELECT SAFE_DIVIDE(COUNTIF(p_emaildomain = 'protonmail.com'), COUNT(*))
FROM scored WHERE dt BETWEEN '2026-09-14' AND '2026-10-13' AND fraud_score >= 0.20
```
Merchant names are exact-match strings; if unsure, first run
`SELECT DISTINCT merchant FROM scored WHERE dt >= '<start>'`.

## Answer format
State the result and the window. Don't paste the SQL unless asked.
