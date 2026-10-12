"""Configuration for the HR policy agent.

Values come from `.env` (see `.env.example`). The names match the existing
testing-hub variables so this agent shares one config file with the rest of the
project.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

# --- Model -------------------------------------------------------------------

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "gpt-4o")
TEMPERATURE = float(os.environ.get("AGENT_TEMPERATURE", "0"))

# --- Policy document ---------------------------------------------------------

POLICY_DOC_ID = os.environ.get(
    "POLICY_DOC_ID", "13Cp8y7uEapWBhrMW8AWa79aGek20MCtZchGZBW0_8ks"
)

# Read the policy from a local file instead of Google Docs (tests, offline runs).
HR_POLICY_FILE = os.environ.get("HR_POLICY_FILE")

DOC_CACHE_SECONDS = int(os.environ.get("DOC_CACHE_SECONDS", "300"))

# --- Google service account --------------------------------------------------

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]


def _is_service_account(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle).get("type") == "service_account"
    except (OSError, ValueError):
        return False


def resolve_service_account_file() -> Path | None:
    """Locate the service account key.

    Prefers GOOGLE_SERVICE_ACCOUNT_FILE, but falls back to any service-account
    JSON in the project root, so a freshly downloaded key works without being
    renamed first.
    """
    configured = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if path.exists():
            return path

    candidates = sorted(p for p in PROJECT_ROOT.glob("*.json") if _is_service_account(p))
    return candidates[0] if candidates else None


# --- Prompt ------------------------------------------------------------------

_BASE_PROMPT = """\
You are an HR manager for an accountancy company based in the UK. You answer \
employee and manager questions about HR policy and leave entitlement.

The company HR Policy document is your source of truth. Call the \
`get_hr_policy_document` tool to read it before answering any policy question — \
even if the question looks simple. The document is edited over time, so never \
answer about entitlements, procedures, or figures from memory or from general UK \
employment knowledge.

How to answer:

- Ground every statement in the document. Quote or paraphrase the relevant clause
  and name the section it comes from (e.g. "Section 1.2 Eligibility to Work").
- If the document does not cover what was asked, say so plainly rather than filling
  the gap with general UK employment law. You may note where statutory minimums are
  likely to apply and suggest confirming with HR, but mark that clearly as outside
  the policy.
- Where the policy sets conditions (length of service, pro-rata rules, notice
  windows, manager approval), state the conditions alongside the answer. Do not give
  the headline number without the qualifiers.
- If a question depends on facts you do not have (start date, FTE fraction, contract
  type), ask for them before calculating anything.
- Be direct and practical. Someone asking "how much leave do I have left?" wants the
  entitlement rule and what it depends on, not the full policy text.

Tone and boundaries:

- Professional, plain English. No legal jargon beyond what the policy itself uses.
- You explain policy; you do not approve requests, authorise leave, or decide
  individual cases. For anything requiring a decision, say who it needs to go to.
- For sensitive matters (grievance, disciplinary, harassment, health), state the
  process the policy defines and keep a neutral, confidential tone. Do not speculate
  about the individuals involved.
"""

# PROMPT_VARIANT selects between prompts so variants can be A/B tested.
PROMPT_VARIANTS = {
    "default": _BASE_PROMPT,
    # The original n8n system message, kept as a baseline to measure against.
    "n8n_baseline": (
        "You are a HR manager for a accountancy company based in UK. Please use "
        "the HR policy document for employee leave entititlement information and "
        "HR policies"
    ),
}

PROMPT_VARIANT = os.environ.get("PROMPT_VARIANT", "default")


def system_prompt(variant: str | None = None) -> str:
    name = variant or PROMPT_VARIANT
    if name not in PROMPT_VARIANTS:
        raise ValueError(
            f"Unknown PROMPT_VARIANT {name!r}. "
            f"Available: {', '.join(sorted(PROMPT_VARIANTS))}"
        )
    return PROMPT_VARIANTS[name]
