# TODO: Import FileSessionManager

# TODO: Create FileSessionManager with session_id and storage_dir

# TODO: Add session_manager to Agent (keep existing MCP tools from Task 2)

# TODO: Test by introducing yourself, then restart script to verify memory

"""DataStream Corp analyst agent — MCP tools + session + sliding window."""

import sys

from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.session.file_session_manager import FileSessionManager
from strands.tools.mcp import MCPClient

from local_model import get_model

# --- MCP server (stdio) ---------------------------------------------------
mcp_client = MCPClient(
    lambda: stdio_client(
        StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
    )
)

# --- Session persistence (from the earlier exercise) ----------------------
session_manager = FileSessionManager(session_id="datastream-analyst")

# --- Conversation window --------------------------------------------------
conversation_manager = SlidingWindowConversationManager(
    window_size=6,
    should_truncate_results=True,
    # per_turn removed — trim only after the turn completes
)

# --- Agent ----------------------------------------------------------------
agent = Agent(
    model=get_model(),
    tools=[mcp_client],
    session_manager=session_manager,
    conversation_manager=conversation_manager,
    callback_handler=None,       # no streaming; we print the result ourselves
    system_prompt=(
        "You are a data analyst for DataStream Corp. "
        "Call list_schema first to learn the tables, then use query_db "
        "with valid SQLite SELECT statements to answer questions."
    ),
)

# --- Three-message memory test -------------------------------------------
if __name__ == "__main__":
    prompts = [
        "Remember this: my favorite department is Operations.",
        "Which department has the largest budget?",
        "What did I say my favorite department was?",
    ]

    for i, p in enumerate(prompts, 1):
        print(f"\n=== [{i}] {p}")
        print(agent(p))
        print(f"--- messages after turn {i}: {len(agent.messages)}")

    print("\n=== final window ===")
    for m in agent.messages:
        print(f"  {m['role']:9} {str(m['content'])[:90]}")