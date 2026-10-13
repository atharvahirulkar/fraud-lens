---
name: fraud-kpis
description: >-
  Headline fraud KPIs for a date range - transaction counts, model flag rate,
  high-risk rate, dollar amounts, average/max fraud score, and day-by-day
  trends. Use for "what was the fraud rate", "how many transactions",
  "how much money", "which day was worst", or period-over-period comparisons.
metadata:
  adk_additional_tools:
    - get_fraud_metrics
---

# Fraud KPIs

Call `get_fraud_metrics(start_date, end_date, include_daily)` with ISO dates.
Both ends are inclusive.

## Picking the call
- One number for a period → one call, `include_daily=false`, read `totals`.
- "Which day…", "trend", "spike", "busiest" → `include_daily=true`, compare `daily` rows.
- "This week vs the week before", "compared to" → two calls, one per window,
  then report both values and the change in percentage points.

## Reading the result
- `flagged` / `flag_rate`: the model's own fraud decision. This is what "fraud",
  "flagged" and "fraud rate" mean.
- `high_risk` / `high_risk_rate`: transactions with `fraud_score >= high_risk_threshold`
  (the review band). Use for "high-risk", "risky", "suspicious".
- Rates are fractions: 0.0412 → 4.12%.
- `days_with_data` shorter than the window means some days have no batch; say so.

## Answer format
Lead with the number, then the window, e.g.
"Fraud rate (model flags) for <start> to <end> was <flag_rate>% (<flagged> of
<transactions>); high-risk rate was <high_risk_rate>% (<high_risk> transactions)."
When the flag rate is 0, also give the high-risk rate so the answer is useful.
