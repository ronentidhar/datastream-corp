# TODO: Import FileSessionManager

# TODO: Create FileSessionManager with session_id and storage_dir

# TODO: Add session_manager to Agent (keep existing MCP tools from Task 2)

# TODO: Test by introducing yourself, then restart script to verify memory

# Hooks & Interrupts (Human-in-the-Loop)
# TODO: Import HookProvider, HookRegistry, BeforeToolCallEvent

# TODO: Create ApprovalHook class that checks for destructive queries

# TODO: Add hooks parameter to Agent with ApprovalHook instance

# TODO: Implement interrupt handling loop to ask for user approval

# TODO: Test with destructive query and verify approval is required

"""DataStream Corp analyst agent.

MCP tools over stdio + human approval on destructive SQL + structured output.
"""

import re
import sys
from typing import Any

from mcp import StdioServerParameters, stdio_client
from pydantic import BaseModel, Field, field_validator
from strands import Agent
from strands.agent.conversation_manager import SummarizingConversationManager
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
from strands.tools.mcp import MCPClient

# from strands.session.file_session_manager import FileSessionManager


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
# Approval hook
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
# Agent
# --------------------------------------------------------------------------
mcp_client = MCPClient(
    lambda: stdio_client(
        StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
    )
)

agent = Agent(
    tools=[mcp_client],
    hooks=[ApprovalHook("datastream")],
    # session_manager=FileSessionManager(session_id="datastream-hooks"),
    conversation_manager=SummarizingConversationManager(
        summary_ratio=0.5,
        preserve_recent_messages=2,
    ),
    callback_handler=None,
    system_prompt=(
        "You are a database operator for DataStream Corp. "
        "Call list_schema first to learn the tables, then use query_db to "
        "carry out the user's request. You may run write statements when asked; "
        "a separate approval system handles authorization, so do not refuse "
        "on safety grounds — just issue the SQL."
    ),
)


def run(prompt, structured_output_model=None):
    """Invoke the agent, servicing any approval interrupts until it finishes."""
    result = agent(prompt, structured_output_model=structured_output_model)

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
        result = agent(responses)

    return result


# --------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------
if __name__ == "__main__":
    # 1) Plain read — no approval, free-text answer
    print("\n=== Read query")
    print(run("How many employees are in Engineering?").message["content"][0]["text"])

    # 2) Destructive write — approval hook interrupts
    print("\n=== Destructive query")
    print(run("Delete all rows from the audit_log table.").message["content"][0]["text"])

    # 3) Structured output
    print("\n=== Structured report")
    result = run(
        "Produce a report on every department: name, budget, and employee count.",
        structured_output_model=DepartmentReport,
    )
    report: DepartmentReport = result.structured_output

    print(f"\n{report.title}")
    print(f"{report.summary}\n")
    for d in report.departments:
        print(f"  {d.name:12} ${d.budget:>9,}  {d.employee_count:>4} employees")
    print(f"  {'-' * 40}")
    print(f"  {'TOTAL':12} {'':>10}  {report.total_employees:>4} employees")