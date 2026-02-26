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
    ConfidenceEnvelope,
    DefinitionResponse,
    FileSymbolsResponse,
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
