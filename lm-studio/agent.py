# TODO: Import FileSessionManager

# TODO: Create FileSessionManager with session_id and storage_dir

# TODO: Add session_manager to Agent (keep existing MCP tools from Task 2)

# TODO: Test by introducing yourself, then restart script to verify memory

# Agents as Tools (Multi-Agent Architecture)
# TODO: Import http_request from strands_tools

# TODO: Create data_agent as @tool with database access (MCP tools)

# TODO: Create weather_agent as @tool with http_request tool

# TODO: Create orchestrator with both agents as tools

# TODO: Test with database queries, weather queries, and combined queries

"""DataStream Corp orchestrator — data specialist + weather specialist."""

import re
import sys
from typing import Any

from mcp import StdioServerParameters, stdio_client
from pydantic import BaseModel, Field, field_validator
from strands import Agent, tool
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
from strands.tools.mcp import MCPClient
from strands.types.agent import Limits
from strands_tools import http_request

from local_model import get_model


# --------------------------------------------------------------------------
# Structured output schema
# --------------------------------------------------------------------------
class Department(BaseModel):
    """A single department's headline figures."""

    name: str = Field(description="Department name")
    budget: int = Field(description="Annual budget in whole dollars")
    employee_count: int = Field(description="Number of employees in this department")


class DepartmentReport(BaseModel):
    """Summary report of all DataStream Corp departments."""

    title: str = Field(description="Short report title")
    summary: str = Field(description="Two or three sentences on what the data shows")
    departments: list[Department] = Field(
        description="One entry per department, largest budget first"
    )
    total_employees: int = Field(description="Sum of employees across all departments")

    @field_validator("total_employees")
    @classmethod
    def must_match_sum(cls, v: int, info) -> int:
        expected = sum(d.employee_count for d in info.data.get("departments", []))
        if expected and v != expected:
            raise ValueError(f"total_employees must equal the sum of departments ({expected})")
        return v


# --------------------------------------------------------------------------
# Approval hook — human sign-off before any destructive SQL
# --------------------------------------------------------------------------
DESTRUCTIVE_KEYWORDS = {
    "drop", "delete", "update", "insert", "alter",
    "truncate", "replace", "create", "attach", "detach", "vacuum",
}


def is_destructive(sql: str) -> bool:
    """True if the statement is anything other than a plain read."""
    cleaned = re.sub(r"--[^\n]*", " ", sql)
    cleaned = re.sub(r"/\*.*?\*/", " ", cleaned, flags=re.S)
    tokens = re.findall(r"[a-zA-Z_]+", cleaned.lower())
    if not tokens:
        return False
    return tokens[0] in DESTRUCTIVE_KEYWORDS or (
        ";" in cleaned and any(t in DESTRUCTIVE_KEYWORDS for t in tokens)
    )


class ApprovalHook(HookProvider):
    """Pause the agent and ask a human before any write hits the database."""

    def __init__(self, app_name: str) -> None:
        self.app_name = app_name

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.approve)

    def approve(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] != "query_db":
            return

        sql = event.tool_use["input"].get("sql", "")
        if not is_destructive(sql):
            return

        if event.agent.state.get(f"{self.app_name}-approval") == "t":
            return

        approval = event.interrupt(f"{self.app_name}-approval", reason={"sql": sql})

        if approval.lower() not in ("y", "t"):
            event.cancel_tool = "User denied permission to run this statement."

        event.agent.state.set(f"{self.app_name}-approval", approval.lower())


# --------------------------------------------------------------------------
# Shared MCP client
# --------------------------------------------------------------------------
mcp_client = MCPClient(
    lambda: stdio_client(
        StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
    )
)


# Safety cap on the agent loop. Small local models sometimes never emit a
# terminal answer — they keep calling tools forever. This bounds every
# invocation so a weak model degrades to a partial answer instead of looping.
TURN_LIMIT: Limits = {"turns": 8}


def drain_interrupts(agent_obj, result):
    """Service approval interrupts raised inside an agent loop."""
    while result.stop_reason == "interrupt":
        responses = []
        for interrupt in result.interrupts:
            if interrupt.name == "datastream-approval":
                print(f"\n  ⚠  The agent wants to run:\n     {interrupt.reason['sql']}")
                answer = input("  Allow? (y)es / (t)rust all / (N)o: ")
                responses.append({
                    "interruptResponse": {
                        "interruptId": interrupt.id,
                        "response": answer,
                    }
                })
        result = agent_obj(responses)
    return result


# --------------------------------------------------------------------------
# System prompts
# --------------------------------------------------------------------------
DATA_PROMPT = """
You are a database operator for DataStream Corp.
Call list_schema first to learn the tables, then use query_db to carry out
the request. You may run write statements when asked; a separate approval
system handles authorization, so do not refuse on safety grounds — just
issue the SQL. State plainly whether your counts filter on employee status.
"""

WEATHER_PROMPT = """
You are a weather assistant. Use http_request against the free Open-Meteo
API — no API key needed.

Geocode first:
  https://geocoding-api.open-meteo.com/v1/search?name=CITY&count=1
Then fetch:
  https://api.open-meteo.com/v1/forecast?latitude=LAT&longitude=LON&current=temperature_2m,precipitation,weather_code

Report temperature in Celsius and describe conditions in plain language.
"""

ORCHESTRATOR_PROMPT = """
You coordinate two specialists for DataStream Corp.

- data_agent: anything about the company's own database — departments,
  employees, projects, budgets, headcounts, salaries, the audit log.
- weather_agent: anything about weather, temperature, or forecasts.

Pick the right specialist for each part of the request. If a question spans
both domains, call both and combine their answers into one reply. Never
invent facts yourself — always delegate to a specialist.
"""


# --------------------------------------------------------------------------
# Specialist agents, exposed as tools
# --------------------------------------------------------------------------
@tool
def data_agent(question: str) -> str:
    """Answer questions about DataStream Corp's internal database.

    Covers departments, employees, projects, project assignments, user
    preferences, and the audit log — budgets, headcounts, salaries, hire
    dates, project status. Use for anything about the company's own records.

    Args:
        question: A natural-language question about the company data.

    Returns:
        The specialist's answer, or an error message if it failed.
    """
    try:
        specialist = Agent(
            model=get_model(),
            tools=[mcp_client],
            hooks=[ApprovalHook("datastream")],
            callback_handler=None,
            system_prompt=DATA_PROMPT,
        )
        result = drain_interrupts(specialist, specialist(question, limits=TURN_LIMIT))
        return str(result)
    except Exception as e:
        return f"Error in data agent: {e}"


@tool
def weather_agent(question: str) -> str:
    """Answer questions about current weather and forecasts for any location.

    Use for temperature, conditions, rain, wind, and forecasts anywhere in
    the world. Not for company data.

    Args:
        question: A natural-language question about the weather somewhere.

    Returns:
        The specialist's answer, or an error message if it failed.
    """
    try:
        specialist = Agent(
            model=get_model(),
            tools=[http_request],
            callback_handler=None,
            system_prompt=WEATHER_PROMPT,
        )
        return str(specialist(question, limits=TURN_LIMIT))
    except Exception as e:
        return f"Error in weather agent: {e}"


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------
orchestrator = Agent(
    model=get_model(),
    tools=[data_agent, weather_agent],
    callback_handler=None,
    system_prompt=ORCHESTRATOR_PROMPT,
)


# --------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------
if __name__ == "__main__":
    prompts = [
        "Which department has the largest budget?",                          # data only
        "What's the weather in Haifa right now?",                            # weather only
        "How many people work in Engineering, and what's it like in Tel Aviv?",  # both
    ]

    for p in prompts:
        print(f"\n=== {p}")
        print(orchestrator(p, limits=TURN_LIMIT))