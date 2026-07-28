****# Strands vs. the raw API

For the other comparison — Strands against the Claude Agent SDK, framework vs.
framework — see [`docs/strands-vs-agent-sdk.md`](strands-vs-agent-sdk.md).

Strands is an **agent framework**, not a model. It sits *on top of* the same
Claude / OpenAI / Bedrock endpoints you could call directly — so the real
question isn't "Strands vs. the model API," it's **"let the framework drive the
agent loop, or drive it yourself?"**

This project is a good illustration: the *exact same* Strands code runs against
Amazon Bedrock (`aws-Strands/`) and a local model via LM Studio (`lm-studio/`).
Only the `model=` backend differs.

## The core thing: Strands runs the agentic loop for you

With the **direct API**, a tool-using agent is a loop *you* write:

```python
while True:
    resp = client.messages.create(model=..., messages=msgs, tools=schemas)
    if resp.stop_reason != "tool_use":
        break
    for call in tool_calls(resp):
        result = dispatch(call)           # you route name -> function
        msgs.append(tool_result(result))  # you format it back
```

Strands *is* that loop (`event_loop_cycle`). You just write `agent("question")`.
Everything below is work you'd otherwise hand-build inside that loop.

## What you'd re-implement yourself with the raw API

| Capability | In this project | Raw API equivalent |
|---|---|---|
| **Agentic loop** | `agent("…")` | You write the while-loop + tool dispatch |
| **Tool definitions** | `@tool def weather_agent(...)` — schema auto-generated from type hints + docstring | Hand-write JSON schemas, keep them in sync with the code |
| **MCP integration** | `MCPClient(...)` passed as `tools=` | Wire the MCP SDK to the model's tool-call format yourself |
| **Provider swap** | `model=get_model()` → Bedrock **or** LM Studio, same code | Rewrite request/response handling per provider (Anthropic ≠ OpenAI schemas) |
| **Multi-agent** | orchestrator with `data_agent` / `weather_agent` as tools | Compose and route between models manually |
| **Context management** | `SummarizingConversationManager`, `SlidingWindowConversationManager` | Track the message list, decide when/what to trim or summarize |
| **Memory across runs** | `FileSessionManager` | Serialize / reload conversation state yourself |
| **Human-in-the-loop** | `ApprovalHook` on `BeforeToolCallEvent` — pause before destructive SQL | Intercept every tool call in your loop and gate it |
| **Structured output** | `structured_output_model=DepartmentReport` (Pydantic + validation + retry) | Tool / `response_format` extraction, then validate + retry on mismatch |
| **Safety limits** | `limits={"turns": 8}` — the runaway cap | Your loop, your counter |
| **Observability / retries** | built-in metrics, OTEL tracing, token usage, backoff | Add per-provider |

The **provider-swap** row is the one you feel most directly in this repo:
switching Bedrock ↔ Gemma-via-LM-Studio was a one-line `model=` change. With the
direct API that's two different SDKs and two different message formats.

## What the direct API gives *you* back (the tradeoffs)

Strands isn't free of cost in control:

- **Transparency** — you don't see the exact request shape. Example: the noisy
  `reasoningContent is not supported…` warning was Strands' provider abstraction
  leaking, something you'd never see calling LM Studio directly.
- **Simplicity for simple jobs** — for a single-shot completion (summarize,
  classify; no tools, no loop), the direct API is *less* code and one fewer
  dependency.
- **Latency & debugging** — fewer layers between you and the model; stack traces
  don't pass through a framework.

## Rule of thumb

- **Direct API** → one-off completions, full control of prompt/params, minimal
  deps, or you already have your own orchestration.
- **Strands** → exactly what this app is: multiple tools, MCP, multi-agent
  delegation, memory, approval gates, and running the *same code* on cloud or
  local models. Re-implementing that loop + tool plumbing + provider abstraction
  by hand is real, bug-prone work.

## What does it cost?

**Strands itself is free** — open source (Apache 2.0). There is no license fee
and no per-call charge for the framework. You pay only for **where the model
runs**:

| Path | Framework | Model inference | Hardware |
|---|---|---|---|
| `aws-Strands/` (Bedrock) | free | **pay AWS per token** (input + output, Bedrock Claude pricing) | none (AWS-hosted) |
| `lm-studio/` (local) | free | free (open weights: Gemma, Llama) | **your own machine** (electricity) |

So Bedrock costs you per-token model usage on AWS; the local path costs you
nothing beyond the hardware you already own. Strands adds **zero** to either.
