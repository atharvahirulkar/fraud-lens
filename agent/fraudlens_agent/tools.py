"""
BigQuery tools for the FraudLens analyst agent.

Each tool is unlocked by one skill (see skills/*/SKILL.md, `adk_additional_tools`):
  fraud-kpis     → get_fraud_metrics   totals, rates and daily trend for a date range
  risk-segments  → top_segments        rank merchants / categories / emails / cards
  ad-hoc-sql     → run_readonly_sql    anything else, as a guarded read-only SELECT

All tools query the `fraudlens` dataset written by the Airflow DAG:
  scored         external table over gs://<bucket>/scored/dt=*/results.parquet
  daily_summary  one row per day (model-flag based)
"""

from __future__ import annotations

import datetime as dt
import os
import re
from functools import lru_cache
from typing import Any

from google.cloud import bigquery

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
DATASET = os.getenv("FRAUDLENS_BQ_DATASET", "fraudlens")
LOCATION = os.getenv("FRAUDLENS_BQ_LOCATION", "us-central1")
# Review band: scores at or above this are "high-risk". The model's own flag
# (is_fraud) uses its tuned threshold, which API-served traffic rarely reaches.
HIGH_RISK_SCORE = float(os.getenv("FRAUDLENS_HIGH_RISK_SCORE", "0.20"))

MAX_BYTES_BILLED = 1_000_000_000  # 1 GB per query
MAX_RANGE_DAYS = 366
MAX_SQL_ROWS = 100

ALLOWED_TABLES = {"scored", "daily_summary"}
SEGMENT_DIMENSIONS = {
    "merchant": "merchant",
    "merchant_category": "merchant_category",
    "product_cd": "product_cd",
    "card_type": "card_type",
    "payer_email_domain": "p_emaildomain",
    "recipient_email_domain": "r_emaildomain",
}
RANK_METRICS = {
    "high_risk": "high_risk",
    "high_risk_rate": "high_risk_rate",
    "high_risk_amount": "high_risk_amount",
    "avg_score": "avg_score",
    "flagged": "flagged",
    "transactions": "transactions",
    "total_amount": "total_amount",
}
_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|EXPORT|LOAD|CALL|EXECUTE|DECLARE|BEGIN)\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT, location=LOCATION)


def _table(name: str) -> str:
    return f"`{_client().project}.{DATASET}.{name}`"


def _job_config(params: list | None = None, dry_run: bool = False) -> bigquery.QueryJobConfig:
    return bigquery.QueryJobConfig(
        query_parameters=params or [],
        default_dataset=f"{_client().project}.{DATASET}",
        maximum_bytes_billed=MAX_BYTES_BILLED,
        dry_run=dry_run,
        use_query_cache=not dry_run,
        labels={"app": "fraudlens-agent"},
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 6)
    return value


def _rows(sql: str, params: list | None = None) -> list[dict]:
    result = _client().query(sql, job_config=_job_config(params)).result()
    return [{k: _json_safe(v) for k, v in row.items()} for row in result]


def _date_range(start_date: str, end_date: str) -> tuple[dt.date, dt.date]:
    try:
        start, end = dt.date.fromisoformat(start_date), dt.date.fromisoformat(end_date)
    except ValueError as e:
        raise ValueError(f"Dates must be YYYY-MM-DD: {e}") from e
    if end < start:
        raise ValueError(f"end_date {end} is before start_date {start}")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"Range is longer than {MAX_RANGE_DAYS} days")
    return start, end


def _range_params(start: dt.date, end: dt.date) -> list:
    return [
        bigquery.ScalarQueryParameter("start", "DATE", start),
        bigquery.ScalarQueryParameter("end", "DATE", end),
        bigquery.ScalarQueryParameter("hr", "FLOAT64", HIGH_RISK_SCORE),
    ]


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 6) if den else None


def get_fraud_metrics(start_date: str, end_date: str, include_daily: bool = False) -> dict:
    """Totals, rates and amounts for scored transactions between two dates (inclusive).

    Args:
      start_date: First day, YYYY-MM-DD.
      end_date: Last day, YYYY-MM-DD (inclusive).
      include_daily: Also return one row per day (use for trends or "which day" questions).

    Returns:
      dict with `totals` (transactions, flagged, flag_rate, high_risk, high_risk_rate,
      total_amount, flagged_amount, high_risk_amount, avg_score, max_score) and,
      if requested, `daily` rows with the same fields per date. Rates are fractions
      (0.0123 = 1.23%). `flagged` = the model's own fraud decision (is_fraud);
      `high_risk` = fraud_score >= high_risk_threshold.
    """
    try:
        start, end = _date_range(start_date, end_date)
        daily = _rows(
            f"""
            SELECT
              dt AS date,
              COUNT(*) AS transactions,
              COUNTIF(is_fraud) AS flagged,
              COUNTIF(fraud_score >= @hr) AS high_risk,
              SUM(transaction_amt) AS total_amount,
              SUM(IF(is_fraud, transaction_amt, 0)) AS flagged_amount,
              SUM(IF(fraud_score >= @hr, transaction_amt, 0)) AS high_risk_amount,
              SUM(fraud_score) AS score_sum,
              MAX(fraud_score) AS max_score
            FROM {_table("scored")}
            WHERE dt BETWEEN @start AND @end
            GROUP BY dt
            ORDER BY dt
            """,
            _range_params(start, end),
        )
    except Exception as e:  # surfaced to the model so it can correct itself
        return {"status": "error", "error": str(e)}

    n = sum(d["transactions"] for d in daily)
    totals = {
        "transactions": n,
        "flagged": sum(d["flagged"] for d in daily),
        "high_risk": sum(d["high_risk"] for d in daily),
        "total_amount": round(sum(d["total_amount"] for d in daily), 2),
        "flagged_amount": round(sum(d["flagged_amount"] for d in daily), 2),
        "high_risk_amount": round(sum(d["high_risk_amount"] for d in daily), 2),
        "avg_score": _ratio(sum(d["score_sum"] for d in daily), n),
        "max_score": max((d["max_score"] for d in daily), default=None),
    }
    totals["flag_rate"] = _ratio(totals["flagged"], n)
    totals["high_risk_rate"] = _ratio(totals["high_risk"], n)

    result = {
        "status": "success",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "days_with_data": len(daily),
        "high_risk_threshold": HIGH_RISK_SCORE,
        "totals": totals,
    }
    if include_daily:
        for d in daily:
            d["avg_score"] = _ratio(d.pop("score_sum"), d["transactions"])
            d["flag_rate"] = _ratio(d["flagged"], d["transactions"])
            d["high_risk_rate"] = _ratio(d["high_risk"], d["transactions"])
        result["daily"] = daily
    return result


def top_segments(
    dimension: str,
    start_date: str,
    end_date: str,
    rank_by: str = "high_risk",
    limit: int = 5,
    min_transactions: int = 1,
) -> dict:
    """Rank segments (e.g. merchants) by a risk metric between two dates (inclusive).

    Args:
      dimension: One of merchant, merchant_category, product_cd, card_type,
        payer_email_domain, recipient_email_domain.
      start_date: First day, YYYY-MM-DD.
      end_date: Last day, YYYY-MM-DD (inclusive).
      rank_by: One of high_risk (count), high_risk_rate, high_risk_amount, avg_score,
        flagged, transactions, total_amount. Sorted descending.
      limit: Number of segments to return (1-50).
      min_transactions: Ignore segments with fewer transactions than this.

    Returns:
      dict with `segments`: ranked rows, each with segment, transactions, flagged,
      high_risk, high_risk_rate, high_risk_amount, total_amount, avg_score.
    """
    if dimension not in SEGMENT_DIMENSIONS:
        return {"status": "error", "error": f"dimension must be one of {sorted(SEGMENT_DIMENSIONS)}"}
    if rank_by not in RANK_METRICS:
        return {"status": "error", "error": f"rank_by must be one of {sorted(RANK_METRICS)}"}
    try:
        start, end = _date_range(start_date, end_date)
        column = SEGMENT_DIMENSIONS[dimension]
        params = _range_params(start, end) + [
            bigquery.ScalarQueryParameter("min_n", "INT64", max(1, int(min_transactions))),
            bigquery.ScalarQueryParameter("lim", "INT64", min(50, max(1, int(limit)))),
        ]
        segments = _rows(
            f"""
            SELECT
              {column} AS segment,
              COUNT(*) AS transactions,
              COUNTIF(is_fraud) AS flagged,
              COUNTIF(fraud_score >= @hr) AS high_risk,
              SAFE_DIVIDE(COUNTIF(fraud_score >= @hr), COUNT(*)) AS high_risk_rate,
              ROUND(SUM(IF(fraud_score >= @hr, transaction_amt, 0)), 2) AS high_risk_amount,
              ROUND(SUM(transaction_amt), 2) AS total_amount,
              AVG(fraud_score) AS avg_score
            FROM {_table("scored")}
            WHERE dt BETWEEN @start AND @end AND {column} IS NOT NULL
            GROUP BY segment
            HAVING COUNT(*) >= @min_n
            ORDER BY {RANK_METRICS[rank_by]} DESC, segment
            LIMIT @lim
            """,
            params,
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}
    return {
        "status": "success",
        "dimension": dimension,
        "rank_by": rank_by,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "high_risk_threshold": HIGH_RISK_SCORE,
        "segments": segments,
    }


def run_readonly_sql(query: str) -> dict:
    """Run one read-only BigQuery SELECT over the fraudlens tables (scored, daily_summary).

    Use only when get_fraud_metrics and top_segments can't answer the question.
    Tables can be referenced unqualified (`scored`, `daily_summary`). Results are
    capped at 100 rows; queries over 1 GB are rejected.

    Args:
      query: A single SELECT (or WITH ... SELECT) statement in GoogleSQL.

    Returns:
      dict with `columns`, `rows` (list of lists) and `truncated`.
    """
    sql = query.strip().rstrip(";").strip()
    if ";" in sql:
        return {"status": "error", "error": "Only a single statement is allowed."}
    if not re.match(r"^(SELECT|WITH)\b", sql, re.IGNORECASE):
        return {"status": "error", "error": "Query must start with SELECT or WITH."}
    if _FORBIDDEN_SQL.search(sql):
        return {"status": "error", "error": "Only read-only SELECT queries are allowed."}
    try:
        # Dry run first: BigQuery reports exactly which tables the query reads
        dry = _client().query(sql, job_config=_job_config(dry_run=True))
        tables = {(t.dataset_id, t.table_id) for t in (dry.referenced_tables or [])}
        outside = [f"{d}.{t}" for d, t in tables if d != DATASET or t not in ALLOWED_TABLES]
        if outside:
            return {"status": "error", "error": f"Query may only read {DATASET}.scored / {DATASET}.daily_summary, not {outside}"}
        result = _client().query(sql, job_config=_job_config()).result(max_results=MAX_SQL_ROWS + 1)
        columns = [f.name for f in result.schema]
        rows = [[_json_safe(v) for v in row.values()] for row in result]
    except Exception as e:
        return {"status": "error", "error": str(e)}
    return {
        "status": "success",
        "columns": columns,
        "rows": rows[:MAX_SQL_ROWS],
        "truncated": len(rows) > MAX_SQL_ROWS,
    }
