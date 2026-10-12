"""Run the HR policy agent eval suite.

    python -m evals.run                         # all cases
    python -m evals.run --category grounding    # one category
    python -m evals.run --id HR-01 --id HR-20   # specific cases
    python -m evals.run --variant n8n_baseline  # compare prompt variants

Each case gets a fresh agent, so conversation history never leaks between cases.
Exit code is 0 only if every case passed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from evals.grading import Judge, Result, deterministic_checks
from hr_agent import config
from hr_agent.agent import HRPolicyAgent
from hr_agent.policy import PolicySource

CASES_FILE = Path(__file__).parent / "cases.yaml"
RESULTS_DIR = Path(__file__).parent / "results"

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


CATEGORY_BLURB = {
    "accuracy": "Facts stated plainly in the document.",
    "grounding": "Things the document does **not** say. The agent must decline "
                 "to invent them — the failure mode that matters most here.",
    "consistency": "Paraphrases of one question must not contradict each other.",
    "boundary": "Explains policy; does not approve, authorise, or decide.",
    "sensitive": "Grievance and harassment handled neutrally, via process.",
    "robustness": "Prompt-leak resistance and prompt injection.",
}


def _cell(text: str) -> str:
    """Flatten a YAML value into one markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


def _checks(case: dict) -> str:
    bits = []
    if case.get("policy_file"):
        bits.append("poisoned doc")
    if case.get("setup"):
        bits.append(f"{len(case['setup'])}-turn setup")
    if case.get("require_tool_call", True):
        bits.append("tool call")
    if case.get("require_citation"):
        bits.append("citation")
    bits += [f"`{s}`" for s in case.get("must_include") or []]
    bits += [f"¬`{s}`" for s in case.get("must_not_include") or []]
    bits.append("judge")
    return ", ".join(bits)


def print_case_list(cases: list[dict]) -> None:
    """Emit the case inventory as markdown (used to regenerate docs/TESTING.md)."""
    by_category: dict[str, list[dict]] = {}
    for case in cases:
        by_category.setdefault(case["category"], []).append(case)

    order = [c for c in CATEGORY_BLURB if c in by_category]
    order += [c for c in sorted(by_category) if c not in CATEGORY_BLURB]

    for category in order:
        group = by_category[category]
        print(f"### `{category}` — {len(group)} case"
              f"{'s' if len(group) != 1 else ''}\n")
        if category in CATEGORY_BLURB:
            print(CATEGORY_BLURB[category] + "\n")
        print("| ID | Question | Expected | Source | Checks |")
        print("|---|---|---|---|---|")
        for case in group:
            questions = case.get("variants") or [case["question"]]
            asked = "<br>".join(_cell(q) for q in questions)
            print(f"| `{case['id']}` | {asked} | {_cell(case.get('expect', ''))} "
                  f"| {_cell(case.get('source', ''))} | {_checks(case)} |")
        print()


def _run_case(case: dict, judge: Judge, variant: str | None) -> Result:
    questions = case.get("variants") or [case["question"]]

    answers: list[str] = []
    tool_calls: list[str] = []
    failures: list[str] = []
    judge_reasons: list[str] = []

    # A case may serve a different document — e.g. one with an injected
    # instruction, to test indirect prompt injection through the tool.
    policy_file = case.get("policy_file")
    source = (
        PolicySource(path=Path(__file__).parent.parent / policy_file)
        if policy_file
        else None
    )

    try:
        for question in questions:
            # A fresh agent per question: no history bleed between cases,
            # paraphrase variants are answered independently, and HR-61
            # genuinely sees an empty conversation.
            agent = HRPolicyAgent(prompt_variant=variant, policy_source=source)

            # Turns sent before the graded question, to set up a false premise.
            for turn in case.get("setup") or []:
                agent.ask(turn)

            reply = agent.ask_with_trace(question)
            answers.append(reply.text)
            tool_calls.extend(reply.tool_calls)
            failures.extend(deterministic_checks(case, reply.text, reply.tool_calls))

            ruling = judge.grade(case, question, reply.text)
            judge_reasons.append(ruling.reason)
            if ruling.verdict == "FAIL":
                failures.append(f"judge: {ruling.reason}")

        if len(answers) > 1:
            ruling = judge.consistent(answers)
            if ruling.verdict == "FAIL":
                failures.append(f"inconsistent: {ruling.reason}")

    except Exception as exc:
        return Result(
            case_id=case["id"], category=case["category"], question=questions[0],
            answer="", passed=False, failures=[], tool_calls=tool_calls,
            error=f"{type(exc).__name__}: {exc}",
        )

    return Result(
        case_id=case["id"],
        category=case["category"],
        question=" | ".join(questions),
        answer="\n---\n".join(answers),
        passed=not failures,
        failures=failures,
        judge_reason=" | ".join(judge_reasons),
        tool_calls=sorted(set(tool_calls)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run HR policy agent evals.")
    parser.add_argument("--category", action="append", help="only this category")
    parser.add_argument("--id", action="append", dest="ids", help="only this case id")
    parser.add_argument("--variant", help="prompt variant to test")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("BATCH_CONCURRENCY", "3")),
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="run each case N times; a case only passes if every run passes",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_cases",
        help="print the case inventory as markdown and exit (no API calls)",
    )
    parser.add_argument("--json", type=Path, help="also write results to this path")
    args = parser.parse_args(argv)

    cases = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))
    if args.category:
        wanted = {c.lower() for c in args.category}
        cases = [c for c in cases if c["category"].lower() in wanted]
    if args.ids:
        wanted = {i.upper() for i in args.ids}
        cases = [c for c in cases if c["id"].upper() in wanted]

    if not cases:
        print("No cases matched.", file=sys.stderr)
        return 2

    if args.list_cases:
        print_case_list(cases)
        return 0

    variant = args.variant or config.PROMPT_VARIANT
    judge = Judge()
    print(
        f"{BOLD}{len(cases)} cases{OFF} · agent {config.AGENT_MODEL} "
        f"· prompt {variant} · judge {judge.model_name} "
        f"· concurrency {args.concurrency}\n"
    )

    started = time.monotonic()
    jobs = [(c, i) for c in cases for i in range(args.repeats)]
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        runs = list(pool.map(lambda j: _run_case(j[0], judge, variant), jobs))
    elapsed = time.monotonic() - started

    # Collapse repeats: a case passes only if every one of its runs passed.
    merged: dict[str, Result] = {}
    for result in runs:
        existing = merged.get(result.case_id)
        if existing is None:
            result.runs, result.passes = 1, int(result.passed)
            merged[result.case_id] = result
            continue
        existing.runs += 1
        existing.passes += int(result.passed)
        existing.passed = existing.passes == existing.runs
        if not result.passed:  # keep a failing sample to show the operator
            existing.failures = result.failures
            existing.answer = result.answer
    results = list(merged.values())

    results.sort(key=lambda r: r.case_id)
    for result in results:
        status = result.status
        colour = {"PASS": GREEN, "FAIL": RED, "FLAKY": "\033[33m"}[status]
        tally = f" {DIM}{result.passes}/{result.runs}{OFF}" if result.runs > 1 else ""
        print(f"{colour}{status:<5}{OFF}{tally} {result.case_id:<7} "
              f"{DIM}{result.category:<12}{OFF} {result.question[:58]}")
        if result.error:
            print(f"      {RED}error: {result.error}{OFF}")
        for failure in result.failures:
            print(f"      {RED}· {failure}{OFF}")

    passed = sum(r.passed for r in results)
    print(f"\n{BOLD}{passed}/{len(results)} passed{OFF} in {elapsed:.1f}s")

    by_category: dict[str, list[Result]] = {}
    for result in results:
        by_category.setdefault(result.category, []).append(result)
    for category, group in sorted(by_category.items()):
        hits = sum(r.passed for r in group)
        colour = GREEN if hits == len(group) else RED
        print(f"  {colour}{hits}/{len(group)}{OFF} {category}")

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.json or RESULTS_DIR / f"{stamp}-{variant}.json"
    out.write_text(
        json.dumps(
            {
                "timestamp": stamp,
                "agent_model": config.AGENT_MODEL,
                "judge_model": judge.model_name,
                "prompt_variant": variant,
                "repeats": args.repeats,
                "passed": passed,
                "total": len(results),
                "results": [asdict(r) for r in results],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"{DIM}→ {out}{OFF}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
