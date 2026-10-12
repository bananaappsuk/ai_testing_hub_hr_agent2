# HR Policy Agent

A Python port of the `HR policy agent` n8n workflow: an HR assistant for a UK
accountancy firm that answers leave-entitlement and policy questions, grounded in
the company HR Policy Google Doc.

Built with LangChain and OpenAI, matching the original workflow — the n8n nodes
were themselves LangChain nodes (`@n8n/n8n-nodes-langchain.*`).

## How it maps to the n8n workflow

| n8n node | Here |
|---|---|
| `When chat message received` (chat trigger) | [`hr_agent/cli.py`](hr_agent/cli.py) |
| `AI Agent` + system message | `create_agent` in [`hr_agent/agent.py`](hr_agent/agent.py); prompts in [`hr_agent/config.py`](hr_agent/config.py) |
| `OpenAI Chat Model` | `ChatOpenAI`, model from `AGENT_MODEL` |
| `Get a document in Google Docs` | `get_hr_policy_document` tool → [`hr_agent/policy.py`](hr_agent/policy.py) |

The `Evaluation` / `Get row(s)` nodes are not ported — they form a disconnected
branch with an unset data table. `.env` suggests where that was heading
(`JUDGE_MODEL`, `SHEET_ID`, `SHEET_TAB`, `BATCH_CONCURRENCY`).

## Stack

Python 3.13 (the macOS system 3.9 is too old for LangChain 1.x).

| Library | Role |
|---|---|
| `langchain` / `langchain-core` | `create_agent` agent loop, `@tool` |
| `langchain-openai` | `ChatOpenAI` for the agent and the eval judge |
| `langgraph` | Per-thread conversation memory |
| `google-api-python-client` / `google-auth` | Google Docs via service account |
| `flask` | Local browser chat UI |
| `python-dotenv` | Loads `.env` |
| `PyYAML` / `pydantic` | Eval cases and structured judge output |

Versions and rationale: [docs/TESTING.md](docs/TESTING.md#libraries).

## Setup

Requires Python 3.10+ (macOS system Python 3.9 is too old).

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env     # then fill in OPENAI_API_KEY
```

Google access uses a **service account**, so there is no browser consent flow.
Put the key JSON in the project root and point `GOOGLE_SERVICE_ACCOUNT_FILE` at
it — any `*.json` in the root with `"type": "service_account"` is picked up
automatically if that path is missing. The policy doc must be shared with the
service account's email (Viewer is enough); the agent says so explicitly if not.

To run with no Google access at all, set `HR_POLICY_FILE=./hr_policy.md`.

## Usage

### Browser

```bash
.venv/bin/python -m hr_agent.web          # http://127.0.0.1:8000
WEB_PORT=8010 .venv/bin/python -m hr_agent.web
```

The local equivalent of the n8n chat trigger's hosted page. Each browser gets
its own conversation (session id in `localStorage`); **New conversation** clears
it. The server binds to `127.0.0.1` only and has no authentication — it speaks to
the policy document with your service account, so do not expose it on a network.

### Terminal

```bash
.venv/bin/python -m hr_agent.cli                                  # chat
.venv/bin/python -m hr_agent.cli "how much leave do I get?"       # one-shot
```

```python
from hr_agent import HRPolicyAgent

agent = HRPolicyAgent()
print(agent.ask("What checks are needed before I start?"))
print(agent.ask("And if they're on a visa?"))   # history is kept
```

## Prompt variants

`PROMPT_VARIANT` selects the system prompt, so variants can be compared on the
same questions. The difference is real — asked about sick pay:

- `default` — "…under the *Leave and Holidays* section. However, the document does
  not specify the exact terms… confirm with HR directly."
- `n8n_baseline` — "Employees are paid while on sick leave."

Add variants to `PROMPT_VARIANTS` in [`hr_agent/config.py`](hr_agent/config.py).

## Evals

```bash
.venv/bin/python -m evals.run                          # all cases, 1 run each
.venv/bin/python -m evals.run --repeats 3              # 3 runs each (recommended)
.venv/bin/python -m evals.run --category grounding
.venv/bin/python -m evals.run --id HR-24
.venv/bin/python -m evals.run --variant n8n_baseline   # compare prompts
```

Cases live in [`evals/cases.yaml`](evals/cases.yaml); every `expect` is grounded
in a named section of the live policy document. Results are written to
`evals/results/` (gitignored). Exit code is 0 only if every case passes.

**Grading is two layers, both of which must pass.** Deterministic checks (the
policy tool was called, required substrings present, forbidden substrings absent,
a section was cited) and an LLM judge on `JUDGE_MODEL` for semantic agreement.
Deterministic checks catch what a judge is unreliable at; the judge catches
"right facts, wrong behaviour".

**Use `--repeats`.** At temperature 0 the agent is still not deterministic — some
cases flip between runs, so a single pass cannot distinguish a 15 from a 17. A
case counts as passing only if *every* repeat passes; partial results are
reported as `FLAKY`.

| Category | Tests |
|---|---|
| `accuracy` | Facts stated plainly in the document |
| `grounding` | Things the document does **not** say — must not be invented |
| `consistency` | Paraphrases of one question must not contradict each other |
| `boundary` | Explains policy; does not approve, authorise, or decide |
| `sensitive` | Grievance and harassment handled neutrally, via process |
| `robustness` | Prompt-leak resistance and injection |

Full documentation — grading layers, case schema, baseline results, known
findings, how to add a case — is in [docs/TESTING.md](docs/TESTING.md).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | Required |
| `AGENT_MODEL` | `gpt-4o` | Chat model |
| `AGENT_TEMPERATURE` | `0` | Sampling temperature |
| `PROMPT_VARIANT` | `default` | Which system prompt to use |
| `POLICY_DOC_ID` | the firm's HR Policy doc | Google Doc to read |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `service_account.json` | Service account key |
| `DOC_CACHE_SECONDS` | `300` | Seconds before re-fetching the document |
| `HR_POLICY_FILE` | unset | Read policy from a local file instead |
| `WEB_PORT` | `8000` | Port for the browser UI |
| `JUDGE_MODEL` | `gpt-4o` | Model used to grade evals |
| `BATCH_CONCURRENCY` | `3` | Parallel eval cases |

## Notes

- The document is fetched once per cache window, not once per question.
- If the document cannot be read, the tool returns an error string to the model
  rather than raising, so the agent says it cannot see the policy instead of
  answering from general employment-law knowledge.
- `.env` and the service account key are gitignored. **Keep them out of version
  control** — the key file is a live private credential.
