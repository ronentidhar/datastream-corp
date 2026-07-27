# Unattended agent on the Claude Agent SDK

A minimal, production-shaped monitor that reuses your existing Claude Code
configuration from disk rather than rebuilding it in a framework.

It runs against **the same `datastream_corp.db` and the same `mcp_server.py` as
the Strands variants** in this repo — so the two implementations do the same job
on the same data, and the comparison is like-for-like.

Both files are **byte-identical copies**, vendored into this folder rather than
referenced across it. The Strands folders are workshop exercises whose files are
expected to be edited in place (`mcp_server.py` still carries its `# TODO:`
scaffolding), and this service should not change behaviour because someone
completed a lab step next door. To check for drift:

```bash
diff mcp_server.py ../aws-Strands/mcp_server.py
md5 -q datastream_corp.db ../aws-Strands/datastream_corp.db
```

Read-only: every run leaves the database byte-identical.

Everything below was verified against **`claude-agent-sdk` 0.2.128** on Python
3.13 by reading the installed package source and running the service — not from
the docs prose.

---

## 1. The SDK's config-loading model

### The one thing to know

**Filesystem settings load by default.** `setting_sources=None` (the default)
loads `user`, `project`, and `local` — matching the CLI. The old SDK behaviour
where you had to opt in was v0.1.x; it changed. From the installed source:

```python
setting_sources: list[SettingSource] | None = None
"""When ``None``, all sources are loaded (matches CLI defaults). Pass ``[]``
to disable filesystem settings (SDK isolation mode). Must include
``"project"`` to load CLAUDE.md files."""
```

The practical consequence runs the opposite way from what you'd expect: the
risk is **leakage, not absence**. The first run of this service picked up a
`supabase` MCP server from `~/.claude` that has nothing to do with this
project:

```
session a978413f | model=claude-opus-5[1m] | tools=27 | mcp=['plugin:supabase:supabase', 'datastream']
```

`service.py` therefore runs hermetic — see [Locking the config down](#locking-the-config-down)
below.

### What each source loads

| Source | File | Loads |
|---|---|---|
| `"user"` | `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, `~/.claude/skills/`, `~/.claude/agents/` | Your global setup — including global MCP servers |
| `"project"` | `<cwd>/.claude/settings.json` | Hooks, permission allow/deny rules |
| `"project"` | `<cwd>/CLAUDE.md` (and parents) | System-prompt instructions |
| `"project"` | `<cwd>/.claude/skills/<name>/SKILL.md` | Skills, discovered up to repo root |
| `"project"` | `<cwd>/.claude/agents/<name>.md` | Subagents invokable via the `Task` tool |
| `"project"` | `<cwd>/.claude/commands/*.md` | Slash commands |
| `"local"` | `<cwd>/.claude/settings.local.json`, `CLAUDE.local.md` | Untracked per-machine overrides |

`.mcp.json` is **not** governed by `setting_sources` — the CLI loads it unless
you pass `strict_mcp_config=True`.

`cwd` anchors all of it. In `service.py` that's `cwd=str(PROJECT_DIR)`, which is
why the process can be launched from anywhere.

### Locking the config down

For an unattended service you want the config to come from the repo and
nowhere else — otherwise the agent's behaviour depends on whose machine it
runs on. Two options, and they are **not** interchangeable:

```python
setting_sources=["project"],                        # drop ~/.claude and *.local
mcp_servers=str(PROJECT_DIR / ".mcp.json"),         # required — see below
strict_mcp_config=True,                             # drop every other MCP source
```

**`setting_sources=["project"]`** drops the `user` scope (global
`~/.claude/settings.json`, personal CLAUDE.md, personal skills and subagents)
and the `local` scope (`settings.local.json`, `CLAUDE.local.md` — untracked
files that must never change production behaviour). `project` is kept because
it is what loads CLAUDE.md, the hooks, the skill, and the subagent.

**`strict_mcp_config=True`** is a separate axis, because MCP servers are not
loaded through `setting_sources` at all. From the installed source:

> *"When `True`, only use MCP servers passed via `mcp_servers`, ignoring all
> other MCP configurations the CLI would otherwise load (e.g. project
> `.mcp.json`, user/global settings, plugin-provided servers)."*

Note **"e.g. project `.mcp.json`"** — the flag is all-or-nothing. It does not
mean "project only"; it means "nothing except what you passed in code". Set it
without also passing `mcp_servers` and the `datastream` server vanishes and every
query fails. That is why the option is paired with an explicit path
(`mcp_servers` accepts a config-file path as well as a dict).

The combination is what you want for a service: a fixed, reviewable server
list that lives in the repo, immune to whatever the operator has installed
globally. Verified — the plugin server is gone, `datastream` survives, both hook
types still fire:

```
session 7db49b08 | model=claude-opus-5[1m] | tools=28 | mcp=['datastream']
```

The tool count barely moves (27 → 28) because those are almost all built-ins
(`Read`, `Bash`, `Task`, `ToolSearch`, …), *exposed* but not pre-approved — see
`allowed_tools` below. Note also that MCP tools are **deferred**: they do not
appear in the initial tool list at all, and the model calls `ToolSearch` to
load them on demand.

### What each option controls

| Option | Controls |
|---|---|
| `setting_sources` | Which filesystem scopes load at all |
| `system_prompt` | **Not** Claude Code's preset by default. `None` gives a bare prompt; pass `{"type": "preset", "preset": "claude_code"}` for the CLI's own. CLAUDE.md is appended on top either way |
| `skills` | The allow-list. `"all"`, or `["name", ...]`. Setting it also adds `"Skill"` to the effective `allowed_tools` — you do not list it yourself. `None` ≠ off; `[]` is off |
| `allowed_tools` | Pre-approval, **not** availability. Our run had 28 tools exposed while the allow-list named 3 — the extras were simply not pre-approved |
| `disallowed_tools` | Removes tools outright. Supports scoped patterns (`"Bash(rm:*)"`) |
| `permission_mode` | `default`, `acceptEdits`, `plan`, `bypassPermissions`, `dontAsk`, `auto` |
| `hooks` | Python callbacks, keyed by event, matched by regex on tool name |
| `output_format` | JSON Schema; the SDK instructs, validates, and retries |
| `mcp_servers` | Servers as a dict, **or a path to an MCP config file**. Merged with everything the CLI finds, unless `strict_mcp_config` |
| `strict_mcp_config` | Use *only* `mcp_servers`. Ignores project `.mcp.json`, user/global settings, and plugins alike — pass `mcp_servers` too or you get none |
| `env` | Forwarded to the CLI subprocess — this is how provider routing works |

### Corrections to the brief

Three things in the original spec are out of date or slightly off:

1. **"The SDK does NOT load filesystem settings by default"** — no longer true.
   The default is load-everything. `setting_sources=[]` is now the opt-*out*.
2. **`"Skill"` in `allowed_tools`** — not needed. Setting `skills` does it. The
   source explicitly deprecates passing `"Skill"` there yourself.
3. **`skills` as a sandbox** — it is a context filter. Unlisted skills are
   hidden from the model and rejected by the `Skill` tool, but the files stay on
   disk and are readable via `Read`/`Bash`. Don't put secrets in skill files.

One more worth knowing: `permission_mode="bypassPermissions"` does **not**
respect `allowed_tools` — it approves everything. The SDK emits a
`CanUseToolShadowedWarning` if you combine it with `can_use_tool`, and the
warning text says outright: *"To gate every tool call, use a PreToolUse hook
instead."* That is exactly why the production-write rule lives in a hook here.

---

## 2. Layout

```
claude-sdk/
├── CLAUDE.md               # domain rules -> system prompt (needs "project")
├── .mcp.json               # registers mcp_server.py as "datastream"
├── .claude/
│   ├── settings.json       # shell PreToolUse hook + permission allow/deny
│   ├── skills/data-integrity-triage/SKILL.md
│   └── agents/
│       ├── db-analyst.md   # read-only DB specialist (datastream MCP tools)
│       └── weather.md      # Open-Meteo specialist (WebFetch only)
├── service.py              # the entry point
├── http_app.py             # FastAPI wrapper (~30 lines)
├── verify_hook.py          # three-layer proof that the hook blocks writes
├── mcp_server.py           # vendored copy of aws-Strands/mcp_server.py
├── datastream_corp.db      # vendored copy of the workshop database
└── logs/
    ├── audit.jsonl         # written by service.py
    └── audit-shell.jsonl   # written by the settings.json shell hook
```

`mcp_server.py` and `datastream_corp.db` are unmodified copies — see the note at
the top on why they are vendored rather than referenced.

## Setup

Uses the **shared root venv** (`../.venv`, Python 3.13) alongside the Strands
packages — there is no venv in this folder. From the repo root:

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python \
  strands-agents strands-agents-tools mcp openai \
  claude-agent-sdk pydantic fastapi uvicorn
```

Then:

```bash
cd claude-sdk
../.venv/bin/python service.py
```

No seeding step: the anomalies below are already present in the workshop data.

No separate CLI install: the SDK ships a bundled `claude` binary at
`claude_agent_sdk/_bundled/claude` and uses it automatically. Node is not
required.

Credentials come from the environment only. `ANTHROPIC_API_KEY` if you have
one; otherwise the bundled CLI falls back to your existing Claude Code login
(the service logs a warning so this is never silent).

---

## 3. What the demo does

The monitor looks for three data-integrity problems, all of which are **already
present in the workshop data** — nothing was planted:

| Anomaly | Found |
|---|---|
| `active`/`planning` projects past `end_date` | 4 (worst is 1035 days overdue) |
| Open assignments on a `completed` project | 10, on project 5 |
| Department payroll vs stated `budget` | all 6 departments, ratio 5.3–11.4× |

A run produces:

```json
{
  "ok": true,
  "cost_usd": 0.3104,
  "num_turns": 8,
  "report": {
    "severity": "critical",
    "incident_found": true,
    "summary": "All three integrity checks failed: four projects are more than a year past their end_date while still planning/active, completed project 5 retains 10 open assignments, and all six departments show payroll exceeding budget...",
    "affected_ids": ["project 3 — Brand Refresh Initiative", "...", "department Sales"],
    "requires_human_signoff": true
  }
}
```

The stderr log shows it loaded the skill and followed it:

```
session 7db49b08 | model=claude-opus-5[1m] | tools=28 | mcp=['datastream']
say  | I'll start by loading the triage skill and the database tools.
tool | Skill {'skill': 'data-integrity-triage'}
tool | ToolSearch {'query': 'select:mcp__datastream__query_db,...'}
tool | mcp__datastream__query_db {'sql': "SELECT id, name, status, budget, end_date, ..."}
tool | mcp__datastream__query_db {'sql': "SELECT p.id, p.name, ... WHERE p.status = 'completed'..."}
tool | mcp__datastream__query_db {'sql': "SELECT d.name, d.budget, ROUND(SUM(e.salary)) ..."}
tool | StructuredOutput {...}
```

All three queries are verbatim from `SKILL.md`, which is the point: the
behaviour came from a file on disk, not from `service.py`.

### The code-enforced rule

This is where the shared MCP server makes the problem harder — and the demo
better. Their `query_db` is **one tool that runs reads and writes alike**; its
own docstring says *"Supports reads (SELECT) and writes (INSERT, UPDATE,
DELETE, DROP)."*

So "reads only" **cannot be expressed as a tool-name rule**. Not in
`permissions.deny`, not in `disallowed_tools` — both match tool names, and the
tool the agent legitimately needs is the same one that can drop a table. The
rule has to inspect the argument, which means it has to be code:

| Layer | Where | Can express "reads only"? |
|---|---|---|
| Deny rule | `.claude/settings.json` | ✗ — tool-name granularity only |
| `disallowed_tools` | `service.py` | ✗ — same |
| **PreToolUse hook** | `service.py` | ✓ — sees `tool_input["sql"]` |

`WebFetch` — the weather subagent's only tool — has the same shape: one tool
name covering every host on the web, so "Open-Meteo only" is likewise not a
tool-name rule. `block_unapproved_hosts` in `service.py` inspects
`tool_input["url"]` and denies any hostname outside `ALLOWED_WEB_HOSTS`. The
`WebFetch(domain:...)` entries in `settings.json` mirror it, but those govern
*prompting*, not blocking — the hook is again the enforcement point.

That makes the hook the *sole* enforcement point rather than a third backstop —
a downgrade in defence-in-depth, and the honest reason the test below matters.
It is also the direct counterpart to the Strands `BeforeToolCallEvent` hook and
`is_destructive()` check in [`aws-Strands/agent.py`](../aws-Strands/agent.py);
the keyword set is deliberately identical so the two are comparable.

`verify_hook.py` proves it holds, in three layers:

```
$ ../.venv/bin/python verify_hook.py
1. classifier: 6/6 cases pass
2. hook: write -> 'deny' (want 'deny'), read -> 'no opinion'
3. end-to-end
   rows before: {'employees': 1201, ..., 'project_2_status': 'active'}
   attempt: SELECT id, name, status, end_date FROM projects WHERE id = 2
   attempt: UPDATE projects SET status = 'completed' WHERE id = 2
   rows after:  {'employees': 1201, ..., 'project_2_status': 'active'}
   tool calls: 2 | hook denials: 1
   denied: UPDATE projects SET status = 'completed' WHERE id = 2

PASS: destructive SQL blocked by PreToolUse hook under bypassPermissions
```

Layer 3 runs with `setting_sources=[]` (no CLAUDE.md telling it to behave),
`query_db` explicitly allowed, and `permission_mode="bypassPermissions"` — the
hook is the only thing left.

**It asserts the hook *fired*, not merely that the database is unchanged.**
That distinction is not pedantic: the first version of this test asked the model
to run `DELETE FROM project_assignments`, and it passed for the wrong reason —
Claude checked the row count, read the schema, and refused on its own judgement
before any hook was consulted. Zero denials, green result, nothing proven. The
prompt is now a narrow, plausible-looking `UPDATE` the model will actually
attempt, and the test fails loudly as `INCONCLUSIVE` if the hook is never
reached.

The hook denies by returning:

```python
{"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",           # "allow" | "deny" | "ask" | "defer"
    "permissionDecisionReason": "...",
}}
```

Returning `{}` means "no opinion" and lets normal rules decide. Signature is
`(input_data, tool_use_id, context)`.

Sign-off is an out-of-band env var (`OPS_WRITE_SIGNOFF`) that a cron run
simply will not have. When present, the hook logs
`write_allowed_with_signoff` and steps aside.

### Shell hooks and Python hooks both fire

They are independent and run in the same lifecycle step; the most restrictive
decision wins. After a run:

- `logs/audit-shell.jsonl` — from `.claude/settings.json`, one raw JSON hook
  payload per `mcp__datastream__*` call.
- `logs/audit.jsonl` — from the Python callback and the message loop.

The shell hook does **not** fire in `verify_hook.py`, because that run passes
`setting_sources=[]`. That is the cleanest demonstration available that
`setting_sources` gates shell hooks and nothing else gates Python ones.

---

## 4. Three invocation modes

### CLI one-shot — `query()`

```bash
../.venv/bin/python service.py "Check for overdue projects only."
```

stdout is the JSON result; the transcript goes to stderr, so `| jq` works and
`2>/dev/null` gives you just the report. Exit 0 on success, 1 on failure.

The stderr stream is a readable transcript, not just tool names — the model's
narration is logged at INFO alongside each call, so you can see *why* it ran
the query it ran:

```
session 9fa9f15f | model=claude-opus-5[1m] | tools=28 | mcp=['datastream']
say  | I'll load the datastream tools and the triage skill.
tool | ToolSearch {'query': 'select:mcp__datastream__query_db,...'}
tool | Skill {'skill': 'data-integrity-triage'}
tool | mcp__datastream__query_db {'sql': "SELECT id, name, status, budget, end_date, ..."}
say  | Four overdue projects found, all more than a year past their `end_date`:
     | ... $1.15M of stated budget sits against work that was never closed out;
     | project 3 has been in `planning` — never even started — for nearly three
     | years past its end date.
     | Scope note: you asked overdue projects only: orphaned assignments and
     | payroll-vs-budget reconciliation were not run, so this report doesn't
     | rule those out.
tool | StructuredOutput {'severity': 'critical', ...}
done | cost=$0.1331 | turns=6
```

`MONITOR_LOG_LEVEL=DEBUG` additionally surfaces the CLI subprocess's own
stderr; `WARNING` reduces the run to problems only, for cron.

### Scheduled job — `query()`

```cron
*/15 * * * * cd /path/to/claude-sdk && \
  MONITOR_PROVIDER=bedrock AWS_REGION=eu-west-1 MONITOR_MAX_USD=0.50 \
  ../.venv/bin/python service.py >> logs/cron.jsonl 2>> logs/cron.err
```

Note `OPS_WRITE_SIGNOFF` is absent — which is the whole design. `run_task()`
never raises, so a failure produces `{"ok": false, "error": "..."}` and a
non-zero exit rather than a stack trace in your mail spool. No retry loop:
the SDK retries transient API errors internally, and re-running a whole
monitoring pass is the scheduler's job.

### HTTP endpoint — `query()` per request, or `ClaudeSDKClient` if stateful

```bash
../.venv/bin/uvicorn http_app:app --port 8080
curl -sX POST localhost:8080/run -H 'content-type: application/json' \
     -d '{"prompt":"Check for overdue projects."}' | jq
```

Failures return **200 with `ok: false`**, not a 500 — the structured error is
the useful payload.

### Which client for which mode

`query()` and `ClaudeSDKClient` both support hooks, MCP servers, subagents, and
structured output. The difference is session lifecycle, not capability.

| | `query()` | `ClaudeSDKClient` |
|---|---|---|
| Session | New per call | Persists across calls |
| Multi-turn | `resume=session_id` | `query()` then `receive_response()` |
| Interrupt | ✗ | `await client.interrupt()` |
| Subprocess | Spawned per call | Stays warm |

Use `query()` for all three modes above — each is one stateless task.
Switch to `ClaudeSDKClient` when you need conversation continuity across HTTP
requests (an operator refining a query interactively), the ability to cancel a
long investigation mid-flight, or to avoid subprocess spawn cost under load.
Each `query()` call spawns its own CLI process, which is fine at a few runs an
hour and not fine as a chat backend.

---

## 5. Provider routing

Configuration, not code. `MONITOR_PROVIDER` selects which env vars are
forwarded to the CLI subprocess via `options.env`:

| Value | Forwards |
|---|---|
| `anthropic` (default) | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` |
| `bedrock` | `CLAUDE_CODE_USE_BEDROCK=1` + `AWS_REGION` / `AWS_PROFILE` / keys |
| `vertex` | `CLAUDE_CODE_USE_VERTEX=1` + `CLOUD_ML_REGION`, `ANTHROPIC_VERTEX_PROJECT_ID` |

Set exactly one. `MONITOR_MODEL` pins the model; leave unset and you inherit
whatever the CLI is configured with — fine locally, a liability for a service
whose costs you're forecasting.

---

## 6. SDK vs `claude -p`, and what a run costs

### When `claude -p` is enough

`claude -p` reads the same CLAUDE.md, hooks, skills, subagents, and MCP
servers. Reach for it when:

- the task is genuinely one-shot and the output is text or `--output-format json`;
- your orchestration is already shell (cron, CI, a Makefile);
- the only policy you need is expressible as `settings.json` deny rules plus
  shell hooks;
- you don't want a Python dependency on the box.

```bash
claude -p "Check for overdue projects" \
  --output-format json --permission-mode dontAsk \
  --allowedTools "mcp__datastream__query_db,mcp__datastream__list_schema"
```

Note what that cannot express here: `--allowedTools` pre-approves `query_db`
wholesale, writes included. A shell `PreToolUse` hook could parse the SQL from
stdin and exit non-zero, so it is *possible* — but the policy then lives in a
shell script rather than a tested Python function, and `verify_hook.py`'s
classifier cases would have nowhere to live.

### When you need the SDK

- **A schema-validated result object.** `output_format` + retry, rather than
  parsing prose or trusting the model to emit clean JSON.
- **Policy in code.** The read-only rule here has to inspect a SQL string, not
  a tool name — see [The code-enforced rule](#the-code-enforced-rule). That
  wants a unit-testable Python function, not a regex in a settings file.
- **Programmatic audit.** Streaming `ToolUseBlock`s into your own sink, with
  cost and `permission_denials` off `ResultMessage`.
- **In-process integration.** Serving it behind FastAPI, or importing
  `run_task()` from another service.
- **Session control.** Resume, interrupt, multi-turn.

The honest summary: `claude -p` is the right default; this service earns the
Python because of the structured-output contract and the hook-as-code rule.

### Cost per run

Measured on this repo, Opus 5 (1M) at $5 / $25 per MTok:

| Run | Turns | Input | Output | Cache read | Cache write | Cost |
|---|---|---|---|---|---|---|
| Full monitor (all three checks) | 8 | 6 | 2,013 | 43,704 | 23,758 | **$0.310** |
| Full monitor, warm cache | 8 | 8 | 2,446 | 85,047 | 4,498 | **$0.149** |
| Overdue projects only | 6 | 6 | 1,278 | 59,750 | 7,061 | **$0.133** |

The first row is a cold cache — 23.7k cache *writes* at 1.25× versus 4.5k on
the warm run. Same work, less than half the price once the prefix is cached, so
a monitor on a short interval is cheaper per run than these numbers suggest.

Roughly **$0.15–$0.40 per run**. Almost all of it is cache reads on the system
prompt + tool definitions, re-read on each turn — output tokens are a small
fraction. So the levers are turn count and prompt size, not answer length.

At 15-minute intervals that's ~$430–1,150/month per monitor. Ways down, in
order of effect:

1. **Narrow the task.** The two runs above differ only in scope; the narrower
   one cost 63% less.
2. **`MONITOR_MODEL=claude-haiku-4-5`** — $1 / $5 per MTok, 5× cheaper, and
   plenty for "run two queries from a skill and fill a schema". Sonnet 5 at
   $3 / $15 sits in between.
3. **Lower `effort`.** `ClaudeAgentOptions(effort="low")` cuts thinking depth.
4. **Widen the interval.** Anomaly monitors rarely need 15 minutes.

`max_budget_usd` (default `$1.00`, override with `MONITOR_MAX_USD`) is a hard
per-run stop, and `max_turns=20` bounds the loop. Both are SDK-native — no
hand-rolled accounting.

---

## 7. What is hand-rolled, and why

The constraint was to prefer SDK-native features everywhere. Four places
deliberately don't:

| Hand-rolled | Why |
|---|---|
| `audit()` JSONL sink | The SDK exposes the message stream but has no opinion about where audit records go. This is a sink, not a re-implementation. |
| `provider_env()` | A dict of env-var names per provider. Pure configuration mapping; no SDK equivalent exists. |
| Re-validating `structured_output` through Pydantic | The SDK already validated it against the schema. The second pass only converts a `dict` into a typed Python object at the boundary — it does **not** re-implement the retry loop. |
| `is_destructive()` SQL classifier | Unavoidable: the shared `query_db` tool takes reads and writes through one interface, so no SDK-native tool-name rule can express "reads only". Deliberately mirrors the Strands `is_destructive()` keyword set. |

Explicitly **not** hand-rolled, where it would have been tempting:

- No retry loop — the SDK retries transient API errors; re-running a pass is
  the scheduler's job.
- No context trimming — the SDK manages the window.
- No permission checks in application code — `allowed_tools` +
  `permission_mode="dontAsk"` + the hook.
- No agent loop, no tool dispatch, no JSON parsing of tool calls.
- No `"Skill"` in `allowed_tools` — `skills=[...]` handles it.
