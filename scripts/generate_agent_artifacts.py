#!/usr/bin/env python
"""Generate MCP and function-calling integration artifacts for CXXtract2."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.mcp_server.tool_registry import (
    TOOL_SPECS,
    build_tool_description,
    collect_component_schemas,
    export_mcp_tool_definition,
    get_input_schema,
)

MCP_DIR = ROOT / "integrations" / "mcp_server"
FUNC_DIR = ROOT / "integrations" / "function_schemas"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_yaml(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _operation_for(spec) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "operationId": spec.name.replace(".", "_"),
        "summary": spec.name,
        "description": build_tool_description(spec),
        "tags": [spec.group],
        "x-tool-name": spec.name,
        "x-tool-class": spec.tool_class,
        "x-side-effectful": spec.side_effectful,
        "x-http": {"method": spec.method, "path": spec.path},
    }

    params: list[dict[str, Any]] = []
    for path_param in spec.path_params:
        params.append(
            {
                "name": path_param,
                "in": "path",
                "required": True,
                "schema": {"type": "string", "minLength": 1},
            }
        )
    for query_param in spec.query_params:
        if query_param == "include_embedding":
            schema = {"type": "boolean", "default": False}
        else:
            schema = {"type": "string", "minLength": 1}
        params.append(
            {
                "name": query_param,
                "in": "query",
                "required": False,
                "schema": schema,
            }
        )
    if params:
        operation["parameters"] = params

    if spec.method == "POST" and spec.request_model is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{spec.request_model.__name__}"}
                }
            },
        }

    response_schema: dict[str, Any] = {"type": "object"}
    expected_output: dict[str, Any] = {}
    if spec.response_model is not None:
        response_schema = {"$ref": f"#/components/schemas/{spec.response_model.__name__}"}
        expected_output = {
            "model": spec.response_model.__name__,
            "$ref": f"#/components/schemas/{spec.response_model.__name__}",
        }

    operation["responses"] = {
        "200": {
            "description": "Successful response",
            "content": {"application/json": {"schema": response_schema}},
        }
    }
    if expected_output:
        operation["x-expected-output"] = expected_output
    return operation


def _build_openapi(components: dict[str, Any]) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for spec in TOOL_SPECS:
        op = _operation_for(spec)
        path_item = paths.setdefault(spec.path, {})
        path_item[spec.method.lower()] = op

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "CXXtract2 Tool Catalog",
            "version": "1.0.0",
            "description": "Generated tool interface for autonomous LLM agents.",
        },
        "servers": [{"url": "http://127.0.0.1:8000"}],
        "paths": paths,
        "components": {"schemas": components},
    }


def _build_functions_catalog() -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for spec in TOOL_SPECS:
        expected_output = {}
        if spec.response_model is not None:
            expected_output = {
                "model": spec.response_model.__name__,
                "$ref": f"#/components/schemas/{spec.response_model.__name__}",
            }
        catalog.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": build_tool_description(spec),
                    "parameters": get_input_schema(spec),
                },
                "x-tool-class": spec.tool_class,
                "x-side-effectful": spec.side_effectful,
                "x-http": {"method": spec.method, "path": spec.path},
                "x-expected-output": expected_output,
            }
        )
    return catalog


def _build_descriptions_md() -> str:
    lines: list[str] = []
    lines.append("# CXXtract2 Agent Tool Guidance")
    lines.append("")
    lines.append("## Decision Matrix")
    lines.append("")
    lines.append("- Use `cxxtract.query.*` fast-track tools when symbol/function identity is known and you want direct output.")
    lines.append(
        "- Use `cxxtract.explore.*` atomic tools when symbol identity is ambiguous or when you need evidence-first,"
        " bounded-cost step-by-step verification."
    )
    lines.append("- Use operational tools only for explicit workspace/context/cache/sync/vector state management.")
    lines.append("")
    lines.append("## Tool Descriptions")
    lines.append("")
    for spec in TOOL_SPECS:
        lines.append(f"### `{spec.name}`")
        lines.append("")
        lines.append(f"- Class: `{spec.tool_class}`")
        lines.append(f"- Side effectful: `{str(spec.side_effectful).lower()}`")
        lines.append(f"- HTTP: `{spec.method} {spec.path}`")
        lines.append("")
        lines.append(build_tool_description(spec))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _readme_mcp() -> str:
    return (
        "# CXXtract2 MCP Server\n\n"
        "This bundle exposes all CXXtract2 endpoints as MCP tools via JSON-RPC over stdio.\n\n"
        "## Files\n\n"
        "- `server.py`: MCP stdio server (`initialize`, `tools/list`, `tools/call`).\n"
        "- `tool_registry.py`: canonical 30-tool catalog + schemas + descriptions.\n"
        "- `http_client.py`: validated HTTP dispatch with timeout/retry profiles.\n"
        "- `schema_components.json`: shared component schemas.\n\n"
        "## Run\n\n"
        "```powershell\n"
        "python integrations/mcp_server/server.py\n"
        "```\n\n"
        "Configure target API base URL with `CXXTRACT_BASE_URL`.\n"
    )


def _readme_functions() -> str:
    return (
        "# CXXtract2 Function Calling Schemas\n\n"
        "This bundle contains OpenAPI + function-calling JSON schemas for all CXXtract2 tools.\n\n"
        "## Files\n\n"
        "- `openapi.tools.yaml`: OpenAPI 3.1 document with tool operations and refs.\n"
        "- `functions.catalog.json`: function-calling entries for each tool.\n"
        "- `components.common.json`: shared schema components.\n"
        "- `descriptions.md`: agent-facing usage guidance.\n\n"
        "## Regeneration\n\n"
        "```powershell\n"
        "python scripts/generate_agent_artifacts.py\n"
        "```\n"
    )


def main() -> None:
    components = collect_component_schemas()

    mcp_tools = [export_mcp_tool_definition(spec) for spec in TOOL_SPECS]
    _write_json(MCP_DIR / "schema_components.json", components)
    _write_json(MCP_DIR / "tools.catalog.json", {"tools": mcp_tools})
    (MCP_DIR / "README.md").write_text(_readme_mcp(), encoding="utf-8")

    functions_catalog = _build_functions_catalog()
    openapi_doc = _build_openapi(components)
    _write_json(FUNC_DIR / "components.common.json", {"components": {"schemas": components}})
    _write_json(FUNC_DIR / "functions.catalog.json", {"functions": functions_catalog})
    _write_yaml(FUNC_DIR / "openapi.tools.yaml", openapi_doc)
    (FUNC_DIR / "descriptions.md").write_text(_build_descriptions_md(), encoding="utf-8")
    (FUNC_DIR / "README.md").write_text(_readme_functions(), encoding="utf-8")

    print(f"Generated MCP bundle at: {MCP_DIR}")
    print(f"Generated function schema bundle at: {FUNC_DIR}")


if __name__ == "__main__":
    main()
