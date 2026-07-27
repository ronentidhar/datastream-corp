"""DataStream Corp analyst agent — MCP tools + session + summarizing context."""

import sys

from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.agent.conversation_manager import SummarizingConversationManager
from strands.session.file_session_manager import FileSessionManager
from strands.tools.mcp import MCPClient

mcp_client = MCPClient(
    lambda: stdio_client(
        StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
    )
)

session_manager = FileSessionManager(session_id="datastream-analyst")

conversation_manager = SummarizingConversationManager(
    summary_ratio=0.5,             # summarize the oldest 50% when reducing
    preserve_recent_messages=2,    # always keep the last 2 verbatim
)

agent = Agent(
    tools=[mcp_client],
    session_manager=session_manager,
    conversation_manager=conversation_manager,
    callback_handler=None,
    system_prompt=(
        "You are a data analyst for DataStream Corp. "
        "Call list_schema first to learn the tables, then use query_db "
        "with valid SQLite SELECT statements to answer questions."
    ),
)

if __name__ == "__main__":
    prompts = [
        "Remember this: my favorite department is Operations.",
        "Which department has the largest budget?",
        "How many employees are in Engineering?",
        "What did I say my favorite department was?",
    ]

    for i, p in enumerate(prompts, 1):
        print(f"\n=== [{i}] {p}")
        print(agent(p))
        print(f"--- messages after turn {i}: {len(agent.messages)}")

    print("\n=== final context ===")
    for m in agent.messages:
        print(f"  {m['role']:9} {str(m['content'])[:100]}")