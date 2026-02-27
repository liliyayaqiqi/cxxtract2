# CXXtract2 MCP Server

This bundle exposes all CXXtract2 endpoints as MCP tools via JSON-RPC over stdio.

## Files

- `server.py`: MCP stdio server (`initialize`, `tools/list`, `tools/call`).
- `tool_registry.py`: canonical 30-tool catalog + schemas + descriptions.
- `http_client.py`: validated HTTP dispatch with timeout/retry profiles.
- `schema_components.json`: shared component schemas.

## Run

```powershell
python integrations/mcp_server/server.py
```

Configure target API base URL with `CXXTRACT_BASE_URL`.
