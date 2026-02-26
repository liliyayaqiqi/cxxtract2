#!/usr/bin/env python
"""CXXtract2 API smoke test runner.

Run against a live server:
  python scripts/api_smoke_test.py --base-url http://127.0.0.1:8000 \
    --workspace-id ws_main --root-path F:/dev/ws_main --manifest-path F:/dev/ws_main/workspace.yaml \
    --repo-id repoA --commit-sha 0123456789abcdef0123456789abcdef01234567
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class ApiClient:
    base_url: str
    timeout_s: float = 20.0

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        url = f"{self.base_url.rstrip('/')}{path}"
        body: bytes | None = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url=url, data=body, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return int(resp.status), json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload_out = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload_out = {"raw": raw}
            return int(exc.code), payload_out

    def get(self, path: str) -> tuple[int, Any]:
        return self._request("GET", path, None)

    def post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        return self._request("POST", path, payload)


def expect_status(name: str, status: int, expected: int | set[int]) -> None:
    if isinstance(expected, int):
        ok = status == expected
        expected_text = str(expected)
    else:
        ok = status in expected
        expected_text = ",".join(str(x) for x in sorted(expected))
    if not ok:
        raise RuntimeError(f"{name} failed: HTTP {status}, expected {expected_text}")
    print(f"[PASS] {name}: HTTP {status}")


def run(args: argparse.Namespace) -> int:
    client = ApiClient(base_url=args.base_url, timeout_s=args.timeout_s)

    # 1) health
    status, data = client.get("/health")
    expect_status("GET /health", status, 200)
    print(f"       status={data.get('status')} version={data.get('version')}")

    # 2) workspace lifecycle
    status, _ = client.post(
        "/workspace/register",
        {
            "workspace_id": args.workspace_id,
            "root_path": args.root_path,
            "manifest_path": args.manifest_path,
        },
    )
    expect_status("POST /workspace/register", status, 200)

    status, _ = client.get(f"/workspace/{urllib.parse.quote(args.workspace_id)}")
    expect_status("GET /workspace/{workspace_id}", status, 200)

    status, _ = client.post(f"/workspace/{urllib.parse.quote(args.workspace_id)}/refresh-manifest", {})
    expect_status("POST /workspace/{workspace_id}/refresh-manifest", status, 200)

    # 3) context + query + cache endpoints
    context_id = f"{args.workspace_id}:pr:smoke"
    status, _ = client.post(
        "/context/create-pr-overlay",
        {
            "workspace_id": args.workspace_id,
            "pr_id": "smoke",
            "base_ref": "main",
            "head_ref": "smoke",
            "context_id": context_id,
        },
    )
    expect_status("POST /context/create-pr-overlay", status, 200)

    status, _ = client.post(
        "/query/references",
        {"workspace_id": args.workspace_id, "symbol": args.symbol},
    )
    expect_status("POST /query/references", status, 200)

    status, _ = client.post(
        "/query/definition",
        {"workspace_id": args.workspace_id, "symbol": args.symbol},
    )
    expect_status("POST /query/definition", status, 200)

    status, _ = client.post(
        "/query/call-graph",
        {"workspace_id": args.workspace_id, "symbol": args.symbol, "direction": "both"},
    )
    expect_status("POST /query/call-graph", status, 200)

    status, _ = client.post(
        "/query/file-symbols",
        {"workspace_id": args.workspace_id, "file_key": args.file_key},
    )
    expect_status("POST /query/file-symbols", status, 200)

    status, _ = client.post(
        "/cache/invalidate",
        {"workspace_id": args.workspace_id, "context_id": "", "file_keys": [args.file_key]},
    )
    expect_status("POST /cache/invalidate", status, 200)

    # 4) webhook endpoint
    status, webhook_data = client.post(
        "/webhooks/gitlab",
        {
            "event_type": "merge_request",
            "payload": {
                "workspace_id": args.workspace_id,
                "repo_id": args.repo_id or "",
                "event_sha": args.commit_sha or "",
                "branch": args.branch or "",
            },
        },
    )
    expect_status("POST /webhooks/gitlab", status, 200)
    print(f"       index_job_id={webhook_data.get('index_job_id', '')}")

    # 5) sync endpoints (optional)
    if args.repo_id and args.commit_sha:
        status, sync_job = client.post(
            f"/workspace/{urllib.parse.quote(args.workspace_id)}/sync-repo",
            {
                "repo_id": args.repo_id,
                "commit_sha": args.commit_sha,
                "branch": args.branch or "",
                "force_clean": args.force_clean,
            },
        )
        expect_status("POST /workspace/{workspace_id}/sync-repo", status, 200)
        job_id = sync_job.get("job_id", "")
        if job_id:
            status, _ = client.get(f"/sync-jobs/{urllib.parse.quote(job_id)}")
            expect_status("GET /sync-jobs/{job_id}", status, 200)

        status, _ = client.post(
            f"/workspace/{urllib.parse.quote(args.workspace_id)}/sync-batch",
            {"targets": [{"repo_id": args.repo_id, "commit_sha": args.commit_sha}]},
        )
        expect_status("POST /workspace/{workspace_id}/sync-batch", status, 200)

        status, _ = client.get(
            f"/workspace/{urllib.parse.quote(args.workspace_id)}/repos/{urllib.parse.quote(args.repo_id)}/sync-status"
        )
        expect_status("GET /workspace/{workspace_id}/repos/{repo_id}/sync-status", status, 200)
    else:
        print("[SKIP] sync endpoints (provide --repo-id and --commit-sha)")

    # 6) vector endpoints (optional; auto-skip if disabled/unavailable)
    embedding = [0.0] * args.embedding_dim
    upsert_payload = {
        "workspace_id": args.workspace_id,
        "repo_id": args.repo_id or "repoA",
        "commit_sha": args.commit_sha or ("a" * 40),
        "branch": args.branch or "main",
        "summary_text": "smoke summary text",
        "embedding_model": args.embedding_model,
        "embedding": embedding,
        "metadata": {"source": "api_smoke_test"},
    }
    status, _ = client.post("/commit-diff-summaries/upsert", upsert_payload)
    if status == 503:
        print("[SKIP] vector endpoints (service returned 503: vector disabled/unavailable)")
    else:
        expect_status("POST /commit-diff-summaries/upsert", status, 200)
        status, _ = client.post(
            "/commit-diff-summaries/search",
            {
                "workspace_id": args.workspace_id,
                "query_embedding": embedding,
                "top_k": 5,
                "repo_ids": [args.repo_id] if args.repo_id else [],
                "branches": [args.branch] if args.branch else [],
                "score_threshold": 0.0,
            },
        )
        expect_status("POST /commit-diff-summaries/search", status, 200)

        status, _ = client.get(
            "/commit-diff-summaries/"
            f"{urllib.parse.quote(args.workspace_id)}/"
            f"{urllib.parse.quote(args.repo_id or 'repoA')}/"
            f"{urllib.parse.quote(args.commit_sha or ('a' * 40))}"
        )
        expect_status("GET /commit-diff-summaries/{workspace_id}/{repo_id}/{commit_sha}", status, 200)

    # 7) expire context
    time.sleep(0.05)
    status, _ = client.post(f"/context/{urllib.parse.quote(context_id)}/expire", {})
    expect_status("POST /context/{context_id}/expire", status, 200)

    print("\nAll smoke checks finished.")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run CXXtract2 API smoke tests against a live server.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--workspace-id", default="ws_main")
    parser.add_argument("--root-path", required=True)
    parser.add_argument("--manifest-path", required=True)
    parser.add_argument("--symbol", default="main")
    parser.add_argument("--file-key", default="repoA:src/main.cpp")
    parser.add_argument("--repo-id", default="")
    parser.add_argument("--commit-sha", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--force-clean", action="store_true", default=True)
    parser.add_argument("--timeout-s", type=float, default=20.0)
    parser.add_argument("--embedding-dim", type=int, default=1536)
    parser.add_argument("--embedding-model", default="text-embedding-3-large")
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(run(parse_args(sys.argv[1:])))
    except Exception as exc:
        print(f"[FAIL] {exc}")
        raise SystemExit(1)
