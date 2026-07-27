# Strands vs. the Claude Agent SDK

[`docs/strands_vs_raw_api.md`](strands_vs_raw_api.md) compares Strands to the
**raw API** — framework or hand-rolled loop. This document answers the other
question, the one this repo actually poses by keeping both builds side by side:
**two frameworks, which one, and why?**

Both sit above the same model endpoints. Both run the agentic loop for you. So
the loop is *not* the deciding factor — on that axis they are roughly equal, and
the "what you'd re-implement yourself" table in that document applies just as
well to the Agent SDK. What separates them is **where configuration lives** and
**which models you can reach**.

## First, the line-count trap

The obvious reading of this repo is that the Agent SDK costs you more code:

```
118  aws-Strands/agent.py
344  claude-sdk/service.py
```

*(code lines — comments and docstrings excluded)*

That comparison is wrong, and it is worth dismantling because it is the first
thing anyone notices. The two files are not the same kind of artifact:
`agent.py` is a demo script, `service.py` is an unattended service.

A **minimal Agent SDK build of the same monitor** — same write-blocking hook,
same `IncidentReport` schema, same skill, same subagents, verified to run and
produce the same findings — is **38 lines**:

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
(`service.py`)**. Here is where `service.py`'s 344 actually go:

| Section of `service.py` | Lines | Strands equivalent |
|---|---|---|
| Structured result | 15 | `DepartmentReport` — **20 lines, larger** (it has a `field_validator`) |
| The code-enforced rule (both hooks + audit) | 97 | `is_destructive` + `ApprovalHook` + `drain_interrupts` ≈ 60, doing less |
| Provider routing | 33 | `lm-studio/local_model.py` — 38 lines, in a separate folder |
| Options | 40 | ~15, scattered across three `Agent(...)` constructions |
| One task per invocation | 133 | `orchestrator(p)` — **1 line** |

Only the last row is genuine SDK surface area, and most of *it* isn't
loop-driving either: it is five typed exception handlers, a JSONL audit of
usage / cost / `permission_denials`, and schema validation at the boundary.
`aws-Strands/agent.py` has **no error handling at all** — an API failure crashes
it, and nothing records what the agent did.

**Conclusion: the extra code is features, not framework tax.** Judge the
frameworks on the two things below instead.

## Deciding factor 1 — which models you can reach

| | Strands | Claude Agent SDK |
|---|---|---|
| Anthropic API | ✓ | ✓ |
| Bedrock / Vertex / Foundry | ✓ | ✓ (Claude on those clouds) |
| OpenAI, Gemini, Mistral… | ✓ (first-class) | ✗ officially — only via a proxy |
| **Local / offline** (Ollama, LM Studio) | ✓ (first-class) | ✗ officially — only via a proxy |

This repo demonstrates the gap directly: `lm-studio/` is `aws-Strands/` with
`model=get_model()` added, and it runs fully offline against Gemma 4 E4B.

The Agent SDK has no equivalent switch. There is an unofficial escape hatch —
the SDK forwards `options.env` verbatim to the Claude Code CLI subprocess, so
setting `ANTHROPIC_BASE_URL` to an Anthropic-API-compatible gateway (LiteLLM
proxy, `claude-code-router`) points it at whatever that gateway fronts,
including a local model. But that is unsupported, and the work moves rather
than disappearing: the CLI expects Messages API semantics throughout — tool-use
blocks, thinking blocks, streaming event shapes, structured output — so the
proxy has to synthesize all of it. Strands does the same translation in-process
with a `model=` argument and no extra service to run.

If offline operation, model-cost arbitrage, or provider independence matters,
this row decides it and nothing below outweighs it.

## Deciding factor 2 — where configuration lives

| | Strands | Claude Agent SDK |
|---|---|---|
| System prompts | Python string literals (`WEATHER_PROMPT`) | `CLAUDE.md`, `.claude/agents/*.md` |
| Sub-specialists | `@tool`-wrapped nested `Agent`s | markdown files invoked via `Task` |
| Tool restriction | `tools=[...]` in code | `tools:` frontmatter + `allowed_tools` |
| Permissions | hooks only | `allow`/`deny`/`ask` + `permission_mode`, hooks run first |
| Progressive disclosure | — | **Skills**, loaded on demand |
| Changing a prompt | code change + redeploy | edit a markdown file |

The two subagents in `claude-sdk/.claude/agents/` are **43 lines of markdown**
doing what is prompts, tool lists, and wiring in `agent.py`. That is the payoff,
and it is also why the Agent SDK build looks larger than it is — a third of its
behaviour isn't in the line count at all.

The cost of that model is **implicitness**. Config is discovered from disk, so
an unattended service must pin it down or inherit whatever is on the operator's
machine — hence `setting_sources=["project"]` and `strict_mcp_config=True` in
`build_options()`, both of which exist purely to *stop* discovery. Strands has
no such sharp edge, because there is nothing to discover.

## Everything else

**Agent SDK advantages beyond config**

- Built-in accounting: `total_cost_usd`, `num_turns`, `permission_denials`, plus
  native `max_turns` and `max_budget_usd`. Strands reports no cost; `TURN_LIMIT`
  is the only guard.
- The whole Claude Code tool suite (Read, Grep, Glob, WebFetch, Task) for free.
- Hooks run *before* permission rules and cannot be bypassed by
  `permission_mode` — the property that makes `block_production_writes` the
  real enforcement point.
- The same directory runs under `claude -p` and the IDE, not just your script.

**Strands advantages beyond model choice**

- Pure in-process Python. The Agent SDK spawns a bundled Node CLI subprocess, so
  the loop lives outside your debugger and stack traces stop at the boundary.
- Genuine interactive human-in-the-loop: `event.interrupt()` +
  `drain_interrupts` produces a real *"Allow? (y)es / (t)rust all / (N)o"*
  prompt. The SDK's `dontAsk` is batch-shaped; interactive approval needs a
  `can_use_tool` callback.
- Conversation and session management (`SummarizingConversationManager`,
  `SlidingWindowConversationManager`, `FileSessionManager`) with no SDK
  equivalent.
- Multi-agent composition is ordinary function composition — easy to unit-test
  without a subprocess or a filesystem layout.

## Rule of thumb

- **Any need for non-Claude models — especially local or offline — → Strands.**
  Decisive on its own.
- **Claude-committed, and you want prompts and policy editable without shipping
  code → Agent SDK.** Config-as-files is the whole payoff.
- **Neither is "less code."** At demo scale the SDK is smaller (38 vs. 118); at
  service scale the difference is the features you add, and you add the same
  ones either way.

## Cost

Same as [`docs/strands_vs_raw_api.md`](strands_vs_raw_api.md): both frameworks
are free, and you pay only for where the model runs. The Agent SDK narrows *where* that can
be — Anthropic, Bedrock, or Vertex, but never your own GPU.
