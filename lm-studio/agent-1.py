# TODO: Import FileSessionManager

# TODO: Create FileSessionManager with session_id and storage_dir

# TODO: Add session_manager to Agent (keep existing MCP tools from Task 2)

# TODO: Test by introducing yourself, then restart script to verify memory

import sys

from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.tools.mcp import MCPClient

from local_model import get_model

mcp_client = MCPClient(
    lambda: stdio_client(
        StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
    )
)

agent = Agent(
    model=get_model(),
    tools=[mcp_client],
    callback_handler=None,          # stops the double-print
    system_prompt=(
        "You are a data analyst for DataStream Corp. "
        "Call list_schema first to learn the tables, then use query_db "
        "with valid SQLite SELECT statements to answer questions."
    ),
)




# print(agent("Which department has the largest budget, and how many employees are in it?"))

# if __name__ == "__main__":
#    # Test your agent
#    response = agent("What departments do we have?")
