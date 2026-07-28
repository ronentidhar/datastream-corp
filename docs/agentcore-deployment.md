# AgentCore deployment (Phase 2)

Phase 1 runs the Strands orchestrator on your laptop against a local SQLite file.
Phase 2 moves every piece into AWS: the tools become a hosted MCP runtime behind a
Gateway, the agent itself becomes a hosted runtime, and conversation context moves
into AgentCore Memory. Cognito authenticates both hops.

```
caller ──JWT──▶ Agent Runtime ──M2M OAuth──▶ Gateway ──OAuth──▶ MCP Runtime ──HTTPS──▶ shared DB
                     │
                     └── AgentCore Memory (per-actor, cross-session)
```

## Running it

Four scripts, in order. Each is idempotent and appends its outputs to `.env`, which
the next one reads. Run them from the repo root **on the workshop box** — see
[CLAUDE.md](../CLAUDE.md) for why deploys don't work locally.

| Script | Creates | Records in `.env` |
|---|---|---|
| `runtime_deploy.sh` | MCP server on AgentCore Runtime, JWT authorizer | `MCP_SERVER_ARN`, `MCP_SERVER_URL` |
| `gateway_setup.sh` | Gateway IAM role, OAuth credential provider, Gateway, MCP target | `GATEWAY_ID`, `GATEWAY_URL`, `GATEWAY_ARN`, `CREDENTIAL_PROVIDER_ARN`, `GATEWAY_ROLE_ARN` |
| `memory_setup.sh` | Memory resource + semantic `UserFacts` strategy | `MEMORY_ID`, `LONG_TERM_MEMORY_STRATEGY_ID` |
| `deploy_agent.sh` | Agent runtime, execution-role policies, smoke test | `AGENT_RUNTIME_ARN` |

Then invoke it — `scripts/token.sh` is sourced from `~/.bashrc`:

```bash
ask "Which department has the largest budget?"          # via the agentcore CLI
ask_http "Which department has the largest budget?"     # plain HTTPS, no CLI needed
ask "How many people work in Sales?" bob-smith          # different actor, separate memory
```

## The two authentication hops

Inbound and outbound are separate mechanisms, and confusing them is the usual
source of `AccessDeniedException`:

- **Inbound** (caller → runtime, and agent → Gateway): a Cognito **JWT**, validated
  by each resource's `customJWTAuthorizer` against the pool's discovery URL with
  `allowedClients` pinned to the app client. Mint one with `get_client_token`.
- **Outbound** (Gateway → MCP runtime): an **M2M OAuth** token the Gateway fetches
  itself from the `datastream-cognito-oauth` credential provider, using the
  `client_credentials` grant and the `datastream/mcp.access` scope. The agent gets
  its own via the `@requires_access_token` decorator.

An empty bearer token produces `Authorization method mismatch` rather than a clear
401 — the client falls back to SigV4, which the runtime isn't configured for.

## What Phase 2 changes in the code

`agent-runtime/agent.py` is a different program from `aws-Strands/agent.py`, not an
edit of it:

| | `aws-Strands/agent.py` | `agent-runtime/agent.py` |
|---|---|---|
| Entry | `python agent.py` | `@app.entrypoint` on `BedrockAgentCoreApp` |
| Tools | MCP over streamable-http to localhost | MCP over HTTPS to the Gateway |
| Session state | in-process | `AgentCoreMemorySessionManager` |
| Identity | none | actor id from a forwarded header |
| Config | `.env` | runtime env vars (`agentcore deploy --env`) |

Both keep the same `ApprovalHook` + `is_destructive()` write guard.

## Where the sample code needed fixing

The task pages' sample code contains bugs that are worth knowing about, because
each one fails quietly rather than loudly:

- **Approval gate read the wrong parameter.** It looked for `input["sql"]`, but the
  remote server names it `query`. `is_destructive("")` is `False`, so writes reached
  the shared database with no prompt.
- **Destructive check was substring-based** over `["DELETE","UPDATE","INSERT"]`,
  which misses `DROP TABLE` entirely and fires on reads like
  `... WHERE body LIKE '%update%'`. Checking the leading keyword fixes both.
- **Tool name compared by equality** to `query_db`, but a Gateway prefixes it to
  `datastream-mcp-target___query_db`, so the gate never fired once traffic moved.
- **Actor header looked up case-sensitively.** The runtime canonicalises only
  `Authorization`; HTTP/2 lowercases the rest, so every caller became
  `default_user` and per-actor memory isolation silently collapsed.
- **`relevance_score=0.7`** discards the stored facts, which score ~0.35 against
  task-shaped prompts — making the cross-session recall demo unpassable as written.
- **Credential provider name** (`ac-gateway-mcp-server-identity`) doesn't exist in
  this account; it's resolved at invoke time, so it deploys fine and fails on first
  call.
- **National Weather Service API** covers the US only, and the sample prompts ask
  about Haifa and Tel Aviv. Open-Meteo works globally.

## Debugging

```bash
agentcore status -a datastream_agent
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT --since 15m --follow

# What the Gateway actually exposes
curl -s -X POST "$GATEWAY_URL" -H "Authorization: Bearer $(get_client_token)" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq

# What memory holds for one actor, with relevance scores
aws bedrock-agentcore retrieve-memory-records --memory-id "$MEMORY_ID" --region "$REGION" \
  --namespace "/strategies/$LONG_TERM_MEMORY_STRATEGY_ID/actors/alice-chen/" \
  --search-criteria '{"searchQuery":"report format","topK":5}' \
  --query 'memoryRecordSummaries[].[score,content.text]'
```

**Use a fresh session id after every redeploy.** A session id stays bound to the
agent version that first used it, so reusing one routes to the old code and makes a
working fix look like a no-op. `new_session_id` handles this.

Traces and invocations land in the [GenAI Observability
dashboard](https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#gen-ai-observability/agent-core),
though data can take ~10 minutes to appear after a first launch.
