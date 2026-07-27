# DataStream Corp — Strands Agent (AWS PACE GenAI bootcamp)

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

## Links

- **Strands docs** — Python quickstart: https://strandsagents.com/docs/user-guide/quickstart/python/
- **Event dashboard** — https://catalog.us-east-1.prod.workshops.aws/event/dashboard/en-US
- **Workshop IDE** (hosted VS Code with this project) — https://d2tj74ynxyuqnb.cloudfront.net/?folder=/workshop/datastream-corp
- **Workshop instructions** (phases) — https://d1ck76obc96z7d.cloudfront.net/phase/1

---

## One-time setup (shared)

Both variants use one virtualenv at the repo root. It's created with
[uv](https://docs.astral.sh/uv/):

```bash
# from the repo root
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python strands-agents strands-agents-tools mcp openai
```

> The MCP server is spawned automatically by the agent (stdio transport), so you
> don't start it yourself. Each variant reads the `datastream_corp.db` sitting in
> its own folder.

---

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

---

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
   source .env
   PYTHONPATH=. ../.venv/bin/python agent.py
   ```

Notes:
- Temporary (STS) credentials expire after a few hours — refresh `.env` and
  `source` it again when you get `NoCredentialsError`.
- Confirm access anytime with:
  ```bash
  aws bedrock list-foundation-models \
    --query "modelSummaries[?contains(modelId,'anthropic')].modelId" --output text
  ```

---

## What's in each folder

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
