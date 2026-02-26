#!/usr/bin/env python
"""Granular CXXtract2 API smoke tests for real dev environments.

Examples:
  python scripts/api_smoke_test.py --list-tests
  python scripts/api_smoke_test.py --base-url http://127.0.0.1:8000 \
    --workspace-id ws_main --root-path F:/dev/ws_main --manifest-path F:/dev/ws_main/workspace.yaml
  python scripts/api_smoke_test.py --tests health,workspace_register,query_references
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml


@dataclass
class ApiClient:
    base_url: str
    timeout_s: float = 20.0

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        url = f"{self.base_url.rstrip('/')}{path}"
        data: bytes | None = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url=url, data=data, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                body = json.loads(raw) if raw else {}
                return int(resp.status), body
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {"raw": raw}
            return int(exc.code), body

    def get(self, path: str) -> tuple[int, Any]:
        return self._request("GET", path, None)

    def post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        return self._request("POST", path, payload)


@dataclass
class TestResult:
    name: str
    status: str  # pass|skip|fail
    message: str


class SmokeRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.client = ApiClient(args.base_url, args.timeout_s)
        self.state: dict[str, Any] = {}

        self.tests: list[tuple[str, Callable[[], TestResult]]] = [
            ("health", self.test_health),
            ("rg_basic", self.test_rg_basic),
            ("workspace_register", self.test_workspace_register),
            ("workspace_get", self.test_workspace_get),
            ("workspace_refresh", self.test_workspace_refresh),
            ("context_create_overlay", self.test_context_create_overlay),
            ("context_expire", self.test_context_expire),
            ("query_references", self.test_query_references),
            ("query_definition", self.test_query_definition),
            ("query_call_graph", self.test_query_call_graph),
            ("query_file_symbols", self.test_query_file_symbols),
            ("explore_list_candidates", self.test_explore_list_candidates),
            ("explore_classify_freshness", self.test_explore_classify_freshness),
            ("explore_fetch_references", self.test_explore_fetch_references),
            ("explore_get_confidence", self.test_explore_get_confidence),
            ("cache_invalidate", self.test_cache_invalidate),
            ("webhook_gitlab", self.test_webhook_gitlab),
            ("sync_repo", self.test_sync_repo),
            ("sync_job_get", self.test_sync_job_get),
            ("sync_batch", self.test_sync_batch),
            ("sync_status", self.test_sync_status),
            ("sync_all_repos", self.test_sync_all_repos),
            ("vector_upsert", self.test_vector_upsert),
            ("vector_search", self.test_vector_search),
            ("vector_get", self.test_vector_get),
        ]

    def _entry_repos(self) -> list[str]:
        raw = (self.args.entry_repos or "").strip()
        if not raw:
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]

    def _query_payload_base(self, symbol: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "workspace_id": self.args.workspace_id,
            "symbol": symbol,
            "scope": {
                "entry_repos": self._entry_repos(),
                "max_repo_hops": self.args.max_repo_hops,
            },
        }
        if self.args.max_recall_files is not None:
            payload["max_recall_files"] = self.args.max_recall_files
        if self.args.max_parse_workers is not None:
            payload["max_parse_workers"] = self.args.max_parse_workers
        return payload

    def _ok(self, name: str, message: str = "") -> TestResult:
        return TestResult(name, "pass", message)

    def _skip(self, name: str, message: str) -> TestResult:
        return TestResult(name, "skip", message)

    def _fail(self, name: str, message: str) -> TestResult:
        return TestResult(name, "fail", message)

    def _expect(self, name: str, status: int, expected: int | set[int], body: Any) -> None:
        if isinstance(expected, int):
            if status != expected:
                raise RuntimeError(f"HTTP {status}, expected {expected}, body={body}")
            return
        if status not in expected:
            raise RuntimeError(f"HTTP {status}, expected one of {sorted(expected)}, body={body}")

    def _dump_response(self, test_name: str, body: Any) -> None:
        if not self.args.print_response:
            return
        print(f"[RESPONSE] {test_name}")
        try:
            print(json.dumps(body, ensure_ascii=False, indent=2))
        except Exception:
            print(str(body))

    def _workspace_path(self) -> str:
        return f"/workspace/{urllib.parse.quote(self.args.workspace_id)}"

    def ensure_workspace(self) -> None:
        if self.state.get("workspace_registered"):
            return
        status, body = self.client.post(
            "/workspace/register",
            {
                "workspace_id": self.args.workspace_id,
                "root_path": self.args.root_path,
                "manifest_path": self.args.manifest_path,
            },
        )
        self._expect("workspace_register", status, 200, body)
        self.state["workspace_registered"] = True

    def ensure_overlay_context(self) -> str:
        existing = self.state.get("overlay_context_id")
        if existing:
            return str(existing)
        self.ensure_workspace()
        context_id = f"{self.args.workspace_id}:pr:smoke"
        status, body = self.client.post(
            "/context/create-pr-overlay",
            {
                "workspace_id": self.args.workspace_id,
                "pr_id": "smoke",
                "base_ref": "main",
                "head_ref": "smoke",
                "context_id": context_id,
            },
        )
        self._expect("context_create_overlay", status, 200, body)
        self.state["overlay_context_id"] = context_id
        return context_id

    def ensure_sync_job(self) -> str | None:
        existing = self.state.get("sync_job_id")
        if existing:
            return str(existing)
        if not self.args.repo_id or not self.args.commit_sha:
            return None
        self.ensure_workspace()
        status, body = self.client.post(
            f"{self._workspace_path()}/sync-repo",
            {
                "repo_id": self.args.repo_id,
                "commit_sha": self.args.commit_sha,
                "branch": self.args.branch,
                "force_clean": self.args.force_clean,
            },
        )
        self._expect("sync_repo", status, 200, body)
        job_id = str(body.get("job_id", ""))
        if not job_id:
            raise RuntimeError(f"sync-repo returned no job_id: body={body}")
        self.state["sync_job_id"] = job_id
        return job_id

    def ensure_vector_record(self) -> tuple[str, str] | None:
        repo_id = self.args.repo_id or "repoA"
        commit_sha = self.args.commit_sha or ("a" * 40)
        key = f"{repo_id}:{commit_sha}:{self.args.embedding_model}"
        if self.state.get("vector_record_key") == key:
            return repo_id, commit_sha

        self.ensure_workspace()
        embedding = [0.0] * self.args.embedding_dim
        payload = {
            "workspace_id": self.args.workspace_id,
            "repo_id": repo_id,
            "commit_sha": commit_sha,
            "branch": self.args.branch or "main",
            "summary_text": "smoke summary text",
            "embedding_model": self.args.embedding_model,
            "embedding": embedding,
            "metadata": {"source": "api_smoke_test"},
        }
        status, body = self.client.post("/commit-diff-summaries/upsert", payload)
        if status == 503:
            return None
        self._expect("vector_upsert", status, 200, body)
        self.state["vector_record_key"] = key
        return repo_id, commit_sha

    def test_health(self) -> TestResult:
        name = "health"
        try:
            status, body = self.client.get("/health")
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name, f"status={body.get('status')} version={body.get('version')}")
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_rg_basic(self) -> TestResult:
        name = "rg_basic"
        try:
            status, health = self.client.get("/health")
            self._expect(name, status, 200, health)
            if not bool(health.get("rg_available", False)):
                return self._fail(name, "health.rg_available=false")

            self.ensure_workspace()
            status, body = self.client.post("/query/references", self._query_payload_base(self.args.symbol))
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            warnings = (
                body.get("confidence", {}).get("warnings", [])
                if isinstance(body, dict)
                else []
            )
            recall_warnings = [w for w in warnings if str(w).startswith("recall[")]
            if recall_warnings:
                return self._fail(name, f"recall warnings present: {recall_warnings}")
            return self._ok(name, "rg available and no recall errors")
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_workspace_register(self) -> TestResult:
        name = "workspace_register"
        try:
            self.ensure_workspace()
            return self._ok(name, "workspace registered")
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_workspace_get(self) -> TestResult:
        name = "workspace_get"
        try:
            self.ensure_workspace()
            status, body = self.client.get(self._workspace_path())
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_workspace_refresh(self) -> TestResult:
        name = "workspace_refresh"
        try:
            self.ensure_workspace()
            status, body = self.client.post(f"{self._workspace_path()}/refresh-manifest", {})
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_context_create_overlay(self) -> TestResult:
        name = "context_create_overlay"
        try:
            cid = self.ensure_overlay_context()
            if self.args.print_response:
                self._dump_response(name, {"context_id": cid})
            return self._ok(name, f"context_id={cid}")
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_context_expire(self) -> TestResult:
        name = "context_expire"
        try:
            cid = self.ensure_overlay_context()
            status, body = self.client.post(f"/context/{urllib.parse.quote(cid)}/expire", {})
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name, f"context_id={cid}")
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_query_references(self) -> TestResult:
        name = "query_references"
        try:
            self.ensure_workspace()
            status, body = self.client.post("/query/references", self._query_payload_base(self.args.symbol))
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_query_definition(self) -> TestResult:
        name = "query_definition"
        try:
            self.ensure_workspace()
            status, body = self.client.post("/query/definition", self._query_payload_base(self.args.symbol))
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_query_call_graph(self) -> TestResult:
        name = "query_call_graph"
        try:
            self.ensure_workspace()
            payload = self._query_payload_base(self.args.symbol)
            payload["direction"] = "both"
            status, body = self.client.post("/query/call-graph", payload)
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_query_file_symbols(self) -> TestResult:
        name = "query_file_symbols"
        try:
            self.ensure_workspace()
            status, body = self.client.post(
                "/query/file-symbols",
                {"workspace_id": self.args.workspace_id, "file_key": self.args.file_key},
            )
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_explore_list_candidates(self) -> TestResult:
        name = "explore_list_candidates"
        try:
            self.ensure_workspace()
            payload = self._query_payload_base(self.args.symbol)
            payload["max_files"] = self.args.max_recall_files or 50
            status, body = self.client.post("/explore/list-candidates", payload)
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_explore_classify_freshness(self) -> TestResult:
        name = "explore_classify_freshness"
        try:
            self.ensure_workspace()
            status, listed = self.client.post("/explore/list-candidates", self._query_payload_base(self.args.symbol))
            self._expect(name, status, 200, listed)
            candidate_keys = listed.get("candidates", [])[:20]
            status, body = self.client.post(
                "/explore/classify-freshness",
                {
                    "workspace_id": self.args.workspace_id,
                    "candidate_file_keys": candidate_keys,
                },
            )
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_explore_fetch_references(self) -> TestResult:
        name = "explore_fetch_references"
        try:
            self.ensure_workspace()
            status, listed = self.client.post("/explore/list-candidates", self._query_payload_base(self.args.symbol))
            self._expect(name, status, 200, listed)
            status, body = self.client.post(
                "/explore/fetch-references",
                {
                    "workspace_id": self.args.workspace_id,
                    "symbol": self.args.symbol,
                    "candidate_file_keys": listed.get("candidates", [])[:50],
                },
            )
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_explore_get_confidence(self) -> TestResult:
        name = "explore_get_confidence"
        try:
            status, body = self.client.post(
                "/explore/get-confidence",
                {"verified_files": [self.args.file_key]},
            )
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_cache_invalidate(self) -> TestResult:
        name = "cache_invalidate"
        try:
            self.ensure_workspace()
            status, body = self.client.post(
                "/cache/invalidate",
                {"workspace_id": self.args.workspace_id, "context_id": "", "file_keys": [self.args.file_key]},
            )
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_webhook_gitlab(self) -> TestResult:
        name = "webhook_gitlab"
        try:
            self.ensure_workspace()
            status, body = self.client.post(
                "/webhooks/gitlab",
                {
                    "event_type": "merge_request",
                    "payload": {
                        "workspace_id": self.args.workspace_id,
                        "repo_id": self.args.repo_id,
                        "event_sha": self.args.commit_sha,
                        "branch": self.args.branch,
                    },
                },
            )
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name, f"index_job_id={body.get('index_job_id', '')}")
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_sync_repo(self) -> TestResult:
        name = "sync_repo"
        if not self.args.repo_id or not self.args.commit_sha:
            return self._skip(name, "requires --repo-id and --commit-sha")
        try:
            job_id = self.ensure_sync_job()
            return self._ok(name, f"job_id={job_id}")
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_sync_job_get(self) -> TestResult:
        name = "sync_job_get"
        if not self.args.repo_id or not self.args.commit_sha:
            return self._skip(name, "requires --repo-id and --commit-sha")
        try:
            job_id = self.ensure_sync_job()
            assert job_id is not None
            status, body = self.client.get(f"/sync-jobs/{urllib.parse.quote(job_id)}")
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_sync_batch(self) -> TestResult:
        name = "sync_batch"
        if not self.args.repo_id or not self.args.commit_sha:
            return self._skip(name, "requires --repo-id and --commit-sha")
        try:
            self.ensure_workspace()
            status, body = self.client.post(
                f"{self._workspace_path()}/sync-batch",
                {"targets": [{"repo_id": self.args.repo_id, "commit_sha": self.args.commit_sha}]},
            )
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_sync_status(self) -> TestResult:
        name = "sync_status"
        if not self.args.repo_id:
            return self._skip(name, "requires --repo-id")
        try:
            self.ensure_workspace()
            status, body = self.client.get(
                f"{self._workspace_path()}/repos/{urllib.parse.quote(self.args.repo_id)}/sync-status"
            )
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_sync_all_repos(self) -> TestResult:
        name = "sync_all_repos"
        try:
            self.ensure_workspace()
            status, body = self.client.post(
                f"{self._workspace_path()}/sync-all-repos",
                {"force_clean": self.args.force_clean},
            )
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            jobs = body.get("jobs", [])
            return self._ok(name, f"jobs={len(jobs)} skipped={len(body.get('skipped_repos', []))}")
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_vector_upsert(self) -> TestResult:
        name = "vector_upsert"
        try:
            rec = self.ensure_vector_record()
            if rec is None:
                return self._skip(name, "vector feature unavailable (503)")
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_vector_search(self) -> TestResult:
        name = "vector_search"
        try:
            rec = self.ensure_vector_record()
            if rec is None:
                return self._skip(name, "vector feature unavailable (503)")
            status, body = self.client.post(
                "/commit-diff-summaries/search",
                {
                    "workspace_id": self.args.workspace_id,
                    "query_embedding": [0.0] * self.args.embedding_dim,
                    "top_k": 5,
                    "repo_ids": [self.args.repo_id] if self.args.repo_id else [],
                    "branches": [self.args.branch] if self.args.branch else [],
                    "score_threshold": 0.0,
                },
            )
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def test_vector_get(self) -> TestResult:
        name = "vector_get"
        try:
            rec = self.ensure_vector_record()
            if rec is None:
                return self._skip(name, "vector feature unavailable (503)")
            repo_id, commit_sha = rec
            status, body = self.client.get(
                f"/commit-diff-summaries/{urllib.parse.quote(self.args.workspace_id)}/"
                f"{urllib.parse.quote(repo_id)}/{urllib.parse.quote(commit_sha)}"
            )
            self._expect(name, status, 200, body)
            self._dump_response(name, body)
            return self._ok(name)
        except Exception as exc:
            return self._fail(name, str(exc))

    def available_test_names(self) -> list[str]:
        return [name for name, _ in self.tests]

    def run_selected(self, selected: list[str]) -> list[TestResult]:
        results: list[TestResult] = []
        funcs = dict(self.tests)
        for name in selected:
            fn = funcs.get(name)
            if fn is None:
                results.append(TestResult(name, "fail", "unknown test name"))
                continue
            results.append(fn())
        return results


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run granular CXXtract2 API smoke tests against live service")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--workspace-id", default="")
    parser.add_argument("--root-path", required=True)
    parser.add_argument("--manifest-path", required=True)
    parser.add_argument("--symbol", default="main")
    parser.add_argument("--file-key", default="repoA:src/main.cpp")
    parser.add_argument("--repo-id", default="")
    parser.add_argument("--commit-sha", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--force-clean", action="store_true", default=True)
    parser.add_argument("--timeout-s", type=float, default=60.0)
    parser.add_argument("--entry-repos", default="", help="Comma-separated entry repos for query scope.")
    parser.add_argument("--max-repo-hops", type=int, default=1)
    parser.add_argument("--max-recall-files", type=int, default=None)
    parser.add_argument("--max-parse-workers", type=int, default=None)
    parser.add_argument("--embedding-dim", type=int, default=1536)
    parser.add_argument("--embedding-model", default="text-embedding-3-large")
    parser.add_argument(
        "--tests",
        default="all",
        help="Comma-separated test names, or 'all'. Use --list-tests to inspect names.",
    )
    parser.add_argument("--print-response", action="store_true", default=False)
    parser.add_argument(
        "--allow-workspace-id-mismatch",
        action="store_true",
        default=False,
        help="Allow --workspace-id to differ from workspace_id in manifest.",
    )
    parser.add_argument("--list-tests", action="store_true", default=False)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    manifest_path = Path(args.manifest_path).resolve()
    if not manifest_path.exists():
        print(f"[FAIL] manifest file not found: {manifest_path}")
        return 1

    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[FAIL] failed to read manifest: {exc}")
        return 1
    if not isinstance(raw, dict):
        print(f"[FAIL] invalid manifest structure: {manifest_path}")
        return 1

    manifest_workspace_id = str(raw.get("workspace_id", "")).strip()
    if not manifest_workspace_id:
        print(f"[FAIL] workspace_id missing in manifest: {manifest_path}")
        return 1

    if not args.workspace_id:
        args.workspace_id = manifest_workspace_id
    elif args.workspace_id != manifest_workspace_id and not args.allow_workspace_id_mismatch:
        print(
            "[FAIL] workspace_id mismatch: "
            f"--workspace-id={args.workspace_id} but manifest has {manifest_workspace_id}. "
            "Pass --allow-workspace-id-mismatch to override."
        )
        return 1

    args.manifest_path = str(manifest_path)
    args.root_path = str(Path(args.root_path).resolve())

    runner = SmokeRunner(args)

    if args.list_tests:
        for name in runner.available_test_names():
            print(name)
        return 0

    selected = runner.available_test_names() if args.tests.strip().lower() == "all" else [
        s.strip() for s in args.tests.split(",") if s.strip()
    ]

    results = runner.run_selected(selected)

    passed = 0
    skipped = 0
    failed = 0
    for r in results:
        if r.status == "pass":
            passed += 1
            print(f"[PASS] {r.name} {('- ' + r.message) if r.message else ''}")
        elif r.status == "skip":
            skipped += 1
            print(f"[SKIP] {r.name} - {r.message}")
        else:
            failed += 1
            print(f"[FAIL] {r.name} - {r.message}")

    print(f"\nSummary: pass={passed} skip={skipped} fail={failed} total={len(results)}")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
