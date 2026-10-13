"""
Run the FraudLens analyst against fraud_qa.yaml and grade every answer against
BigQuery ground truth.

Usage (from agent/):
    python -m evals.run_evals                  # all cases
    python -m evals.run_evals --only kpi_      # cases whose id starts with kpi_
    python -m evals.run_evals --repeat 3       # run each case 3x to measure flakiness

Reports per case: pass/fail, expected vs. answer, skills loaded, tools called.
Summary: answer accuracy, routing accuracy (expected skill loaded), mean latency.
Writes evals/results/<timestamp>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import yaml

HERE = Path(__file__).parent
CASES_FILE = HERE / "fraud_qa.yaml"

_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBER = re.compile(r"(?<![\w.])-?\$?\d[\d,]*(?:\.\d+)?")
_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]


# ── Grading ─────────────────────────────────────────────────────────────────

def numbers_in(text: str) -> list[float]:
    """All numbers in the text, ignoring ISO dates (2026-10-07 is not 2026, 10, 7)."""
    text = _ISO_DATE.sub(" ", text)
    out = []
    for m in _NUMBER.findall(text):
        try:
            out.append(float(m.replace("$", "").replace(",", "")))
        except ValueError:
            pass
    return out


def _display(value: float, fmt: str) -> tuple[float, float]:
    """Expected value on the scale the agent is told to show, and the tolerance."""
    if fmt == "pct":
        return value * 100, 0.011          # 2 decimals, allow rounding either way
    if fmt == "usd":
        return value, max(0.011, abs(value) * 0.0005)
    if fmt == "score":
        return value, 0.00051              # 4 decimals
    return value, 0.0                      # counts are exact


def check_number(answer: str, value: float, fmt: str) -> bool:
    target, tol = _display(float(value), fmt)
    return any(abs(n - target) <= tol for n in numbers_in(answer))


def check_date(answer: str, value: dt.date) -> bool:
    text = answer.lower()
    month = _MONTHS[value.month - 1]
    variants = [value.isoformat(), f"{month} {value.day}", f"{month[:3]} {value.day}",
                f"{value.day} {month}", f"{value.day} {month[:3]}"]
    return any(re.search(rf"\b{re.escape(v)}\b", text) for v in variants)


def check_top_k(answer: str, ranked: list[tuple[str, float]], k: int) -> bool:
    """The first k candidate names the answer mentions must form a valid top-k.

    Valid = each is ranked at least as high as the k-th true value (ties allowed),
    and they appear in non-increasing value order.
    """
    if len(ranked) < k:
        return False
    text = answer.lower()
    positions = []
    for name, value in ranked:
        m = re.search(rf"(?<![\w-]){re.escape(str(name).lower())}(?![\w-])", text)
        if m:
            positions.append((m.start(), value))
    mentioned = [v for _, v in sorted(positions)][:k]
    if len(mentioned) < k:
        return False
    kth_value = ranked[k - 1][1]
    in_top = all(v >= kth_value - 1e-12 for v in mentioned)
    ordered = all(a >= b - 1e-12 for a, b in zip(mentioned, mentioned[1:]))
    return in_top and ordered


def grade(answer: str, check: dict, truth_rows: list[tuple]) -> tuple[bool, str]:
    kind = check["type"]
    if not truth_rows:
        return False, "ground truth returned no rows"
    first = truth_rows[0]
    if kind == "number":
        value = first[0]
        return check_number(answer, value, check.get("format", "count")), f"{value}"
    if kind == "numbers":
        fmt = check.get("format", "count")
        oks = [check_number(answer, v, fmt) for v in first]
        return all(oks), ", ".join(f"{v}" for v in first)
    if kind == "date":
        return check_date(answer, first[0]), first[0].isoformat()
    if kind == "text":
        return str(first[0]).lower() in answer.lower(), str(first[0])
    if kind == "top_k":
        k = int(check.get("k", 1))
        ranked = [(r[0], float(r[1])) for r in truth_rows if r[0] is not None]
        expected = ", ".join(f"{n} ({v:.4g})" for n, v in ranked[: k + 1])
        return check_top_k(answer, ranked, k), expected
    raise ValueError(f"Unknown check type {kind}")


# ── Running ─────────────────────────────────────────────────────────────────

def ground_truth(sql: str) -> list[tuple]:
    from google.cloud import bigquery
    client = bigquery.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"),
                             location=os.getenv("FRAUDLENS_BQ_LOCATION", "us-central1"))
    return [tuple(row.values()) for row in client.query(sql).result()]


async def ask(runner, question: str) -> dict:
    from google.genai import types

    session = await runner.session_service.create_session(app_name=runner.app_name, user_id="eval")
    answer, calls = "", []
    start = time.perf_counter()
    async for event in runner.run_async(
        user_id="eval",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=question)]),
    ):
        for call in event.get_function_calls() or []:
            calls.append({"name": call.name, "args": dict(call.args or {})})
        if event.is_final_response() and event.content and event.content.parts:
            answer = "".join(p.text or "" for p in event.content.parts)
    return {"answer": answer.strip(), "calls": calls, "latency_s": round(time.perf_counter() - start, 2)}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="", help="Run cases whose id starts with this prefix")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()

    spec = yaml.safe_load(CASES_FILE.read_text())
    # Pin "today" before the agent module reads it
    os.environ["FRAUDLENS_TODAY"] = spec["reference_date"]
    os.environ.setdefault("FRAUDLENS_HIGH_RISK_SCORE", str(spec["high_risk_score"]))

    from google.adk.runners import InMemoryRunner
    from fraudlens_agent.agent import root_agent

    runner = InMemoryRunner(agent=root_agent, app_name="fraudlens_eval")
    cases = [c for c in spec["cases"] if c["id"].startswith(args.only)]
    results = []
    for case in cases:
        truth = ground_truth(case["ground_truth_sql"])
        for attempt in range(args.repeat):
            try:
                run = await ask(runner, case["question"])
            except Exception as e:  # keep going; a crash is a failed case
                run = {"answer": f"<error: {e}>", "calls": [], "latency_s": None}
            passed, expected = grade(run["answer"], case["check"], truth)
            skills = [c["args"].get("skill_name") or c["args"].get("name") for c in run["calls"] if c["name"] == "load_skill"]
            tools = [c["name"] for c in run["calls"] if c["name"] not in ("load_skill", "load_skill_resource")]
            results.append({
                "id": case["id"], "attempt": attempt, "question": case["question"],
                "passed": passed, "expected": expected, "answer": run["answer"],
                "expected_skill": case["expected_skill"], "skills_loaded": skills,
                "routed": case["expected_skill"] in skills, "tools": tools,
                "calls": run["calls"], "latency_s": run["latency_s"],
            })
            mark = "PASS" if passed else "FAIL"
            route = "✓" if case["expected_skill"] in skills else "✗"
            print(f"[{mark}] {case['id']:<40} route {route} {','.join(skills) or '-':<28} {run['latency_s'] or '-'}s")
            if not passed:
                print(f"       expected: {expected}")
                print(f"       answer:   {run['answer'][:300]!r}")

    n = len(results)
    passed = sum(r["passed"] for r in results)
    routed = sum(r["routed"] for r in results)
    latencies = [r["latency_s"] for r in results if r["latency_s"] is not None]
    summary = {
        "model": root_agent.model,
        "reference_date": spec["reference_date"],
        "cases": len(cases), "runs": n,
        "answer_accuracy": round(passed / n, 4) if n else None,
        "routing_accuracy": round(routed / n, 4) if n else None,
        "mean_latency_s": round(sum(latencies) / len(latencies), 2) if latencies else None,
    }
    print("\n" + json.dumps(summary, indent=2))

    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.json"
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=2, default=str))
    print(f"Results → {out.relative_to(HERE.parent)}")
    return 0 if passed == n else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
