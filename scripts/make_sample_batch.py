"""
Generate a synthetic batch of transactions and upload it to GCS so the
Airflow DAG has something to score.

Writes gs://<bucket>/incoming/<date>/batch.parquet with the columns the
/predict API expects.

Usage:
    python scripts/make_sample_batch.py --bucket fraudlens-artifacts-<project> \
        --date 2026-10-14 --rows 200
"""

import argparse
import io
from datetime import date

import numpy as np
import pandas as pd
from google.cloud import storage

EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "anonymous.com", "protonmail.com"]


def make_batch(rows: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    p_email = rng.choice(EMAIL_DOMAINS, rows, p=[0.45, 0.2, 0.15, 0.1, 0.05, 0.05])
    # ~30% of rows have a recipient domain that differs from the payer's
    r_email = np.where(rng.random(rows) < 0.3, rng.choice(EMAIL_DOMAINS, rows), p_email)
    return pd.DataFrame({
        "transaction_id": [f"txn-{seed}-{i:05d}" for i in range(rows)],
        "transaction_amt": np.round(rng.lognormal(mean=4.2, sigma=1.1, size=rows), 2).clip(0.5, 20000),
        "product_cd": rng.choice(list("WCRHS"), rows, p=[0.7, 0.12, 0.08, 0.06, 0.04]),
        "card_type": rng.choice(["debit", "credit"], rows, p=[0.75, 0.25]),
        "addr1": rng.integers(100, 540, rows),
        "dist1": np.where(rng.random(rows) < 0.6, np.nan, np.round(rng.exponential(60, rows), 1)),
        "p_emaildomain": p_email,
        "r_emaildomain": r_email,
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--date", default=date.today().isoformat(), help="Partition date (YYYY-MM-DD)")
    parser.add_argument("--rows", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    df = make_batch(args.rows, args.seed)
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)

    object_name = f"incoming/{args.date}/batch.parquet"
    storage.Client().bucket(args.bucket).blob(object_name).upload_from_string(buf.getvalue())
    print(f"Uploaded {len(df)} rows → gs://{args.bucket}/{object_name}")


if __name__ == "__main__":
    main()
