"""HTTP wrapper used by MCP tool dispatch."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from integrations.mcp_server.tool_registry import ToolSpec


@dataclass(frozen=True)
class ToolClassConfig:
    """Timeout/retry behavior by tool class."""

    timeout_s: float
    retries: int


DEFAULT_CLASS_CONFIG: dict[str, ToolClassConfig] = {
    "aggregated": ToolClassConfig(timeout_s=20.0, retries=1),
    "atomic": ToolClassConfig(timeout_s=30.0, retries=1),
    "operational": ToolClassConfig(timeout_s=60.0, retries=1),
}


class HttpToolError(RuntimeError):
    """Structured HTTP/transport failure for tool calls."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self.envelope = envelope
        super().__init__(str(envelope))


class CxxtractHttpClient:
    """Thin HTTP client for tool dispatch."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("CXXTRACT_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
        self._allow_side_effect_retry = (
            os.getenv("CXXTRACT_MCP_RETRY_SIDE_EFFECTFUL", "false").strip().lower() == "true"
        )

    def _config_for(self, spec: ToolSpec) -> ToolClassConfig:
        timeout_env = os.getenv(f"CXXTRACT_MCP_TIMEOUT_{spec.tool_class.upper()}", "").strip()
        retries_env = os.getenv(f"CXXTRACT_MCP_RETRIES_{spec.tool_class.upper()}", "").strip()
        base = DEFAULT_CLASS_CONFIG[spec.tool_class]
        timeout_s = float(timeout_env) if timeout_env else base.timeout_s
        retries = int(retries_env) if retries_env else base.retries
        if spec.side_effectful and spec.method == "POST" and not self._allow_side_effect_retry:
            retries = 0
        return ToolClassConfig(timeout_s=timeout_s, retries=max(0, retries))

    @staticmethod
    def _format_path(path_template: str, path_values: dict[str, Any]) -> str:
        path = path_template
        for key, value in path_values.items():
            path = path.replace("{" + key + "}", quote(str(value), safe=""))
        return path

    @staticmethod
    def _extract_error_code(detail: Any) -> str:
        if isinstance(detail, dict):
            code = detail.get("code")
            if isinstance(code, str) and code:
                return code
        if isinstance(detail, str) and detail:
            return detail
        return "http_error"

    def call(
        self,
        *,
        spec: ToolSpec,
        validated: dict[str, Any],
        request_id: str | int | None,
    ) -> dict[str, Any]:
        """Dispatch one validated tool call to service HTTP endpoint."""
        config = self._config_for(spec)
        path = self._format_path(spec.path, validated.get("path", {}))
        url = f"{self.base_url}{path}"
        params = validated.get("query") or None
        body = validated.get("body")

        attempts = config.retries + 1
        last_error: dict[str, Any] | None = None

        for attempt in range(1, attempts + 1):
            try:
                with httpx.Client(timeout=config.timeout_s) as client:
                    if spec.method == "GET":
                        resp = client.get(url, params=params)
                    else:
                        resp = client.post(url, params=params, json=body)
            except Exception as exc:
                last_error = {
                    "http_status": 0,
                    "error_code": "transport_error",
                    "detail": str(exc),
                    "tool_name": spec.name,
                    "request_id": request_id,
                    "attempt": attempt,
                }
                continue

            if 200 <= resp.status_code < 300:
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text}

            detail: Any = ""
            try:
                payload = resp.json()
                detail = payload.get("detail", payload)
            except Exception:
                detail = resp.text

            last_error = {
                "http_status": int(resp.status_code),
                "error_code": self._extract_error_code(detail),
                "detail": detail,
                "tool_name": spec.name,
                "request_id": request_id,
                "attempt": attempt,
            }

        assert last_error is not None
        raise HttpToolError(last_error)

