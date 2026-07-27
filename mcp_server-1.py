# TODO: Import FastMCP and sqlite3

# TODO: Create FastMCP server named "DataStream DB"

# TODO: Create query_db tool that executes SQL queries

# TODO: Run the server

# In agent.py:
# TODO: Create MCPClient with stdio transport
# TODO: Connect agent to MCP tools using context manager

import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP   # if the workshop pinned fastmcp v2: from fastmcp import FastMCP

mcp = FastMCP("DataStream DB")

DB_PATH = Path(__file__).parent / "datastream_corp.db"


@mcp.tool()
def query_db(sql: str) -> str:
    """Execute a SQL statement against the DataStream Corp SQLite database.

    Supports both reads (SELECT) and writes (INSERT, UPDATE, DELETE, DROP).

    Args:
        sql: Any valid SQLite statement.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql).fetchall()
        if not rows:
            return "No rows returned."
        header = " | ".join(rows[0].keys())
        body = "\n".join(" | ".join(str(v) for v in row) for row in rows)
        return f"{header}\n{body}"
    except sqlite3.Error as e:
        return f"SQL error: {e}"
    finally:
        conn.close()


@mcp.tool()
def list_schema() -> str:
    """Return the CREATE TABLE statements for every table, so queries can be written correctly."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
        ).fetchall()
        return "\n\n".join(r[0] for r in rows)
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run(transport="stdio")