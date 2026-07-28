# DataStream Corp — agent playground (AWS PACE GenAI bootcamp)

Two unrelated agent builds kept side by side, to compare what a framework gives
you against what a first-party SDK gives you.

| | [`aws-Strands/`](aws-Strands) + [`lm-studio/`](lm-studio) | [`claude-sdk/`](claude-sdk) |
|---|---|---|
| Framework | [Strands](https://strandsagents.com) | [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk) |
| Shape | Multi-agent orchestrator + specialists | Single unattended service |
| Config | In code | Loaded from disk (`CLAUDE.md`, hooks, skills, subagents) |
| Task | Answer questions about the data | Find data-integrity anomalies in it (`--freeform` also answers plain questions) |
| Specialists | `data_agent` + `weather_agent` as `@tool`-wrapped nested `Agent`s | `db-analyst` + `weather` as `.claude/agents/*.md`, invoked via `Task` |
| Database | remote shared DB over HTTPS | `datastream_corp.db`, vendored copy |
| MCP server | `mcp_server.py` (streamable-http, CloudFront proxy) | own copy (stdio, local SQLite) |
| Write guard | `BeforeToolCallEvent` + `is_destructive()` | `PreToolUse` hook + same keyword set |

**Same data, same MCP tools, same write-guard problem** — so this is a genuine
like-for-like comparison of the two approaches. `claude-sdk/` keeps its own
copies of `mcp_server.py` and `datastream_corp.db` rather than reaching across:
the Strands folders are workshop exercises meant to be edited in place, and the
service shouldn't change behaviour when someone completes a lab step.

The two `mcp_server.py` files **have since diverged** — Phase 2 moved the Strands
one onto streamable-http and a remote database, while the `claude-sdk/` copy still
speaks stdio to the local `.db`. They serve the same data either way. See the
difference with `diff claude-sdk/mcp_server.py aws-Strands/mcp_server.py`.

They still share no code — but they do share **one virtualenv** at the repo
root (Python 3.13), so there is a single `uv pip install` to run. See
[One-time setup](#one-time-setup-all-three-variants).

## Links

- **Working notes** — two-environment workflow, deploy order, and the gotchas that cost real time: [CLAUDE.md](CLAUDE.md)
- **AgentCore deployment** — Phase 2: MCP runtime, Gateway, Memory, deployed agent: [docs/agentcore-deployment.md](docs/agentcore-deployment.md)
- **Strands vs. the raw API** — framework vs. hand-rolled loop, and what it costs: [docs/strands_vs_raw_api.md](docs/strands_vs_raw_api.md)
- **Strands vs. the Agent SDK** — which one for production: cost at volume, concurrency, observability, version risk: [docs/strands-vs-agent-sdk.md](docs/strands-vs-agent-sdk.md)
- **Claude Agent SDK write-up** — config-loading model, hooks, costs: [claude-sdk/README.md](claude-sdk/README.md)
- **Strands docs** — Python quickstart: https://strandsagents.com/docs/user-guide/quickstart/python/
- **Event dashboard** — https://catalog.us-east-1.prod.workshops.aws/event/dashboard/en-US
- **Workshop IDE** (hosted VS Code with this project) — https://d2tj74ynxyuqnb.cloudfront.net/?folder=/workshop/datastream-corp
- **Workshop instructions** (phases) — https://d1ck76obc96z7d.cloudfront.net/phase/1

## One-time setup (all three variants)

One virtualenv at the repo root serves all three variants. Created with
[uv](https://docs.astral.sh/uv/):

```bash
# from the repo root
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python \
  strands-agents strands-agents-tools mcp openai \
  claude-agent-sdk pydantic fastapi uvicorn
```

The first line of packages is Strands, the second is the Agent SDK service
(`fastapi`/`uvicorn` only if you want its HTTP mode).

Every command below uses a **relative** path to that venv (`.venv/bin/python`,
or `../.venv/bin/python` from inside a variant folder), so they work unchanged
in the [hosted workshop IDE](https://d2tj74ynxyuqnb.cloudfront.net/?folder=/workshop/datastream-corp)
as well as locally. `claude-sdk/.mcp.json` uses the same relative path — **do
not replace it with an absolute one**, or the workshop environment breaks.

> The workshop material targets Python 3.12; this repo runs 3.13, verified end
> to end — a full `lm-studio/agent.py` run (orchestrator, MCP database queries,
> weather specialist) completes normally. `uv venv --python 3.12 .venv` is a
> safe fallback; the Agent SDK supports 3.10+, so `claude-sdk/` works there too.

> MCP servers are spawned automatically by each agent over stdio — you never
> start one yourself.

### IDE interpreter (macOS, optional)

The IDE interpreter does **not** have to be the one you run with — it only needs
the same packages so imports resolve. Pointing it at the project `.venv` works,
but that path is fragile: recreating the venv, or switching Python version,
invalidates the IDE's cached `site-packages` root and every import goes red.

A stable alternative on macOS — a Python outside the repo that nothing here can
move or delete:

```bash
brew install python@3.13
/opt/homebrew/bin/python3.13 -m pip install --break-system-packages \
  strands-agents strands-agents-tools mcp openai \
  claude-agent-sdk pydantic fastapi uvicorn
```

Then point Rider/PyCharm at `/opt/homebrew/bin/python3.13` (Settings →
Languages & Frameworks → Python → Python Interpreter → Add Interpreter →
Existing). Homebrew's Python is [PEP 668](https://peps.python.org/pep-0668/)
"externally managed", hence `--break-system-packages`; those packages are shared
across every project on the machine, which is the trade for stability.

**Editor-side only.** Runtime still uses `.venv`, so the workshop environment is
unaffected.

---

# 1. Strands multi-agent app

A [Strands](https://strandsagents.com) multi-agent app for a fictional company,
"DataStream Corp". An orchestrator delegates to two specialists:

- **data_agent** — answers questions about the company database (departments,
  employees, projects, budgets, headcounts) via an **MCP server** over a local
  SQLite DB (`datastream_corp.db`).
- **weather_agent** — answers weather questions via the free Open-Meteo API.

The same code runs two ways — the only difference is the model backend:

| Folder | Model backend | Needs | Quality | Speed |
|--------|---------------|-------|---------|-------|
| [`aws-Strands/`](aws-Strands) | Amazon Bedrock (cloud Claude) | AWS credentials with Bedrock access | best | fast |
| [`lm-studio/`](lm-studio) | Local model via LM Studio (Gemma 4 E4B) | LM Studio running locally | good | fast, fully offline |

`aws-Strands/` is the pristine workshop original (Strands defaults to Bedrock).
`lm-studio/` is a copy wired to a local model so it runs off AWS — see
[`lm-studio/local_model.py`](lm-studio/local_model.py).

Both read the `datastream_corp.db` sitting in their own folder. Setup is the
shared one above.

## Run the LM Studio (local) agent — no AWS needed

1. **Install & start LM Studio**, then start its local server and load the model:
   ```bash
   lms server start
   lms load google/gemma-4-e4b        # fast, reliable tool-caller, fits 24GB RAM
   ```
   The default model is `google/gemma-4-e4b`. Override it with env vars if you
   want a different one (e.g. the more precise but slower 26B):
   ```bash
   export LOCAL_MODEL_ID=google/gemma-4-26b-a4b
   export LOCAL_MODEL_URL=http://localhost:1234/v1   # default
   ```

2. **Run the demo:**
   ```bash
   cd lm-studio
   PYTHONPATH=. ../.venv/bin/python agent.py
   ```

Notes:
- A `TURN_LIMIT` safety cap bounds the agent loop so a weak local model can't run
  away in an infinite tool-calling loop.
- On a 24 GB Mac, keep only one model loaded at a time (the 26B is ~15 GB).

## Run the AWS Bedrock (cloud) agent

Needs AWS credentials for an account that has **Bedrock + Anthropic Claude model
access enabled** (region `us-east-1` recommended).

1. **Provide credentials.** Put them in a local, gitignored `aws-Strands/.env`
   (the `.env` file is ignored by git — secrets stay off the repo):
   ```bash
   # aws-Strands/.env
   export AWS_DEFAULT_REGION="us-east-1"
   export AWS_ACCESS_KEY_ID="..."
   export AWS_SECRET_ACCESS_KEY="..."
   export AWS_SESSION_TOKEN="..."      # only for temporary/STS credentials
   ```

2. **Run the demo** (source the creds in the *same* shell):
   ```bash
   cd aws-Strands
   set -a; source .env; source ../.env; set +a
   PYTHONPATH=. ../.venv/bin/python agent.py
   ```

   The agent talks to its MCP server over streamable-http and picks the endpoint
   in this order — it prints the one it chose to stderr on startup:

   | | Endpoint |
   |---|---|
   | `MCP_URL` set | that URL, always wins |
   | `GATEWAY_ID` in root `.env` | the deployed AgentCore Gateway, with a token minted from `.env` |
   | neither | `http://localhost:8000/mcp` |

   For the localhost case, start the server first in another terminal:
   ```bash
   cd aws-Strands && ../.venv/bin/python mcp_server.py
   ```
   Once Phase 2 is deployed, `GATEWAY_ID` is already in `.env` and no local
   server is needed. See [docs/agentcore-deployment.md](docs/agentcore-deployment.md).

Notes:
- Temporary (STS) credentials expire after a few hours — refresh `.env` and
  `source` it again when you get `NoCredentialsError`.
- Confirm access anytime with:
  ```bash
  aws bedrock list-foundation-models \
    --query "modelSummaries[?contains(modelId,'anthropic')].modelId" --output text
  ```

## What's in each Strands folder

| File | What it is |
|------|-----------|
| `agent.py` | The complete demo: orchestrator + data & weather specialists (runs 3 sample prompts). |
| `agent-1.py` … `agent-5.py` | The workshop's progressive exercises — single agent → sessions/memory → summarizing context → approval hooks → structured output. |
| `mcp_server.py` | MCP server exposing `query_db` and `list_schema` tools over the SQLite DB. |
| `testdb.py` | Quick sanity check that the database is readable. |
| `datastream_corp.db` | The SQLite database. |
| `lm-studio/local_model.py` | *(lm-studio only)* `get_model()` — points Strands at LM Studio's OpenAI-compatible server. |

The two folders share the same `agent-*.py` / `mcp_server.py` logic; in
`lm-studio/` every `Agent(...)` is created with `model=get_model()` instead of
Strands' Bedrock default.

---

# 2. Claude Agent SDK ops monitor

An unattended monitoring service on the [Claude Agent
SDK](https://code.claude.com/docs/en/agent-sdk) that **reuses Claude Code
configuration from disk** rather than rebuilding it in a framework — `CLAUDE.md`,
`.claude/settings.json` hooks, `.claude/skills/`, `.claude/agents/`, `.mcp.json`.
No hand-rolled agent loop, tool dispatch, context management, or permission
system.

It audits `datastream_corp.db` for three data-integrity problems that are
genuinely in the workshop data — projects long past their `end_date` but still
`active`, assignments left open on a `completed` project, and department payroll
running 5–11× the stated budget — and returns a schema-validated incident
report.

A `PreToolUse` hook enforces, in code, that it can never write to the database
without human sign-off. That's the sharp edge of the comparison: `query_db` runs
reads *and* writes through one tool, so "reads only" can't be expressed as a
tool-name permission rule in either framework — both have to inspect the SQL.

Full write-up — the SDK's config-loading model, hook semantics, invocation
modes, cost per run, and when to use `claude -p` instead:
**[claude-sdk/README.md](claude-sdk/README.md)**.

## Setup

Nothing extra — it uses the shared root venv from
[One-time setup](#one-time-setup-all-three-variants). No seeding step either:
the database is a copy of the workshop one and the anomalies are already in it.

No Claude Code CLI or Node install needed — the SDK ships its own binary.
Credentials come from the environment: `ANTHROPIC_API_KEY`, or your existing
Claude Code login as a fallback.

## Run it

```bash
cd claude-sdk

# CLI one-shot — JSON on stdout, transcript on stderr
../.venv/bin/python service.py
../.venv/bin/python service.py "Check for overdue projects only."

# Prove the safety hook blocks a write even under bypassPermissions
../.venv/bin/python verify_hook.py

# HTTP endpoint
../.venv/bin/uvicorn http_app:app --port 8080
curl -sX POST localhost:8080/run -H 'content-type: application/json' \
     -d '{"prompt":"Check for overdue projects."}' | jq
```

Useful env vars: `MONITOR_MODEL` (e.g. `claude-haiku-4-5`, ~5× cheaper),
`MONITOR_MAX_USD` (hard per-run budget, default `1.00`), `MONITOR_PROVIDER`
(`anthropic` | `bedrock` | `vertex`), `MONITOR_LOG_LEVEL` (`DEBUG` also shows
the CLI subprocess's stderr).

Like the Strands variants, a run streams a live transcript to **stderr** —
the model's narration and every tool call — while **stdout** stays pure JSON.
So `| jq` works, and `2>/dev/null` gives you just the result. Everything is
also appended to `claude-sdk/logs/audit.jsonl`.

A run costs roughly **$0.09–$0.37** on Opus 5.
