"""Unit tests for the eval grader (no BigQuery or Gemini needed)."""

import datetime as dt

from evals.run_evals import check_date, check_number, check_top_k, grade, numbers_in


def test_numbers_ignore_iso_dates_and_parse_money():
    text = "From 2026-10-07 to 2026-10-13 there were 2,301 transactions worth $12,345.67 (4.12%)."
    assert numbers_in(text) == [2301, 12345.67, 4.12]


def test_pct_and_usd_tolerance():
    assert check_number("High-risk rate was 4.12%.", 0.041234, "pct")
    assert not check_number("High-risk rate was 4.20%.", 0.041234, "pct")
    assert check_number("Total $9,876.54", 9876.543, "usd")
    assert check_number("Count: 15", 15, "count")
    assert not check_number("Count: 16", 15, "count")


def test_date_variants():
    d = dt.date(2026, 10, 9)
    assert check_date("The worst day was 2026-10-09.", d)
    assert check_date("October 9 had the highest rate", d)
    assert not check_date("October 19 had the highest rate", d)


def test_top_k_order_and_ties():
    ranked = [("CoinSwap Exchange", 40), ("GiftCard Hub", 30), ("LuxeTime Watches", 30), ("QuickVolt Electronics", 10)]
    assert check_top_k("1. CoinSwap Exchange 2. GiftCard Hub 3. LuxeTime Watches", ranked, 3)
    # tie at the cut: either order of the tied pair is fine
    assert check_top_k("1. CoinSwap Exchange 2. LuxeTime Watches 3. GiftCard Hub", ranked, 3)
    assert not check_top_k("1. GiftCard Hub 2. CoinSwap Exchange 3. LuxeTime Watches", ranked, 3)
    assert not check_top_k("1. CoinSwap Exchange 2. QuickVolt Electronics 3. GiftCard Hub", ranked, 3)


def test_grade_numbers_needs_every_value():
    ok, _ = grade("Flag rate 0.00%, high-risk rate 4.12%.", {"type": "numbers", "format": "pct"}, [(0.0, 0.0412)])
    assert ok
    ok, _ = grade("Flag rate 0.00%.", {"type": "numbers", "format": "pct"}, [(0.0, 0.0412)])
    assert not ok
