# CXXtract2 Function Calling Schemas

This bundle contains OpenAPI + function-calling JSON schemas for all CXXtract2 tools.

## Files

- `openapi.tools.yaml`: OpenAPI 3.1 document with tool operations and refs.
- `functions.catalog.json`: function-calling entries for each tool.
- `components.common.json`: shared schema components.
- `descriptions.md`: agent-facing usage guidance.

## Regeneration

```powershell
python scripts/generate_agent_artifacts.py
```
