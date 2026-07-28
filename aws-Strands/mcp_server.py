from fastmcp import FastMCP
import requests
import urllib.parse

mcp = FastMCP("DataStream DB")

@mcp.tool()
def query_db(query: str) -> str:
    """Execute SQL operations on the DataStream Corp SQLite database. Supports full CRUD operations: SELECT (read), INSERT (create), UPDATE (modify), DELETE (remove). Takes a SQL statement as input and returns the results."""
    # NOTE: In real production, never accept raw SQL queries like this!
    # This is only for lab purposes to simulate enterprise database access.
    try:
        # Encode SQL query for URL
        encoded_query = urllib.parse.quote(query)
        url = f"https://d1ck76obc96z7d.cloudfront.net/sql?sql_query={encoded_query}"
        
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        return response.text
    except Exception as e:
        return f"Error executing query: {str(e)}"

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
