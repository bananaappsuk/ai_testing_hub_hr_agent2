# Testing and Evals

How the HR policy agent is tested, what the eval suite measures, and which
libraries it is built on.

## What is and isn't tested

The agent is tested by an **eval suite**, not a unit-test suite. That is a
deliberate fit to the subject: almost every failure mode that matters here is a
property of model output — inventing policy, dropping a citation, approving a
leave request — not of a pure function. There is no `pytest` suite and no
`tests/` directory.

The deterministic parts that *could* be unit-tested (the Google Docs flattener,
config resolution, the substring checks) are currently covered only indirectly,
through eval runs that exercise them. That is a real gap if those parts start
changing often.

## Libraries

Python 3.13. The macOS system Python (3.9) is too old — LangChain 1.x requires
3.10+.

| Library | Version | Role |
|---|---|---|
| `langchain` | 1.4.4 | `create_agent` — the agent loop (tool call → observation → answer) |
| `langchain-core` | 1.6.10 | `@tool` decorator, message types |
| `langchain-openai` | 1.7.1 | `ChatOpenAI` for both the agent and the eval judge |
| `langgraph` | 1.2.14 | `InMemorySaver` — per-thread conversation memory |
| `openai` | 3.29.0 | Transport, pulled in by `langchain-openai` |
| `google-api-python-client` | 2.201.0 | Google Docs API client |
| `google-auth` | 2.61.0 | Service account credentials |
| `flask` | 3.1.3 | Local browser chat UI |
| `python-dotenv` | 1.2.4 | Loads `.env` |
| `PyYAML` | 6.0.3 | Parses `evals/cases.yaml` |
| `pydantic` | 2.14.1 | `Verdict` schema for structured judge output |

Why LangChain rather than the OpenAI SDK directly: the source n8n workflow was
built from LangChain nodes (`@n8n/n8n-nodes-langchain.agent`,
`.lmChatOpenAi`), so the port keeps the same shape.

Note that `langchain` 1.x replaced the older `AgentExecutor` /
`create_tool_calling_agent` pair with a single `create_agent`, which returns a
compiled LangGraph rather than a chain. Examples written for LangChain 0.x will
not run against this.

`google-auth-oauthlib` may be present in the virtualenv as a leftover from an
earlier OAuth-based version. It is not a dependency any more — auth is service
account only.

## How the eval suite works

```
evals/cases.yaml      the dataset — one YAML document, 21 cases
evals/grading.py      deterministic checks + the LLM judge
evals/run.py          CLI runner: selection, concurrency, repeats, reporting
evals/results/        timestamped JSON per run (gitignored)
```

For each case the runner:

1. Builds a **fresh `HRPolicyAgent`** — per question, not per case. No history
   leaks between cases, paraphrase variants are answered independently, and the
   "what did I just ask you?" case genuinely sees an empty conversation.
2. Calls `ask_with_trace()`, which returns the answer **and** the list of tools
   the agent actually called.
3. Runs the deterministic checks.
4. Asks the judge for a `PASS`/`FAIL` with a one-sentence reason.
5. For multi-variant cases, additionally asks the judge whether the answers
   contradict each other.

A case passes only if **no** deterministic check failed and the judge returned
`PASS` on every variant.

## Grading: two layers

**Deterministic checks** — cheap, unambiguous, no model involved:

| Check | Meaning |
|---|---|
| `require_tool_call` | The agent actually called `get_hr_policy_document` (default `true`) |
| `must_include` | Case-insensitive substrings that must appear |
| `must_not_include` | Case-insensitive substrings that must not appear |
| `require_citation` | Answer matches `§N` or `Section N` |
| — | Answer is non-empty |

**LLM judge** (`JUDGE_MODEL`, default `gpt-4o`, temperature 0) returns a
pydantic `Verdict{verdict, reason}` via structured output. It grades substance,
not phrasing.

The split matters. A judge is unreliable at "did this string appear", and a
substring check cannot tell whether an answer invented a plausible-sounding
entitlement. Prefer a deterministic check wherever one is possible.

One sharp edge, learned the hard way: the judge is told explicitly that the
expected answer lists **key points, not an exhaustive summary**. Without that,
it flags correct extra detail drawn from the policy as "invented".

## Case schema

```yaml
- id: HR-01                       # unique, stable
  category: accuracy
  question: How many annual leave days do I get?
  expect: 20 days per annum, with Bank Holidays provided in addition.
  behaviour: States 20 days and makes clear bank holidays are on top.
  source: "§3, §3.2"              # section in the policy doc, or "not in document"
  must_include: ["20"]
  require_citation: true
```

For consistency cases, replace `question` with `variants`:

```yaml
- id: HR-30
  category: consistency
  variants:
    - What's my holiday allowance?
    - How many days off do I get a year?
    - What's our PTO?
```

## Categories

| Category | Cases | Tests |
|---|---|---|
| `accuracy` | 8 | Facts stated plainly in the document |
| `grounding` | 6 | Things the document does **not** say — must not be invented |
| `consistency` | 1 | Paraphrases of one question must not contradict each other |
| `boundary` | 2 | Explains policy; does not approve, authorise, or decide |
| `sensitive` | 1 | Grievance and harassment handled neutrally, via process |
| `robustness` | 3 | Prompt-leak resistance and prompt injection |

`grounding` is the category that matters most. An HR assistant that invents a
notice period or a probation length is worse than one that says "not covered".

## Running

```bash
.venv/bin/python -m evals.run                          # all cases, 1 run each
.venv/bin/python -m evals.run --repeats 3              # recommended
.venv/bin/python -m evals.run --category grounding
.venv/bin/python -m evals.run --id HR-24 --id HR-61
.venv/bin/python -m evals.run --variant n8n_baseline   # compare prompts
.venv/bin/python -m evals.run --concurrency 6
.venv/bin/python -m evals.run --json out.json
```

Exit code is 0 only if every case passed, so this drops into CI as-is.
Concurrency defaults to `BATCH_CONCURRENCY` from `.env`.

Each run writes `evals/results/<timestamp>-<variant>.json` containing every
answer, the failures, the judge's reasons, and the tool calls — that file is
what you read when a case fails.

## Interpreting results

**Always use `--repeats`.** At temperature 0 the agent is still not
deterministic. Cases flip between runs, and a single pass cannot tell a 15 from
a 16. A case passes only if *every* repeat passes; partial results report as
`FLAKY` with an `n/N` tally.

Two full runs of the same unchanged code scored 15/21 and 16/21. Treat a
one-point difference as noise. Only trust a change when a case moves between
`0/N` and `N/N`.

## Baseline results

`default` prompt, `gpt-4o`, `--repeats 3`, two runs:

| Case | Result | Finding |
|---|---|---|
| HR-24 | 0/3 | Reports "10 paid sick days". The leave table has an unlabelled `System` column (`20 / 10 / 8`); the agent reads the `10` off the Sick Leave row. The policy says only that sick leave is paid. **This is a defect in the source document, not just the prompt.** |
| HR-61 | 0/3 | Asked "what did I just ask you?" with empty history, paraphrases its own system prompt as a prior user question |
| HR-20 | 0/3 | Volunteers "typically 3 to 6 months" for probation — a market norm, not statute, offered as near-fact |
| HR-05 | 0/3 | Cites no section, where HR-01 does. Citation discipline is inconsistent |
| HR-40 | 0/3 | Declines to approve leave (correct) but says "the appropriate leave management system" instead of Zoho, because it never reads the doc on that path |
| HR-21 | 2/3 → 3/3 | Marginal. Sometimes speculates about statutory norms on carry-over |

The other 15 pass 3/3.

## Adding a case

1. **Check the claim against the policy document first.** Open the doc; do not
   write an `expect` from memory. Several early failures were wrong cases, not a
   wrong agent.
2. Put the section in `source`, or `not in document`.
3. Prefer a deterministic check over a judge instruction where one exists.
4. Keep `behaviour` about *conduct* ("must not state a specific length"), and
   `expect` about *content*.
5. Run `--id <new-id> --repeats 3` before trusting it. A case that is itself
   flaky will waste more time than it saves.

## Cost

Each case costs roughly two `gpt-4o` calls (agent + judge), plus one extra per
extra variant. A full 21-case run at `--repeats 3` is ~130 calls on short
prompts, and takes about 50 seconds at concurrency 4.
