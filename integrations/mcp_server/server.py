"""JSON-RPC MCP server exposing CXXtract2 tools over stdio."""

from __future__ import annotations

import json
import sys
from typing import Any

from integrations.mcp_server.http_client import CxxtractHttpClient, HttpToolError
from integrations.mcp_server.tool_registry import TOOL_SPECS, export_mcp_tool_definition, get_tool_spec, validate_arguments

SERVER_NAME = "cxxtract-mcp-server"
SERVER_VERSION = "0.1.0"


def _read_message() -> dict[str, Any] | None:
    """Read one content-length framed JSON-RPC message from stdin."""
    content_length = 0
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            break
        if text.lower().startswith("content-length:"):
            content_length = int(text.split(":", 1)[1].strip())

    if content_length <= 0:
        return None
    payload = sys.stdin.buffer.read(content_length)
    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def _write_message(payload: dict[str, Any]) -> None:
    """Write one content-length framed JSON-RPC message to stdout."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    sys.stdout.buffer.write(header)
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def _result(request_id: str | int | None, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: str | int | None, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


def _tool_result(data: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return {
        "isError": is_error,
        "content": [{"type": "text", "text": text}],
        "structuredContent": data,
    }


def _handle_tools_call(client: CxxtractHttpClient, request: dict[str, Any]) -> dict[str, Any]:
    request_id = request.get("id")
    params = request.get("params", {})
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return _error(request_id, -32602, "tools/call requires non-empty params.name")

    spec = get_tool_spec(name)
    if spec is None:
        return _error(request_id, -32602, f"unknown tool: {name}")

    raw_args = params.get("arguments", {})
    try:
        validated = validate_arguments(spec, raw_args if isinstance(raw_args, dict) else None)
    except Exception as exc:
        return _result(request_id, _tool_result(
            {
                "http_status": 0,
                "error_code": "validation_error",
                "detail": str(exc),
                "tool_name": spec.name,
                "request_id": request_id,
            },
            is_error=True,
        ))

    try:
        data = client.call(spec=spec, validated=validated, request_id=request_id)
    except HttpToolError as exc:
        return _result(request_id, _tool_result(exc.envelope, is_error=True))
    except Exception as exc:
        return _result(request_id, _tool_result(
            {
                "http_status": 0,
                "error_code": "unexpected_error",
                "detail": str(exc),
                "tool_name": spec.name,
                "request_id": request_id,
            },
            is_error=True,
        ))

    return _result(request_id, _tool_result(data, is_error=False))


def _handle_request(client: CxxtractHttpClient, request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method", "")

    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        tools = [export_mcp_tool_definition(spec) for spec in TOOL_SPECS]
        return _result(request_id, {"tools": tools})
    if method == "tools/call":
        return _handle_tools_call(client, request)
    return _error(request_id, -32601, f"method not found: {method}")


def main() -> None:
    client = CxxtractHttpClient()
    while True:
        request = _read_message()
        if request is None:
            break
        try:
            response = _handle_request(client, request)
        except Exception as exc:
            response = _error(request.get("id"), -32000, "server_error", {"detail": str(exc)})
        if response is not None:
            _write_message(response)


if __name__ == "__main__":
    main()

