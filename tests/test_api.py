"""Tests for the FastAPI HTTP endpoints (v3 contracts only)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from cxxtract.config import Settings
from cxxtract.main import create_app
from cxxtract.models import (
    CacheInvalidateResponse,
    CallGraphResponse,
    CommitDiffSummaryGetResponse,
    CommitDiffSummaryRecord,
    CommitDiffSummarySearchResponse,
    ConfidenceEnvelope,
    DefinitionResponse,
    FileSymbolsResponse,
    RepoSyncBatchResponse,
    RepoSyncAllResponse,
    RepoSyncJobResponse,
    RepoSyncJobStatus,
    RepoSyncStatusResponse,
    ReferencesResponse,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "test.db"),
        rg_binary="rg",
        extractor_binary="fake-extractor",
        max_parse_workers=2,
        max_recall_files=50,
        recall_timeout_s=10,
        parse_timeout_s=30,
    )


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.query_references = AsyncMock(return_value=ReferencesResponse(
        symbol="test",
        definition=None,
        references=[],
        confidence=ConfidenceEnvelope(),
    ))
    engine.query_definition = AsyncMock(return_value=DefinitionResponse(
        symbol="test",
        definitions=[],
        confidence=ConfidenceEnvelope(),
    ))
    engine.query_call_graph = AsyncMock(return_value=CallGraphResponse(
        symbol="test",
        edges=[],
        confidence=ConfidenceEnvelope(),
    ))
    engine.query_file_symbols = AsyncMock(return_value=FileSymbolsResponse(
        file_key="repoA:src/test.cpp",
        symbols=[],
        confidence=ConfidenceEnvelope(),
    ))
    engine.invalidate_cache = AsyncMock(return_value=CacheInvalidateResponse(
        invalidated_files=0,
        message="ok",
    ))
    engine.sync_repo = AsyncMock(return_value=RepoSyncJobResponse(
        job_id="job-1",
        workspace_id="ws_main",
        repo_id="repoA",
        requested_commit_sha="a" * 40,
        requested_branch="main",
        status=RepoSyncJobStatus.PENDING,
    ))
    engine.sync_batch = AsyncMock(return_value=RepoSyncBatchResponse(
        jobs=[
            RepoSyncJobResponse(
                job_id="job-1",
                workspace_id="ws_main",
                repo_id="repoA",
                requested_commit_sha="a" * 40,
                requested_branch="main",
                status=RepoSyncJobStatus.PENDING,
            )
        ]
    ))
    engine.sync_all_repos = AsyncMock(return_value=RepoSyncAllResponse(
        workspace_id="ws_main",
        jobs=[
            RepoSyncJobResponse(
                job_id="job-all-1",
                workspace_id="ws_main",
                repo_id="repoA",
                requested_commit_sha="a" * 40,
                requested_branch="main",
                status=RepoSyncJobStatus.PENDING,
            )
        ],
        skipped_repos=["repoC"],
    ))
    engine.get_sync_job = AsyncMock(return_value=RepoSyncJobResponse(
        job_id="job-1",
        workspace_id="ws_main",
        repo_id="repoA",
        requested_commit_sha="a" * 40,
        requested_branch="main",
        status=RepoSyncJobStatus.DONE,
    ))
    engine.get_repo_sync_status = AsyncMock(return_value=RepoSyncStatusResponse(
        workspace_id="ws_main",
        repo_id="repoA",
        last_synced_commit_sha="a" * 40,
        last_synced_branch="main",
    ))
    engine.upsert_commit_diff_summary = AsyncMock(return_value=CommitDiffSummaryRecord(
        id="sum-1",
        workspace_id="ws_main",
        repo_id="repoA",
        commit_sha="a" * 40,
        branch="main",
        summary_text="test summary",
        embedding_model="text-embedding-3-large",
        embedding_dim=1536,
        metadata={},
        created_at="",
        updated_at="",
    ))
    engine.search_commit_diff_summaries = AsyncMock(return_value=CommitDiffSummarySearchResponse(hits=[]))
    engine.get_commit_diff_summary = AsyncMock(return_value=CommitDiffSummaryGetResponse(found=False, record=None))
    return engine


@pytest.fixture
async def client(settings: Settings, mock_engine):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    app = create_app(settings)
    app.router.lifespan_context = _noop_lifespan
    app.state.settings = settings
    app.state.engine = mock_engine
    app.state.rg_version = "ripgrep 14.0.0"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestHealthEndpoint:

    async def test_health_returns_200(self, client: AsyncClient):
        with patch("cxxtract.api.routes.repo.count_tracked_files", AsyncMock(return_value=5)), patch(
            "cxxtract.api.routes.repo.count_symbols", AsyncMock(return_value=42)
        ):
            resp = await client.get("/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["cache_file_count"] == 5
        assert data["cache_symbol_count"] == 42


class TestQueryContracts:

    async def test_references_valid(self, client: AsyncClient, mock_engine):
        resp = await client.post(
            "/query/references",
            json={"symbol": "Session::Auth", "workspace_id": "ws_main"},
        )
        assert resp.status_code == 200
        mock_engine.query_references.assert_awaited_once()

    async def test_definition_valid(self, client: AsyncClient, mock_engine):
        resp = await client.post(
            "/query/definition",
            json={"symbol": "MyClass", "workspace_id": "ws_main"},
        )
        assert resp.status_code == 200
        mock_engine.query_definition.assert_awaited_once()

    async def test_call_graph_valid(self, client: AsyncClient, mock_engine):
        resp = await client.post(
            "/query/call-graph",
            json={"symbol": "ns::foo", "workspace_id": "ws_main", "direction": "outgoing"},
        )
        assert resp.status_code == 200
        mock_engine.query_call_graph.assert_awaited_once()

    async def test_file_symbols_valid(self, client: AsyncClient, mock_engine):
        resp = await client.post(
            "/query/file-symbols",
            json={"workspace_id": "ws_main", "file_key": "repoA:src/main.cpp"},
        )
        assert resp.status_code == 200
        mock_engine.query_file_symbols.assert_awaited_once()

    async def test_cache_invalidate_valid(self, client: AsyncClient, mock_engine):
        resp = await client.post(
            "/cache/invalidate",
            json={"workspace_id": "ws_main", "file_keys": ["repoA:src/a.cpp"]},
        )
        assert resp.status_code == 200
        mock_engine.invalidate_cache.assert_awaited_once()


class TestLegacyFieldsRejected:

    async def test_references_rejects_repo_root(self, client: AsyncClient):
        resp = await client.post(
            "/query/references",
            json={"symbol": "foo", "workspace_id": "ws_main", "repo_root": "C:/repo"},
        )
        assert resp.status_code == 422

    async def test_file_symbols_rejects_file_path(self, client: AsyncClient):
        resp = await client.post(
            "/query/file-symbols",
            json={"workspace_id": "ws_main", "file_path": "src/a.cpp"},
        )
        assert resp.status_code == 422

    async def test_cache_invalidate_rejects_file_paths(self, client: AsyncClient):
        resp = await client.post(
            "/cache/invalidate",
            json={"workspace_id": "ws_main", "file_paths": ["a.cpp"]},
        )
        assert resp.status_code == 422

    async def test_query_requires_workspace_id(self, client: AsyncClient):
        resp = await client.post("/query/references", json={"symbol": "foo"})
        assert resp.status_code == 422


class TestSyncAndVectorEndpoints:

    async def test_sync_repo_valid(self, client: AsyncClient, mock_engine):
        resp = await client.post(
            "/workspace/ws_main/sync-repo",
            json={"repo_id": "repoA", "commit_sha": "a" * 40, "branch": "main"},
        )
        assert resp.status_code == 200
        mock_engine.sync_repo.assert_awaited_once()

    async def test_sync_batch_valid(self, client: AsyncClient, mock_engine):
        resp = await client.post(
            "/workspace/ws_main/sync-batch",
            json={"targets": [{"repo_id": "repoA", "commit_sha": "a" * 40}]},
        )
        assert resp.status_code == 200
        mock_engine.sync_batch.assert_awaited_once()

    async def test_sync_all_repos_valid(self, client: AsyncClient, mock_engine):
        resp = await client.post(
            "/workspace/ws_main/sync-all-repos",
            json={"force_clean": True},
        )
        assert resp.status_code == 200
        mock_engine.sync_all_repos.assert_awaited_once()

    async def test_sync_repo_rejects_short_sha(self, client: AsyncClient):
        resp = await client.post(
            "/workspace/ws_main/sync-repo",
            json={"repo_id": "repoA", "commit_sha": "abc123"},
        )
        assert resp.status_code == 422

    async def test_get_sync_job(self, client: AsyncClient, mock_engine):
        resp = await client.get("/sync-jobs/job-1")
        assert resp.status_code == 200
        mock_engine.get_sync_job.assert_awaited_once()

    async def test_get_sync_status(self, client: AsyncClient, mock_engine):
        resp = await client.get("/workspace/ws_main/repos/repoA/sync-status")
        assert resp.status_code == 200
        mock_engine.get_repo_sync_status.assert_awaited_once()

    async def test_commit_summary_upsert(self, client: AsyncClient, mock_engine):
        resp = await client.post(
            "/commit-diff-summaries/upsert",
            json={
                "workspace_id": "ws_main",
                "repo_id": "repoA",
                "commit_sha": "a" * 40,
                "branch": "main",
                "summary_text": "summary",
                "embedding_model": "text-embedding-3-large",
                "embedding": [0.0] * 1536,
                "metadata": {},
            },
        )
        assert resp.status_code == 200
        mock_engine.upsert_commit_diff_summary.assert_awaited_once()

    async def test_commit_summary_search(self, client: AsyncClient, mock_engine):
        resp = await client.post(
            "/commit-diff-summaries/search",
            json={
                "query_embedding": [0.0] * 1536,
                "top_k": 5,
                "workspace_id": "ws_main",
            },
        )
        assert resp.status_code == 200
        mock_engine.search_commit_diff_summaries.assert_awaited_once()

    async def test_commit_summary_get(self, client: AsyncClient, mock_engine):
        resp = await client.get(f"/commit-diff-summaries/ws_main/repoA/{'a'*40}")
        assert resp.status_code == 200
        mock_engine.get_commit_diff_summary.assert_awaited_once()
