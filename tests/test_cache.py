"""Tests for v3 repository layer (context + canonical file_key semantics)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import pytest

from cxxtract.cache import repository as repo
from cxxtract.cache.db import close_db, get_connection, init_db
from cxxtract.cache.hasher import (
    compute_composite_hash,
    compute_content_hash,
    compute_flags_hash,
    compute_includes_hash,
)
from cxxtract.models import (
    ExtractedCallEdge,
    ExtractedReference,
    ExtractedSymbol,
    ExtractorOutput,
    ParsePayload,
)


class TestInitDb:

    async def test_init_memory(self, db_conn: aiosqlite.Connection):
        assert db_conn is not None
        cursor = await db_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in await cursor.fetchall()}
        assert "tracked_files" in tables
        assert "analysis_contexts" in tables

    async def test_get_connection_before_init_raises(self):
        import cxxtract.cache.db as db_mod

        saved = db_mod._connection
        db_mod._connection = None
        try:
            try:
                get_connection()
                assert False, "expected RuntimeError"
            except RuntimeError:
                pass
        finally:
            db_mod._connection = saved

    async def test_close_db_idempotent(self):
        import cxxtract.cache.db as db_mod

        saved = db_mod._connection
        db_mod._connection = None
        await close_db()
        db_mod._connection = saved

    async def test_init_db_fails_when_vector_enabled_without_extension(self):
        with patch("cxxtract.cache.db.default_sqlite_vec_path", return_value=Path("Z:/__missing__/sqlite_vec.dll")):
            with pytest.raises(RuntimeError):
                await init_db(
                    ":memory:",
                    enable_vector_features=True,
                    commit_embedding_dim=1536,
                )


class TestHashers:

    def test_content_hash(self, tmp_path: Path):
        f = tmp_path / "a.cpp"
        f.write_bytes(b"int main() {}")
        assert compute_content_hash(str(f)) == hashlib.sha256(b"int main() {}").hexdigest()

    def test_flags_hash_order_independent(self):
        assert compute_flags_hash(["-O2", "-Wall"]) == compute_flags_hash(["-Wall", "-O2"])

    def test_includes_hash_order_independent(self):
        assert compute_includes_hash(["a", "b"]) == compute_includes_hash(["b", "a"])


def _make_output(file_path: str, symbol: str = "foo") -> ExtractorOutput:
    return ExtractorOutput(
        file=file_path,
        symbols=[
            ExtractedSymbol(
                name=symbol,
                qualified_name=f"ns::{symbol}",
                kind="Function",
                line=1,
                col=1,
                extent_end_line=1,
            )
        ],
        references=[ExtractedReference(symbol=f"ns::{symbol}", line=1, col=4, kind="call")],
        call_edges=[ExtractedCallEdge(caller="ns::caller", callee=f"ns::{symbol}", line=1)],
        include_deps=[],
    )


async def _make_payload(context_id: str, file_key: str, repo_id: str, rel_path: str, abs_path: str) -> ParsePayload:
    output = _make_output(abs_path)
    content_hash = compute_content_hash(abs_path)
    flags_hash = compute_flags_hash(["-std=c++17"])
    includes_hash = compute_includes_hash([])
    composite_hash = compute_composite_hash(content_hash, includes_hash, flags_hash)

    return ParsePayload(
        context_id=context_id,
        file_key=file_key,
        repo_id=repo_id,
        rel_path=rel_path,
        abs_path=abs_path,
        output=output,
        resolved_include_deps=[],
        content_hash=content_hash,
        flags_hash=flags_hash,
        includes_hash=includes_hash,
        composite_hash=composite_hash,
        warnings=[],
    )


async def _bootstrap_workspace(tmp_path: Path, workspace_id: str = "ws_main") -> str:
    manifest = tmp_path / "workspace.yaml"
    manifest.write_text("workspace_id: ws_main\nrepos: []\npath_remaps: []\n")
    await repo.upsert_workspace(workspace_id, str(tmp_path), str(manifest))
    return await repo.ensure_baseline_context(workspace_id)


class TestRepositoryCore:

    async def test_upsert_and_get_tracked_file(self, db_conn: aiosqlite.Connection, tmp_path: Path):
        context_id = await _bootstrap_workspace(tmp_path)
        src = tmp_path / "repos" / "repoA" / "src" / "a.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int foo() { return 1; }")

        file_key = "repoA:src/a.cpp"
        payload = await _make_payload(context_id, file_key, "repoA", "src/a.cpp", str(src).replace("\\", "/"))
        await repo.upsert_parse_payload(payload)

        tracked = await repo.get_tracked_file(context_id, file_key)
        assert tracked is not None
        assert tracked["repo_id"] == "repoA"
        assert await repo.get_composite_hash(context_id, file_key) == payload.composite_hash

    async def test_symbol_reference_and_call_queries(self, db_conn: aiosqlite.Connection, tmp_path: Path):
        context_id = await _bootstrap_workspace(tmp_path)
        src = tmp_path / "repos" / "repoA" / "src" / "a.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int foo() { return 1; }")

        file_key = "repoA:src/a.cpp"
        payload = await _make_payload(context_id, file_key, "repoA", "src/a.cpp", str(src).replace("\\", "/"))
        await repo.upsert_parse_payload(payload)

        defs = await repo.search_symbols_by_name("foo", context_chain=[context_id], candidate_file_keys={file_key})
        refs = await repo.search_references_by_symbol("foo", context_chain=[context_id], candidate_file_keys={file_key})
        edges = await repo.get_call_edges_for_callee("ns::foo", context_chain=[context_id], candidate_file_keys={file_key})

        assert len(defs) == 1
        assert defs[0]["qualified_name"] == "ns::foo"
        assert len(refs) == 1
        assert len(edges) == 1

    async def test_fts_query_handles_cpp_qualified_symbol(self, db_conn: aiosqlite.Connection, tmp_path: Path):
        context_id = await _bootstrap_workspace(tmp_path)
        file_key = "webrtc:pc/ice_transport.h"
        await repo.upsert_recall_content(
            context_id=context_id,
            file_key=file_key,
            repo_id="webrtc",
            content="class IceTransportInterface {}; namespace webrtc {}",
        )

        hits = await repo.search_recall_candidates(
            context_id,
            "webrtc::IceTransportInterface",
            repo_ids=["webrtc"],
            max_files=20,
        )
        assert file_key in hits

    async def test_overlay_first_context_chain(self, db_conn: aiosqlite.Connection, tmp_path: Path):
        baseline = await _bootstrap_workspace(tmp_path)
        await repo.upsert_analysis_context("ws_main:pr:1", "ws_main", "pr", base_context_id=baseline)

        src = tmp_path / "repos" / "repoA" / "src" / "a.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int foo() { return 1; }")

        file_key = "repoA:src/a.cpp"

        base_payload = await _make_payload(baseline, file_key, "repoA", "src/a.cpp", str(src).replace("\\", "/"))
        await repo.upsert_parse_payload(base_payload)

        overlay_output = _make_output(str(src), symbol="foo_pr")
        content_hash = compute_content_hash(str(src))
        flags_hash = compute_flags_hash(["-std=c++17"])
        includes_hash = compute_includes_hash([])
        overlay_payload = ParsePayload(
            context_id="ws_main:pr:1",
            file_key=file_key,
            repo_id="repoA",
            rel_path="src/a.cpp",
            abs_path=str(src).replace("\\", "/"),
            output=overlay_output,
            resolved_include_deps=[],
            content_hash=content_hash,
            flags_hash=flags_hash,
            includes_hash=includes_hash,
            composite_hash=compute_composite_hash(content_hash, includes_hash, flags_hash),
            warnings=[],
        )
        await repo.upsert_parse_payload(overlay_payload)

        defs = await repo.search_symbols_by_name("foo", context_chain=["ws_main:pr:1", baseline], candidate_file_keys={file_key})
        assert any(d["qualified_name"] == "ns::foo_pr" for d in defs)

    async def test_parse_runs(self, db_conn: aiosqlite.Connection, tmp_path: Path):
        context_id = await _bootstrap_workspace(tmp_path)
        file_key = "repoA:src/a.cpp"
        run_id = await repo.insert_parse_run(context_id, file_key, "C:/abs/a.cpp")
        assert run_id > 0

        await repo.finish_parse_run(run_id, success=False, error_msg="timeout")
        runs = await repo.get_parse_runs(context_id, file_key)
        assert len(runs) == 1
        assert runs[0]["success"] == 0

    async def test_clear_context(self, db_conn: aiosqlite.Connection, tmp_path: Path):
        context_id = await _bootstrap_workspace(tmp_path)
        src = tmp_path / "repos" / "repoA" / "src" / "a.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int foo() { return 1; }")

        file_key = "repoA:src/a.cpp"
        payload = await _make_payload(context_id, file_key, "repoA", "src/a.cpp", str(src).replace("\\", "/"))
        await repo.upsert_parse_payload(payload)

        deleted = await repo.clear_context(context_id)
        assert deleted == 1
        assert await repo.count_tracked_files(context_id) == 0


class TestMetrics:

    async def test_metrics_helpers(self, db_conn: aiosqlite.Connection, tmp_path: Path):
        context_id = await _bootstrap_workspace(tmp_path)
        assert await repo.count_active_contexts() >= 1
        assert await repo.get_overlay_disk_usage_bytes() > 0

        await repo.insert_index_job(
            job_id="job-1",
            workspace_id="ws_main",
            repo_id="repoA",
            context_id=context_id,
            event_type="merge_request",
        )
        assert await repo.get_index_queue_depth() >= 1
        assert await repo.get_oldest_pending_job_age_s() >= -1.0

    async def test_repo_sync_metrics(self, db_conn: aiosqlite.Connection, tmp_path: Path):
        context_id = await _bootstrap_workspace(tmp_path)
        await repo.insert_repo_sync_job(
            job_id="sync-job-1",
            workspace_id="ws_main",
            repo_id="repoA",
            requested_commit_sha="a" * 40,
        )
        assert await repo.get_repo_sync_queue_depth() >= 1
        leased = await repo.lease_next_repo_sync_job()
        assert leased is not None
        assert leased["status"] == "running"
        assert await repo.get_active_sync_jobs() >= 1


class TestRepoSyncState:

    async def test_repo_sync_job_lifecycle(self, db_conn: aiosqlite.Connection, tmp_path: Path):
        await _bootstrap_workspace(tmp_path)
        await repo.insert_repo_sync_job(
            job_id="job-1",
            workspace_id="ws_main",
            repo_id="repoA",
            requested_commit_sha="a" * 40,
            requested_branch="main",
            max_attempts=2,
        )

        leased = await repo.lease_next_repo_sync_job()
        assert leased is not None
        assert leased["id"] == "job-1"
        assert leased["status"] == "running"
        assert leased["attempts"] == 1

        await repo.mark_repo_sync_job_failed(
            job_id="job-1",
            error_code="missing_token_env",
            error_message="token missing",
            dead_letter=False,
        )
        failed = await repo.get_repo_sync_job("job-1")
        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["error_code"] == "missing_token_env"

        leased2 = await repo.lease_next_repo_sync_job()
        assert leased2 is not None
        assert leased2["attempts"] == 2

        await repo.mark_repo_sync_job_done(job_id="job-1", resolved_commit_sha="b" * 40)
        done = await repo.get_repo_sync_job("job-1")
        assert done is not None
        assert done["status"] == "done"
        assert done["resolved_commit_sha"] == "b" * 40

    async def test_repo_sync_state_upsert(self, db_conn: aiosqlite.Connection, tmp_path: Path):
        await _bootstrap_workspace(tmp_path)
        await repo.upsert_repo_sync_state(
            workspace_id="ws_main",
            repo_id="repoA",
            success=False,
            error_code="commit_not_found",
            error_message="missing sha",
        )
        state = await repo.get_repo_sync_state("ws_main", "repoA")
        assert state is not None
        assert state["last_error_code"] == "commit_not_found"

        await repo.upsert_repo_sync_state(
            workspace_id="ws_main",
            repo_id="repoA",
            success=True,
            last_synced_commit_sha="c" * 40,
            last_synced_branch="main",
        )
        state2 = await repo.get_repo_sync_state("ws_main", "repoA")
        assert state2 is not None
        assert state2["last_synced_commit_sha"] == "c" * 40
