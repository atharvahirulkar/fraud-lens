"""
Generate synthetic batches of transactions and upload them to GCS so the
Airflow DAG has something to score.

Writes gs://<bucket>/incoming/<date>/batch.parquet with the columns the
/predict API expects, plus merchant metadata that the DAG passes through to
BigQuery. Each merchant has its own transaction profile (amounts, products,
card and email mix), so some merchants draw riskier-looking traffic than
others. The data is synthetic; nothing here is a real merchant.

Usage:
    python scripts/make_sample_batch.py --bucket fraudlens-artifacts-<project> \
        --date 2026-10-14 --rows 200
    # A date range writes one batch per day:
    python scripts/make_sample_batch.py --bucket ... --start 2026-09-22 --end 2026-10-13
    # Write locally instead of uploading:
    python scripts/make_sample_batch.py --out-dir /tmp/batches --start ... --end ...
"""

import argparse
import io
import zlib
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "anonymous.com", "protonmail.com", "mail.com"]

# name, category, share of traffic, amount lognormal mean, product_cd probs (W C R H S),
# credit share, payer email probs (same order as EMAIL_DOMAINS)
MERCHANTS = [
    ("FreshCart Grocery",    "grocery",      0.16, 3.6, [0.90, 0.02, 0.04, 0.02, 0.02], 0.20, [0.50, 0.20, 0.15, 0.10, 0.03, 0.01, 0.01]),
    ("Metro Transit Pass",   "travel",       0.10, 3.0, [0.85, 0.05, 0.05, 0.03, 0.02], 0.15, [0.50, 0.20, 0.15, 0.10, 0.03, 0.01, 0.01]),
    ("Corner Coffee Co",     "restaurants",  0.12, 2.6, [0.92, 0.02, 0.02, 0.02, 0.02], 0.15, [0.50, 0.20, 0.15, 0.10, 0.03, 0.01, 0.01]),
    ("StreamBox Media",      "subscription", 0.09, 2.9, [0.20, 0.10, 0.10, 0.10, 0.50], 0.30, [0.45, 0.20, 0.15, 0.10, 0.04, 0.03, 0.03]),
    ("HomeGoods Depot",      "home",         0.09, 4.6, [0.80, 0.05, 0.10, 0.03, 0.02], 0.25, [0.45, 0.20, 0.15, 0.12, 0.04, 0.02, 0.02]),
    ("SkyHigh Airlines",     "travel",       0.06, 5.8, [0.40, 0.15, 0.30, 0.10, 0.05], 0.55, [0.45, 0.15, 0.15, 0.15, 0.04, 0.03, 0.03]),
    ("Urban Threads",        "apparel",      0.09, 4.2, [0.70, 0.10, 0.10, 0.05, 0.05], 0.25, [0.45, 0.20, 0.15, 0.10, 0.04, 0.03, 0.03]),
    ("PixelPlay Games",      "digital",      0.07, 3.5, [0.20, 0.30, 0.05, 0.30, 0.15], 0.40, [0.35, 0.15, 0.15, 0.10, 0.08, 0.09, 0.08]),
    ("QuickVolt Electronics","electronics",  0.08, 5.6, [0.30, 0.35, 0.10, 0.20, 0.05], 0.55, [0.30, 0.10, 0.10, 0.10, 0.10, 0.15, 0.15]),
    ("GiftCard Hub",         "gift_cards",   0.06, 5.6, [0.10, 0.40, 0.05, 0.35, 0.10], 0.70, [0.15, 0.05, 0.05, 0.05, 0.10, 0.30, 0.30]),
    ("CoinSwap Exchange",    "crypto",       0.04, 5.7, [0.10, 0.30, 0.10, 0.40, 0.10], 0.75, [0.15, 0.05, 0.05, 0.05, 0.10, 0.30, 0.30]),
    ("LuxeTime Watches",     "luxury",       0.04, 6.6, [0.50, 0.20, 0.20, 0.05, 0.05], 0.65, [0.35, 0.10, 0.10, 0.15, 0.05, 0.12, 0.13]),
]


def make_batch(rows: int, seed: int, day: str = "") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    shares = np.array([m[2] for m in MERCHANTS])
    idx = rng.choice(len(MERCHANTS), rows, p=shares / shares.sum())

    amt = np.empty(rows)
    product = np.empty(rows, dtype=object)
    card = np.empty(rows, dtype=object)
    p_email = np.empty(rows, dtype=object)
    for i, (_, _, _, mu, prod_p, credit, email_p) in enumerate(MERCHANTS):
        mask = idx == i
        n = int(mask.sum())
        if not n:
            continue
        amt[mask] = rng.lognormal(mean=mu, sigma=0.7, size=n)
        product[mask] = rng.choice(list("WCRHS"), n, p=prod_p)
        card[mask] = np.where(rng.random(n) < credit, "credit", "debit")
        p_email[mask] = rng.choice(EMAIL_DOMAINS, n, p=email_p)

    # ~30% of rows have a recipient domain that differs from the payer's
    r_email = np.where(rng.random(rows) < 0.3, rng.choice(EMAIL_DOMAINS, rows), p_email)
    prefix = f"txn-{day}-" if day else f"txn-{seed}-"
    return pd.DataFrame({
        "transaction_id": [f"{prefix}{i:05d}" for i in range(rows)],
        "transaction_amt": np.round(amt, 2).clip(0.5, 20000),
        "product_cd": product,
        "card_type": card,
        "addr1": rng.integers(100, 540, rows),
        "dist1": np.where(rng.random(rows) < 0.6, np.nan, np.round(rng.exponential(60, rows), 1)),
        "p_emaildomain": p_email,
        "r_emaildomain": r_email,
        "merchant": [MERCHANTS[i][0] for i in idx],
        "merchant_category": [MERCHANTS[i][1] for i in idx],
    })


def _dates(args) -> list[str]:
    if args.start:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end or args.start)
        return [(start + timedelta(days=d)).isoformat() for d in range((end - start).days + 1)]
    return [args.date]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--bucket")
    target.add_argument("--out-dir", help="Write <date>.parquet files here instead of uploading")
    parser.add_argument("--date", default=date.today().isoformat(), help="Partition date (YYYY-MM-DD)")
    parser.add_argument("--start", help="First date of a range (inclusive)")
    parser.add_argument("--end", help="Last date of a range (inclusive)")
    parser.add_argument("--rows", type=int, default=0, help="Rows per batch (default: 250-450, varies by day)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    bucket = None
    if args.bucket:
        from google.cloud import storage
        bucket = storage.Client().bucket(args.bucket)

    for day in _dates(args):
        # Seed from the date so re-running a day reproduces the same batch
        seed = args.seed + zlib.crc32(day.encode())
        rows = args.rows or int(np.random.default_rng(seed).integers(250, 451))
        df = make_batch(rows, seed, day)
        if bucket is None:
            out = Path(args.out_dir) / f"{day}.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(out, index=False)
            print(f"Wrote {len(df)} rows → {out}")
        else:
            buf = io.BytesIO()
            df.to_parquet(buf, index=False)
            object_name = f"incoming/{day}/batch.parquet"
            bucket.blob(object_name).upload_from_string(buf.getvalue())
            print(f"Uploaded {len(df)} rows → gs://{args.bucket}/{object_name}")


if __name__ == "__main__":
    main()
