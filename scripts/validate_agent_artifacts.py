#!/usr/bin/env python
"""Validate generated MCP/function integration artifacts."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.mcp_server.tool_registry import TOOL_SPECS, route_inventory


def _read_text(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"missing file: {path}")
    return path.read_text(encoding="utf-8")


def _route_inventory_from_routes_py(path: Path) -> set[tuple[str, str]]:
    text = _read_text(path)
    pattern = re.compile(r'@router\.(get|post)\(\s*"([^"]+)"', re.DOTALL)
    out: set[tuple[str, str]] = set()
    for method, route in pattern.findall(text):
        out.add((method.upper(), route))
    return out


def _load_json(path: Path) -> Any:
    return json.loads(_read_text(path))


def _validate() -> None:
    expected_tools = {spec.name for spec in TOOL_SPECS}
    if len(expected_tools) != 30:
        raise RuntimeError(f"expected 30 tools, got {len(expected_tools)}")

    route_file = ROOT / "src" / "cxxtract" / "api" / "routes.py"
    route_api = _route_inventory_from_routes_py(route_file)
    route_tools = route_inventory()
    if route_api != route_tools:
        missing = sorted(route_api - route_tools)
        extra = sorted(route_tools - route_api)
        raise RuntimeError(f"route parity mismatch missing={missing} extra={extra}")

    mcp_schema = ROOT / "integrations" / "mcp_server" / "schema_components.json"
    mcp_catalog = ROOT / "integrations" / "mcp_server" / "tools.catalog.json"
    openapi_path = ROOT / "integrations" / "function_schemas" / "openapi.tools.yaml"
    func_catalog = ROOT / "integrations" / "function_schemas" / "functions.catalog.json"
    common_json = ROOT / "integrations" / "function_schemas" / "components.common.json"
    descriptions_md = ROOT / "integrations" / "function_schemas" / "descriptions.md"

    for path in (mcp_schema, mcp_catalog, openapi_path, func_catalog, common_json, descriptions_md):
        if not path.exists():
            raise RuntimeError(f"artifact missing: {path}")

    tools_doc = _load_json(mcp_catalog)
    tools = tools_doc.get("tools", [])
    tool_names = {entry.get("name", "") for entry in tools}
    if tool_names != expected_tools:
        raise RuntimeError("MCP catalog tool names do not match registry")

    funcs_doc = _load_json(func_catalog)
    functions = funcs_doc.get("functions", [])
    func_names = {entry.get("function", {}).get("name", "") for entry in functions}
    if func_names != expected_tools:
        raise RuntimeError("Function catalog tool names do not match registry")

    openapi_doc = yaml.safe_load(_read_text(openapi_path))
    ops: set[str] = set()
    for path_item in openapi_doc.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for op in path_item.values():
            if isinstance(op, dict) and op.get("x-tool-name"):
                ops.add(op["x-tool-name"])
    if ops != expected_tools:
        raise RuntimeError("OpenAPI tool names do not match registry")

    mutating_tools = [spec.name for spec in TOOL_SPECS if spec.side_effectful]
    for entry in tools:
        name = entry.get("name", "")
        side = bool(entry.get("x-side-effectful", False))
        if (name in mutating_tools) != side:
            raise RuntimeError(f"side-effect labeling mismatch for tool: {name}")


def main() -> int:
    try:
        _validate()
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return 1
    print("[PASS] Agent artifacts validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
