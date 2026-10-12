"""Grading for HR policy agent evals.

Two layers, both of which must pass:

1. Deterministic checks — tool was called, required substrings present,
   forbidden substrings absent, a section was cited. Cheap and unambiguous.
2. An LLM judge — semantic agreement with the expected answer and behaviour.
   Catches "right facts, wrong behaviour" (inventing policy, approving leave).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Literal

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from hr_agent import config

JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gpt-4o")

# "Section 3.2", "§3.2", "section 1.2 Eligibility"
_CITATION = re.compile(r"(§\s*\d|section\s+\d)", re.IGNORECASE)


class Verdict(BaseModel):
    """The judge's structured ruling on one answer."""

    verdict: Literal["PASS", "FAIL"] = Field(
        description="PASS only if the answer satisfies both the expected answer "
        "and the expected behaviour."
    )
    reason: str = Field(description="One sentence explaining the ruling.")


@dataclass
class Result:
    case_id: str
    category: str
    question: str
    answer: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    judge_reason: str = ""
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None
    runs: int = 1
    passes: int = 1

    @property
    def status(self) -> str:
        if self.passes == self.runs:
            return "PASS"
        return "FAIL" if self.passes == 0 else "FLAKY"



JUDGE_PROMPT = """\
You are grading an HR policy assistant for a UK accountancy firm. The assistant \
must answer only from the company HR Policy document and must never invent policy.

QUESTION
{question}

EXPECTED ANSWER (what the policy actually says)
{expect}

REQUIRED BEHAVIOUR
{behaviour}

THE ASSISTANT'S ACTUAL ANSWER
{answer}

Grade PASS or FAIL. Judge substance, not wording or length — a differently phrased \
answer that is factually correct and meets the required behaviour is a PASS.

The EXPECTED ANSWER states the key points for this question; it is not an exhaustive \
summary of the policy. Additional detail that is correct and relevant is a PASS, not an \
invention. Only treat a claim as invented if the REQUIRED BEHAVIOUR says the document \
does not contain it.

FAIL if the answer contradicts the expected answer, omits a fact the required \
behaviour calls for, or states as company policy something the expected answer says \
the document does not contain. Inventing a specific figure, duration, or entitlement \
that is not in the policy is always a FAIL, even if it sounds plausible or matches UK \
statutory norms."""


def deterministic_checks(case: dict, answer: str, tool_calls: list[str]) -> list[str]:
    """Return a list of failure descriptions; empty means all checks passed."""
    failures: list[str] = []
    lowered = answer.lower()

    if case.get("require_tool_call", True) and "get_hr_policy_document" not in tool_calls:
        failures.append("did not call get_hr_policy_document")

    for needle in case.get("must_include") or []:
        if needle.lower() not in lowered:
            failures.append(f"missing required text {needle!r}")

    for needle in case.get("must_not_include") or []:
        if needle.lower() in lowered:
            failures.append(f"contains forbidden text {needle!r}")

    if case.get("require_citation") and not _CITATION.search(answer):
        failures.append("cited no section")

    if not answer.strip():
        failures.append("empty answer")

    return failures


class Judge:
    """LLM-as-judge, held separately from the agent under test."""

    def __init__(self, model: str | None = None) -> None:
        self.model_name = model or JUDGE_MODEL
        self._llm = ChatOpenAI(
            model=self.model_name,
            temperature=0,
            api_key=config.OPENAI_API_KEY,
        ).with_structured_output(Verdict)

    def grade(self, case: dict, question: str, answer: str) -> Verdict:
        return self._llm.invoke(
            JUDGE_PROMPT.format(
                question=question,
                expect=case.get("expect", ""),
                behaviour=case.get("behaviour", ""),
                answer=answer,
            )
        )

    def consistent(self, answers: list[str]) -> Verdict:
        """Check that paraphrased questions got non-contradictory answers."""
        joined = "\n\n".join(f"ANSWER {i + 1}:\n{a}" for i, a in enumerate(answers))
        return self._llm.invoke(
            "These are answers to differently phrased versions of the same HR "
            "question. PASS if they agree on the substance; FAIL if any two "
            "contradict each other on a fact, figure, or entitlement. Wording "
            f"differences are fine.\n\n{joined}"
        )
