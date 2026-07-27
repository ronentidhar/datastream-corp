# Strands vs. the Claude Agent SDK

[`docs/strands_vs_raw_api.md`](strands_vs_raw_api.md) compares Strands to the
**raw API** — framework or hand-rolled loop. This document answers the other
question, the one this repo poses by keeping both builds side by side: **two
frameworks, which one, and why?**

Both sit above the same model endpoints and both run the agentic loop for you,
so the loop is not the deciding factor. What separates them is **cost at
volume**, **how they consume machine resources**, **what you can see when
something goes wrong**, and **which models you can reach**.

---

## The decision

> **For a high-volume, critical service: Strands on Bedrock.**
> **For low-volume, policy-heavy, governed jobs: the Claude Agent SDK.**

That split is not a hedge — it follows from the criteria below, and it is the
architecture this repo already has. The nightly integrity monitor in
`claude-sdk/` is one call per night, where $0.16 and 30 seconds don't matter and
unbypassable write guards do. A request path serving thousands of calls a day
inverts every one of those weights.

Line count is **not** a criterion. It is the first thing anyone notices about
this repo and it decides nothing — see the appendix if you want it dismantled.

---

## Production criteria

| | Strands | Claude Agent SDK |
|---|---|---|
| **Cost lever at volume** | Route Haiku ↔ Sonnet ↔ Opus per request | Claude-only; pays the preset system prompt (36–86K cached tokens) every turn |
| **Concurrency** | In-process; asyncio / worker pools | One Node subprocess per `query()` |
| **Observability** | Python stack traces, OTEL, `py-spy` | Loop runs in a subprocess — stderr and the message stream only |
| **Capacity guarantee** | Bedrock provisioned throughput | API tier rate limits |
| **Version risk** | Pin a Python library; the model is the only moving part | Pins a library **and** a bundled CLI whose preset prompt and tools shift |
| **Enforcement** | Hooks you register | `PreToolUse` hooks run *before* permission rules, unbypassable |
| **Circuit breaker** | You build it | Native `max_budget_usd` / `max_turns` |
| **Policy change** | Code change + redeploy | Edit a file on disk |
| **Cloud posture** | AWS-native — IAM, VPC endpoints, CloudWatch, Guardrails | Bearer token to the Anthropic API (Bedrock/Vertex available) |

The rows that need more than a cell:

### Cost — the one that dominates at volume

Measured from `claude-sdk/logs/audit.jsonl`, 20 real runs of the integrity
monitor: **mean $0.1579 per call**, range $0.085–$0.203.

| Volume | Daily | Annual |
|---|---|---|
| 1,000 calls/day | $158 | ~$58K |
| 10,000 calls/day | $1,579 | ~$576K |

Strands' lever is model routing: one `model=` argument sends the easy majority
to Haiku or Sonnet and reserves Opus for the hard minority. The Agent SDK can
pin a model but cannot route below Claude.

There is a second, less obvious cost. Every run in the log shows
`cache_read_input_tokens` between **36K and 86K** — that is the Claude Code
preset system prompt plus `CLAUDE.md` plus the skill, re-read on every turn.
Cheap at cache rates, not free, and multiplied by turns × calls. You are
renting the batteries-included harness on every turn of every request.

### Concurrency — one subprocess per call

`query()` spawns a Claude Code CLI subprocess via `anyio.open_process`
(`claude_agent_sdk/_internal/transport/subprocess_cli.py:733`). At thousands of
calls that is process-spawn latency and Node RSS per concurrent request, and a
system you scale by process count rather than by asyncio concurrency.

`claude-sdk/http_app.py` already says so in its own docstring:

> *"each request spawns its own CLI subprocess via query(). That is fine for a
> monitor doing a handful of runs an hour; it is not fine as a chat backend."*

Strands runs in-process: ordinary worker pools, ordinary backpressure, ordinary
memory limits.

### Version risk — an uncontrolled input to production behavior

`claude-sdk/service.py` opens with *"Verified against claude-agent-sdk 0.2.128 /
bundled CLI."* That note is load-bearing. The Agent SDK ships a Claude Code CLI
binary, and that CLI's **preset system prompt, built-in tool set, and tool
descriptions** shape model behavior — so bumping the dependency can change what
your service does without a line of your code changing. Pinning your own code
is not enough; you have to pin and re-qualify the CLI.

With Strands the model ID is the only thing that moves behavior, and you pin it
explicitly.

### Observability — answering "why was this run different"

At 3am the question is why a run took 8 turns and $0.20 when the same prompt
took 3 turns and $0.09 yesterday. In-process Python answers that with a
profiler and a trace. A Node subprocess answers it with whatever you wrote into
your own audit log — which `service.py` does build (`logs/audit.jsonl`, one JSON
object per tool call), and which is the only reason those runs are
reconstructable after the fact.

### Determinism — the part that resists an SLA

Across the same prompt, the log shows **3–8 turns** (2.7× spread), **$0.085–$0.203**
(2.4×), and roughly 30 seconds wall clock. That variance is a property of the
agent loop, not of either framework — but it is what you are putting an SLA on.
See *Is an agent the right shape?* below.

### Where the Agent SDK is genuinely stronger

Not a consolation prize — these matter on exactly the governed workloads it
should keep:

- **`PreToolUse` hooks run before permission rules and cannot be bypassed by
  `permission_mode`.** For a hard "never write to production" rule that is a
  better enforcement primitive than a `BeforeToolCallEvent` handler you could
  forget to register.
- **`max_budget_usd` and `max_turns`** are native spend and runaway circuit
  breakers.
- **Structured output with SDK-side validation and retry.**
- **Policy lives on disk** — change a prompt, a tool allowlist, or a subagent
  without shipping code.

---

## Which models you can reach

| | Strands | Claude Agent SDK |
|---|---|---|
| Anthropic API | ✓ | ✓ |
| Bedrock / Vertex / Foundry | ✓ | ✓ (Claude on those clouds) |
| OpenAI, Gemini, Mistral… | ✓ (first-class) | ✗ officially — only via a proxy |
| **Local / offline** (Ollama, LM Studio) | ✓ (first-class) | ✗ officially — only via a proxy |

This repo demonstrates the gap directly: `lm-studio/` is `aws-Strands/` with
`model=get_model()` added, and it runs fully offline against Gemma 4 E4B.

The Agent SDK has no equivalent switch. There is an unofficial escape hatch —
the SDK forwards `options.env` verbatim to the CLI subprocess, so setting
`ANTHROPIC_BASE_URL` to an Anthropic-API-compatible gateway (LiteLLM proxy,
`claude-code-router`) points it at whatever that gateway fronts, including a
local model. But that is unsupported, and the work moves rather than
disappearing: the CLI expects Messages API semantics throughout — tool-use
blocks, thinking blocks, streaming event shapes, structured output — so the
proxy has to synthesize all of it. Strands does the same translation in-process
with a `model=` argument and no extra service to run.

At production volume this row is not really about offline operation — it is the
**cost lever** from the criteria above wearing a different hat.

---

## Where configuration lives

| | Strands | Claude Agent SDK |
|---|---|---|
| System prompts | Python string literals (`WEATHER_PROMPT`) | `CLAUDE.md`, `.claude/agents/*.md` |
| Sub-specialists | `@tool`-wrapped nested `Agent`s | markdown files invoked via `Task` |
| Tool restriction | `tools=[...]` in code | `tools:` frontmatter + `allowed_tools` |
| Permissions | hooks only | `allow`/`deny`/`ask` + `permission_mode`, hooks run first |
| Progressive disclosure | — | **Skills**, loaded on demand |
| Changing a prompt | code change + redeploy | edit a markdown file |

The two subagents in `claude-sdk/.claude/agents/` are 43 lines of markdown doing
what is prompts, tool lists, and wiring in `agent.py`.

The cost of that model is **implicitness**, and it is sharper in production than
in a demo: config is discovered from disk, so an unattended service must pin it
down or inherit whatever is on the host — hence `setting_sources=["project"]`
and `strict_mcp_config=True` in `build_options()`, both of which exist purely to
*stop* discovery. It also means your deployment artifact is a directory tree,
not just a wheel. Strands has no such sharp edge, because there is nothing to
discover.

---

## Is an agent the right shape at all?

For a critical service the agent loop is itself the risk, whichever framework
runs it — the 2.7× turn spread above is inherent to letting a model decide how
many steps to take.

Look at what the triage task actually needs: `SKILL.md` hands the model **three
fixed SQL queries**. The agent's only real decision is to run them. That is a
workflow, not an agent — three `sqlite3` calls plus one LLM call to interpret
the results would be deterministic, milliseconds of I/O, one predictable API
call, and roughly an order of magnitude cheaper.

So the sharper version of the recommendation: **reserve the agent loop for
genuinely open-ended requests and run the well-specified ones as workflows.**
That also argues for Strands, because it is a library you call however you like
rather than a harness that owns the loop.

---

## Rule of thumb

- **Thousands of calls on a critical path → Strands on Bedrock.** Cost routing,
  in-process concurrency, and AWS-native operations decide it.
- **Any need for non-Claude models, especially local or offline → Strands.**
  Decisive on its own.
- **Low-volume governed jobs, or policy that must change without a deploy →
  Agent SDK.** Unbypassable hooks and config-as-files are the payoff.
- **Well-specified high-volume tasks → neither loop.** Write the workflow.

---

## Cost of the frameworks themselves

Both are free. You pay for where the model runs. The Agent SDK narrows *where*
that can be — Anthropic, Bedrock, Vertex, or Foundry, but never your own GPU —
which is the cost lever discussed above, not a licensing question.

---

## Appendix: the line-count trap

Kept because it is the first thing anyone notices about this repo, and because
it is wrong. It decides nothing about production.

```
118  aws-Strands/agent.py
344  claude-sdk/service.py
```

*(code lines — comments and docstrings excluded)*

The two files are not the same kind of artifact: `agent.py` is a demo script,
`service.py` is an unattended service. A **minimal Agent SDK build of the same
monitor** — same write-blocking hook, same `IncidentReport` schema, same skill,
same subagents, verified to run and produce the same findings — is **38 lines**:

```python
class IncidentReport(BaseModel):
    severity: Literal["critical", "warning", "info"]
    incident_found: bool
    summary: str
    affected_ids: list[str] = []
    recommended_action: str
    requires_human_signoff: bool


async def block_writes(data, _id, _ctx):
    sql = str(data.get("tool_input", {}).get("sql", "")).lower()
    if set(re.findall(r"[a-z_]+", sql)) & DESTRUCTIVE:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": "reads only"}}
    return {}


options = ClaudeAgentOptions(
    cwd=".",
    setting_sources=["project"],
    system_prompt={"type": "preset", "preset": "claude_code"},
    skills=["data-integrity-triage"],
    allowed_tools=["mcp__datastream__query_db", "mcp__datastream__list_schema"],
    disallowed_tools=["Bash", "Write", "Edit"],
    permission_mode="dontAsk",
    hooks={"PreToolUse": [HookMatcher(matcher="mcp__datastream__.*",
                                      hooks=[block_writes])]},
    output_format={"type": "json_schema",
                   "schema": IncidentReport.model_json_schema()},
)
async for msg in query(prompt="Run the data-integrity monitor.", options=options):
    if isinstance(msg, ResultMessage):
        print(json.dumps(msg.structured_output, indent=2))
```

So the real spread is **118 (Strands) vs. 38 (minimal SDK) vs. 344
(`service.py`)**. Where `service.py`'s 344 actually go:

| Section of `service.py` | Lines | Strands equivalent |
|---|---|---|
| Structured result | 15 | `DepartmentReport` — **20 lines, larger** (it has a `field_validator`) |
| The code-enforced rule (both hooks + audit) | 97 | `is_destructive` + `ApprovalHook` + `drain_interrupts` ≈ 60, doing less |
| Provider routing | 33 | `lm-studio/local_model.py` — 38 lines, in a separate folder |
| Options | 40 | ~15, scattered across three `Agent(...)` constructions |
| One task per invocation | 133 | `orchestrator(p)` — **1 line** |

Only the last row is genuine SDK surface area, and most of *it* is not
loop-driving either: five typed exception handlers, a JSONL audit of usage /
cost / `permission_denials`, and schema validation at the boundary.
`aws-Strands/agent.py` has **no error handling at all** — an API failure crashes
it, and nothing records what the agent did. Those are the features that make the
observability and circuit-breaker rows above possible.

**The extra code is features, not framework tax** — which is exactly why it is
the wrong axis to decide production on.
