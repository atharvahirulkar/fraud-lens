---
name: risk-segments
description: >-
  Rank merchants, merchant categories, product codes, card types, or payer /
  recipient email domains by fraud risk over a date range. Use for "top
  flagged merchants", "riskiest category", "which email domain…",
  "credit vs debit", or any "by <segment>" breakdown.
metadata:
  adk_additional_tools:
    - top_segments
---

# Risk segments

Call `top_segments(dimension, start_date, end_date, rank_by, limit, min_transactions)`.

## Arguments
- `dimension`: `merchant`, `merchant_category`, `product_cd`, `card_type`,
  `payer_email_domain`, `recipient_email_domain`.
- `rank_by` (descending):
  - `high_risk` - count of high-risk transactions. **Use this for "top flagged X",
    "most fraud", "riskiest X"**: the model's own flag is almost always 0 for
    API-served traffic, so ranking by it says nothing.
  - `high_risk_rate` - share of the segment's transactions that are high-risk
  - `high_risk_amount` - dollars in high-risk transactions
  - `avg_score` - mean fraud score
  - `flagged` - model fraud flags (is_fraud). Only when the user explicitly asks
    about the model's flag / is_fraud. Every result row includes `flagged`
    anyway, so you can still report it next to the high-risk ranking.
  - `transactions`, `total_amount` - volume
- `limit`: how many to return. For a comparison of all values of a small
  dimension (card_type, product_cd), use `limit=10`.
- `min_transactions`: set it when the user gives a minimum volume
  ("with at least 50 transactions"); otherwise leave at 1.

## Answer format
Give a numbered list in rank order with the ranking metric and the transaction
count, e.g. "1. <segment> - <high_risk> high-risk of <transactions> (<high_risk_rate>%)".
Name the window and the metric you ranked by.
If the top values tie, say so.
