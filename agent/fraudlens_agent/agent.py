"""
FraudLens analyst: answers plain-English questions over the FraudLens BigQuery
tables with Gemini (Vertex AI) and three Agent Skills.

Run:
    adk web agent/          # dev UI
    adk run agent/fraudlens_agent
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.skills import load_skill_from_dir
from google.adk.tools.skill_toolset import SkillDiscoveryMode, SkillToolset
from google.genai import types

from .tools import HIGH_RISK_SCORE, get_fraud_metrics, run_readonly_sql, top_segments

MODEL = os.getenv("FRAUDLENS_AGENT_MODEL", "gemini-2.5-flash")
SKILLS_DIR = Path(__file__).parent / "skills"


def _today() -> dt.date:
    # Evals pin "today" so relative windows ("last week") are reproducible
    pinned = os.getenv("FRAUDLENS_TODAY")
    return dt.date.fromisoformat(pinned) if pinned else dt.date.today()


def instruction(_ctx: ReadonlyContext) -> str:
    today = _today()
    yesterday = today - dt.timedelta(days=1)
    return f"""You are FraudLens Analyst. You answer questions about FraudLens batch
fraud-scoring results stored in BigQuery. The data is synthetic demo traffic
scored by the production XGBoost model.

Today is {today.isoformat()} ({today:%A}). Batches land nightly, so the most recent
data is for yesterday, {yesterday.isoformat()}.

Date windows (all inclusive, all ending yesterday unless the user gives dates):
- "yesterday" = {yesterday.isoformat()}
- "last week", "past week", "last 7 days", "this week" = {(today - dt.timedelta(days=7)).isoformat()} to {yesterday.isoformat()}
- "the week before" / "previous week" = the 7 days before that: {(today - dt.timedelta(days=14)).isoformat()} to {(today - dt.timedelta(days=8)).isoformat()}
- "last 2 weeks", "last 14 days" = {(today - dt.timedelta(days=14)).isoformat()} to {yesterday.isoformat()}
- "last 30 days", "last month" = {(today - dt.timedelta(days=30)).isoformat()} to {yesterday.isoformat()}
- A named date without a year means the most recent past occurrence.

Definitions:
- "fraud", "flagged", "fraud rate" = the model's decision (is_fraud).
- "high-risk", "risky", "suspicious" = fraud_score >= {HIGH_RISK_SCORE:.2f}.
- "top flagged merchants" (or any "top flagged <segment>") = rank by high-risk count,
  and mention the model-flag count alongside.

How to work:
1. Pick the skill that fits: fraud-kpis (totals, rates, trends), risk-segments
   (rankings by merchant/category/email/card/product), ad-hoc-sql (anything else).
   Load it with load_skill, then call the tool it provides.
2. Every number in your answer must come from a tool result in this conversation.
   Never estimate or reuse numbers from examples.
3. Always state the date window you used.
4. Percentages with 2 decimals, dollar amounts with 2 decimals and a $ sign,
   scores with 4 decimals, dates as YYYY-MM-DD.
5. If a tool returns an error, fix the call and retry once; if it still fails, say so.
6. Keep answers short: the answer first, then one line of context.
"""


def _skills():
    return [load_skill_from_dir(p) for p in sorted(SKILLS_DIR.iterdir()) if (p / "SKILL.md").exists()]


root_agent = Agent(
    name="fraudlens_analyst",
    model=MODEL,
    description="Answers plain-English questions about FraudLens fraud-scoring results in BigQuery.",
    instruction=instruction,
    tools=[
        SkillToolset(
            skills=_skills(),
            # Each skill's tool is declared to the model only once the skill is loaded
            additional_tools=[get_fraud_metrics, top_segments, run_readonly_sql],
            discovery_mode=SkillDiscoveryMode.EAGER,
        )
    ],
    generate_content_config=types.GenerateContentConfig(
        temperature=0,
        # Vertex returns 429 under bursty load (e.g. eval runs); back off and retry
        http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=5, initial_delay=2.0, max_delay=30.0,
                                                 http_status_codes=[429, 500, 503])
        ),
    ),
)
