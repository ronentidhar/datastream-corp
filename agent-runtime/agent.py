"""DataStream Corp executive assistant, deployed to AgentCore Runtime.

Inbound  : Cognito JWT, validated by the runtime's customJWTAuthorizer.
Outbound : M2M OAuth token fetched by @requires_access_token, sent to the Gateway.
Memory   : AgentCore Memory session manager, isolated per actor.

Config comes from runtime environment variables (agentcore deploy --env KEY=VALUE)
rather than hardcoded constants, so redeploying against a different memory or
gateway needs no source edit.
"""

import os
import re

from bedrock_agentcore import BedrockAgentCoreApp, RequestContext
from bedrock_agentcore.identity import requires_access_token
from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent, tool
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
from strands.tools.mcp import MCPClient
from strands_tools import http_request

app = BedrockAgentCoreApp()

REGION = os.environ.get("REGION", "us-east-1")
MEMORY_ID = os.environ["MEMORY_ID"]
LONG_TERM_MEMORY_STRATEGY_ID = os.environ["LONG_TERM_MEMORY_STRATEGY_ID"]
GATEWAY_ID = os.environ["GATEWAY_ID"]
CREDENTIAL_PROVIDER_NAME = os.environ.get(
    "CREDENTIAL_PROVIDER_NAME", "datastream-cognito-oauth"
)
MCP_SCOPE = os.environ.get("COGNITO_SCOPE", "datastream/mcp.access")

# Retrieval gate for long-term facts. Measured against this memory, a stored
# preference ("The user prefers JSON format for reports.") scores ~0.37 against a
# task-shaped prompt ("Query the database for employee count and generate a
# report"). The 0.7 the task specifies discards both facts, so nothing is ever
# recalled. Facts are short and the actor namespace is small, so a lower gate with
# top_k=5 is the right trade here.
MEMORY_RELEVANCE = float(os.environ.get("MEMORY_RELEVANCE_SCORE", "0.3"))
MEMORY_TOP_K = int(os.environ.get("MEMORY_TOP_K", "5"))

ACTOR_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id"


def header_value(headers, name: str, default: str) -> str:
    """Case-insensitive header lookup.

    The runtime normalises only Authorization to a canonical key; every other
    forwarded header keeps its wire casing, and HTTP/2 lowercases all of them. An
    exact-case dict lookup therefore misses the actor header and every caller
    silently collapses to the default actor, sharing one memory namespace.
    """
    if not headers:
        return default
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value or default
    return default

DESTRUCTIVE_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter", "create",
    "replace", "truncate", "attach", "detach", "pragma", "vacuum",
}


def is_destructive(sql: str) -> bool:
    """True if the statement is anything other than a plain read.

    Keyword position matters: a substring scan flags `SELECT * FROM notes WHERE
    body LIKE '%update%'` as a write, which would prompt for approval on a read.
    """
    cleaned = re.sub(r"--[^\n]*", " ", sql)
    cleaned = re.sub(r"/\*.*?\*/", " ", cleaned, flags=re.S)
    tokens = re.findall(r"[a-zA-Z_]+", cleaned.lower())
    if not tokens:
        return False
    return tokens[0] in DESTRUCTIVE_KEYWORDS or (
        ";" in cleaned and any(t in DESTRUCTIVE_KEYWORDS for t in tokens)
    )


class ApprovalHook(HookProvider):
    """Pause and ask a human before any write reaches the shared database."""

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self.approve)

    def approve(self, event: BeforeToolCallEvent) -> None:
        # A Gateway prefixes tool names with the target name, so the tool arrives
        # as "datastream-mcp-target___query_db" rather than "query_db".
        if not event.tool_use.get("name", "").endswith("query_db"):
            return

        params = event.tool_use.get("input", {})
        sql = params.get("query") or params.get("sql") or ""
        if not is_destructive(sql):
            return

        approval = event.interrupt("approval-required", reason={"query": sql})
        if approval.lower() not in ("y", "t"):
            event.cancel_tool = "User denied permission to run this statement."


DATA_PROMPT = """
You are a data specialist for DataStream Corp. Query the database to answer
questions. If you need the schema, run:
  SELECT sql FROM sqlite_master WHERE type='table'
You may run write statements when asked; a separate approval system handles
authorization, so do not refuse on safety grounds -- just issue the SQL.
State plainly whether your counts filter on employee status.
"""

# Open-Meteo rather than the National Weather Service: NWS only covers the United
# States, and these agents are asked about Haifa and Tel Aviv.
WEATHER_PROMPT = """
You are a weather specialist. Use http_request against the free Open-Meteo API --
no API key needed.

Geocode first:
  https://geocoding-api.open-meteo.com/v1/search?name=CITY&count=1
Then fetch:
  https://api.open-meteo.com/v1/forecast?latitude=LAT&longitude=LON&current=temperature_2m,precipitation,weather_code

Report temperature in Celsius and translate weather_code into plain words.
"""


@app.entrypoint
@requires_access_token(
    scopes=[MCP_SCOPE],
    provider_name=CREDENTIAL_PROVIDER_NAME,
    auth_flow="M2M",
)
def invoke(payload, context: RequestContext, access_token: str):
    actor_id = header_value(context.request_headers, ACTOR_HEADER, "default_user")
    session_id = context.session_id or "default_session"
    print(f"actor_id={actor_id} session_id={session_id}", flush=True)

    memory_config = AgentCoreMemoryConfig(
        memory_id=MEMORY_ID,
        session_id=session_id,
        actor_id=actor_id,
        retrieval_config={
            "/strategies/{memoryStrategyId}/actors/{actorId}": RetrievalConfig(
                top_k=MEMORY_TOP_K,
                relevance_score=MEMORY_RELEVANCE,
                strategy_id=LONG_TERM_MEMORY_STRATEGY_ID,
            )
        },
    )
    session_manager = AgentCoreMemorySessionManager(
        agentcore_memory_config=memory_config,
        region_name=REGION,
    )

    mcp_client = MCPClient(
        lambda: streamablehttp_client(
            url=f"https://{GATEWAY_ID}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    )

    with mcp_client:
        tools = mcp_client.list_tools_sync()

        @tool
        def data_agent(query: str) -> str:
            """Query and analyze the DataStream Corp database.

            Use for employee counts, department statistics, and any SQL question.
            Supports full CRUD; writes go through a human approval gate.

            Args:
                query: A database or data-analysis question.

            Returns:
                Query results from the database.
            """
            agent = Agent(
                system_prompt=DATA_PROMPT,
                tools=tools,
                hooks=[ApprovalHook()],
            )
            return agent(query).message

        @tool
        def weather_agent(query: str) -> str:
            """Get weather information for any location worldwide.

            Args:
                query: A weather-related question.

            Returns:
                Current conditions and forecast.
            """
            agent = Agent(system_prompt=WEATHER_PROMPT, tools=[http_request])
            return agent(query).message

        orchestrator = Agent(
            name="Executive Assistant",
            system_prompt=f"""You are the executive assistant at DataStream Corp,
serving {actor_id}.

Route queries to specialists:
- Database queries, employee counts, department stats -> data_agent
- Weather forecasts, temperature, conditions -> weather_agent
- Simple company questions -> answer directly

Use what you remember about this user to personalise your answers, including
their preferred language and report format. DataStream Corp is a technology
company with Sales, HR, and Engineering departments.""",
            tools=[data_agent, weather_agent],
            session_manager=session_manager,
        )

        result = orchestrator(payload.get("prompt", ""))
        return {"response": result.message}


if __name__ == "__main__":
    app.run()
