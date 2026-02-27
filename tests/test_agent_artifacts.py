from __future__ import annotations

import json
from pathlib import Path

import yaml

from integrations.mcp_server.tool_registry import TOOL_SPECS, build_tool_description, route_inventory


ROOT = Path(__file__).resolve().parents[1]


def test_tool_registry_covers_expected_surface():
    assert len(TOOL_SPECS) == 30
    names = {spec.name for spec in TOOL_SPECS}
    assert len(names) == 30
    for spec in TOOL_SPECS:
        assert spec.name.startswith("cxxtract.")
        assert spec.method in {"GET", "POST"}
        assert spec.path.startswith("/")


def test_tool_descriptions_include_agent_guidance_sections():
    for spec in TOOL_SPECS:
        desc = build_tool_description(spec)
        assert "What this tool does:" in desc
        assert "When to use:" in desc
        assert "When not to use:" in desc
        assert "Input prerequisites:" in desc
        assert "Expected output:" in desc


def test_generated_catalogs_exist_and_match_registry():
    expected = {spec.name for spec in TOOL_SPECS}

    mcp_catalog_path = ROOT / "integrations" / "mcp_server" / "tools.catalog.json"
    fn_catalog_path = ROOT / "integrations" / "function_schemas" / "functions.catalog.json"
    openapi_path = ROOT / "integrations" / "function_schemas" / "openapi.tools.yaml"

    assert mcp_catalog_path.exists()
    assert fn_catalog_path.exists()
    assert openapi_path.exists()

    mcp_catalog = json.loads(mcp_catalog_path.read_text(encoding="utf-8"))
    mcp_names = {entry["name"] for entry in mcp_catalog["tools"]}
    assert mcp_names == expected

    fn_catalog = json.loads(fn_catalog_path.read_text(encoding="utf-8"))
    fn_names = {entry["function"]["name"] for entry in fn_catalog["functions"]}
    assert fn_names == expected

    openapi_doc = yaml.safe_load(openapi_path.read_text(encoding="utf-8"))
    op_names: set[str] = set()
    for path_item in openapi_doc.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for op in path_item.values():
            if isinstance(op, dict) and op.get("x-tool-name"):
                op_names.add(op["x-tool-name"])
    assert op_names == expected


def test_tool_routes_match_router_inventory():
    route_file = ROOT / "src" / "cxxtract" / "api" / "routes.py"
    text = route_file.read_text(encoding="utf-8")
    found: set[tuple[str, str]] = set()
    import re

    for method, path in re.findall(r'@router\.(get|post)\(\s*"([^"]+)"', text, re.DOTALL):
        found.add((method.upper(), path))

    assert route_inventory() == found
