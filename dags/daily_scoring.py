"""
Airflow DAG for daily batch fraud detection scoring (GCP).

Runs nightly at 2 AM UTC on the Compute Engine Airflow VM:
1. Lists new transactions in GCS   gs://<bucket>/incoming/YYYY-MM-DD/*.parquet
2. Validates the schema
3. Scores each row via the Cloud Run inference API (POST /predict)
4. Writes results to GCS            gs://<bucket>/scored/dt=YYYY-MM-DD/results.parquet
5. Refreshes BigQuery: external table over scored/ + a native daily_summary table

Task flow:
  extract_from_gcs → validate_transactions → score_and_load → bigquery_refresh

Tasks pass GCS object paths (small strings) through XCom — never DataFrames.
"""

import io
import math
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from airflow.decorators import dag, task
from airflow.exceptions import AirflowSkipException
from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook
from airflow.providers.google.cloud.hooks.gcs import GCSHook

# Configuration (set in infra/airflow-vm/docker-compose.yml)
GCS_BUCKET = os.environ["GCS_BUCKET"]
GCP_PROJECT_ID = os.environ["GCP_PROJECT_ID"]
BQ_DATASET = os.getenv("BQ_DATASET", "fraudlens")
BQ_LOCATION = os.getenv("BQ_LOCATION", "us-central1")
API_ENDPOINT = os.environ["API_ENDPOINT"].rstrip("/")
INCOMING_PREFIX = "incoming"
SCORED_PREFIX = "scored"

REQUIRED_COLS = ["transaction_amt", "product_cd"]
OPTIONAL_COLS = ["card_type", "addr1", "dist1", "p_emaildomain", "r_emaildomain"]

default_args = {
    "owner": "fraudlens-ml",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "depends_on_past": False,
}


def _read_parquet(gcs: GCSHook, object_names: list[str]) -> pd.DataFrame:
    frames = [
        pd.read_parquet(io.BytesIO(gcs.download(bucket_name=GCS_BUCKET, object_name=name)))
        for name in object_names
    ]
    return pd.concat(frames, ignore_index=True)


def _clean(value):
    """Convert pandas/numpy scalars to JSON-safe Python values (NaN → None)."""
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


@dag(
    dag_id="fraudlens_daily_scoring",
    description="Daily batch fraud detection scoring (GCS → Cloud Run → GCS/BigQuery)",
    schedule="0 2 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["fraudlens", "ml-pipeline", "batch", "gcp"],
)
def fraudlens_daily_scoring():

    @task
    def extract_from_gcs(ds: str = None) -> list[str]:
        """List the day's incoming parquet files. Skips the run if there are none."""
        prefix = f"{INCOMING_PREFIX}/{ds}/"
        objects = [
            o for o in GCSHook().list(bucket_name=GCS_BUCKET, prefix=prefix)
            if o.endswith(".parquet")
        ]
        if not objects:
            raise AirflowSkipException(f"No files at gs://{GCS_BUCKET}/{prefix}")
        print(f"[Extract] {len(objects)} file(s) under gs://{GCS_BUCKET}/{prefix}")
        return objects

    @task
    def validate_transactions(object_names: list[str]) -> list[str]:
        """Fail fast on schema problems before calling the API."""
        df = _read_parquet(GCSHook(), object_names)
        missing = [c for c in REQUIRED_COLS if c not in df.columns]
        if missing:
            raise ValueError(f"Missing required columns: {missing}")
        bad_amt = (pd.to_numeric(df["transaction_amt"], errors="coerce") <= 0).sum()
        if bad_amt:
            print(f"[Validate] ⚠ {bad_amt} rows with non-positive amount will be rejected by the API")
        print(f"[Validate] ✓ {len(df)} transactions passed validation")
        return object_names

    @task
    def score_and_load(object_names: list[str], ds: str = None) -> str:
        """Score every row via /predict and write results parquet to GCS."""
        gcs = GCSHook()
        df = _read_parquet(gcs, object_names)
        if "transaction_id" not in df.columns:
            df["transaction_id"] = [f"{ds}-{i}" for i in range(len(df))]

        print(f"[Score] Scoring {len(df)} transactions via {API_ENDPOINT}/predict")
        session = requests.Session()
        # Wake the Cloud Run service (scale-to-zero) before the loop
        session.get(f"{API_ENDPOINT}/health", timeout=60).raise_for_status()

        results, failed = [], 0
        for row in df.to_dict(orient="records"):
            payload = {c: _clean(row.get(c)) for c in REQUIRED_COLS + OPTIONAL_COLS}
            try:
                resp = session.post(f"{API_ENDPOINT}/predict", json=payload, timeout=30)
                resp.raise_for_status()
                pred = resp.json()
                results.append({
                    "transaction_id": str(row["transaction_id"]),
                    "transaction_amt": payload["transaction_amt"],
                    "product_cd": payload["product_cd"],
                    "fraud_score": pred["fraud_score"],
                    "is_fraud": pred["is_fraud"],
                    "model_version": pred["model_version"],
                    "explanation": pred["explanation"],
                    "scored_at": datetime.now(timezone.utc),
                })
            except Exception as e:
                failed += 1
                print(f"[Score] Error on {row.get('transaction_id')}: {e}")

        if not results:
            raise RuntimeError(f"All {failed} transactions failed to score")
        print(f"[Score] ✓ Scored {len(results)} ({failed} failures)")

        buf = io.BytesIO()
        # Microsecond timestamps: BigQuery can't read parquet TIMESTAMP(NANOS)
        pd.DataFrame(results).to_parquet(
            buf, index=False, coerce_timestamps="us", allow_truncated_timestamps=True
        )
        object_name = f"{SCORED_PREFIX}/dt={ds}/results.parquet"
        gcs.upload(
            bucket_name=GCS_BUCKET,
            object_name=object_name,
            data=buf.getvalue(),
            mime_type="application/octet-stream",
        )
        print(f"[Load] ✓ gs://{GCS_BUCKET}/{object_name}")
        return object_name

    @task
    def bigquery_refresh(_scored_object: str, ds: str = None) -> None:
        """
        Ensure the hive-partitioned external table exists (schema inferred from
        the parquet files), then upsert today's row into daily_summary.
        """
        table = f"`{GCP_PROJECT_ID}.{BQ_DATASET}.scored`"
        summary = f"`{GCP_PROJECT_ID}.{BQ_DATASET}.daily_summary`"
        sql = f"""
        CREATE EXTERNAL TABLE IF NOT EXISTS {table}
        WITH PARTITION COLUMNS
        OPTIONS (
          format = 'PARQUET',
          uris = ['gs://{GCS_BUCKET}/{SCORED_PREFIX}/*'],
          hive_partition_uri_prefix = 'gs://{GCS_BUCKET}/{SCORED_PREFIX}'
        );

        CREATE TABLE IF NOT EXISTS {summary} (
          dt DATE,
          transactions INT64,
          flagged INT64,
          fraud_rate FLOAT64,
          avg_score FLOAT64,
          p95_score FLOAT64,
          flagged_amount FLOAT64,
          updated_at TIMESTAMP
        );

        MERGE {summary} T
        USING (
          SELECT
            dt,
            COUNT(*) AS transactions,
            COUNTIF(is_fraud) AS flagged,
            SAFE_DIVIDE(COUNTIF(is_fraud), COUNT(*)) AS fraud_rate,
            AVG(fraud_score) AS avg_score,
            APPROX_QUANTILES(fraud_score, 100)[OFFSET(95)] AS p95_score,
            SUM(IF(is_fraud, transaction_amt, 0)) AS flagged_amount,
            CURRENT_TIMESTAMP() AS updated_at
          FROM {table}
          WHERE dt = DATE '{ds}'
          GROUP BY dt
        ) S
        ON T.dt = S.dt
        WHEN MATCHED THEN UPDATE SET
          transactions = S.transactions, flagged = S.flagged, fraud_rate = S.fraud_rate,
          avg_score = S.avg_score, p95_score = S.p95_score,
          flagged_amount = S.flagged_amount, updated_at = S.updated_at
        WHEN NOT MATCHED THEN INSERT ROW;
        """
        hook = BigQueryHook(use_legacy_sql=False, location=BQ_LOCATION)
        hook.insert_job(
            configuration={"query": {"query": sql, "useLegacySql": False}},
            project_id=GCP_PROJECT_ID,
            location=BQ_LOCATION,
        ).result()
        print(f"[BigQuery] ✓ {summary} updated for {ds}")

    files = extract_from_gcs()
    validated = validate_transactions(files)
    scored = score_and_load(validated)
    bigquery_refresh(scored)


fraudlens_daily_scoring()
