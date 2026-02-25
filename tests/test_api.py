"""Tests for the FastAPI HTTP endpoints (routes.py).

Uses httpx.AsyncClient with the ASGI transport to test routing,
request validation, serialization, and status codes.  The
OrchestratorEngine is mocked so these tests are independent of
the pipeline internals.

Covers:
  - GET /health
  - POST /query/references
  - POST /query/definition
  - POST /query/call-graph
  - POST /query/file-symbols
  - POST /cache/invalidate
  - Error cases: 422 on invalid body
"""

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
    SymbolLocation,
)


# ====================================================================
# Fixtures
# ====================================================================

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
    """Create a mock OrchestratorEngine with all query methods stubbed."""
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
        file="test.cpp",
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
    """Create an httpx AsyncClient wired to the FastAPI app with mocked engine."""
    # Bypass lifespan (which tries to init DB, find rg, etc.)
    # by patching the lifespan to a no-op
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    app = create_app(settings)
    app.router.lifespan_context = _noop_lifespan

    # Set up app state manually
    app.state.settings = settings
    app.state.engine = mock_engine
    app.state.rg_version = "ripgrep 14.0.0"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ====================================================================
# GET /health
# ====================================================================

class TestHealthEndpoint:

    async def test_health_returns_200(self, client: AsyncClient):
        # Mock the repository calls made by the health endpoint
        with patch(
            "cxxtract.api.routes.repo.count_tracked_files",
            AsyncMock(return_value=5),
        ), patch(
            "cxxtract.api.routes.repo.count_symbols",
            AsyncMock(return_value=42),
        ):
            resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"]  # non-empty
        assert data["cache_file_count"] == 5
        assert data["cache_symbol_count"] == 42
        assert isinstance(data["rg_available"], bool)

    async def test_health_cache_stats_error(self, client: AsyncClient):
        """If cache stats fail, should still return 200 with zero counts."""
        with patch(
            "cxxtract.api.routes.repo.count_tracked_files",
            AsyncMock(side_effect=RuntimeError("DB not initialised")),
        ), patch(
            "cxxtract.api.routes.repo.count_symbols",
            AsyncMock(side_effect=RuntimeError("DB not initialised")),
        ):
            resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cache_file_count"] == 0
        assert data["cache_symbol_count"] == 0


# ====================================================================
# POST /query/references
# ====================================================================

class TestQueryReferences:

    async def test_valid_request(self, client: AsyncClient, mock_engine):
        resp = await client.post("/query/references", json={
            "symbol": "Session::Auth",
            "repo_root": "F:/projects/test",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "test"
        assert "confidence" in data
        mock_engine.query_references.assert_awaited_once()

    async def test_missing_required_field(self, client: AsyncClient):
        resp = await client.post("/query/references", json={
            "symbol": "foo",
            # missing repo_root
        })
        assert resp.status_code == 422


# ====================================================================
# POST /query/definition
# ====================================================================

class TestQueryDefinition:

    async def test_valid_request(self, client: AsyncClient, mock_engine):
        resp = await client.post("/query/definition", json={
            "symbol": "MyClass",
            "repo_root": "/tmp/project",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "test"
        assert "definitions" in data
        mock_engine.query_definition.assert_awaited_once()


# ====================================================================
# POST /query/call-graph
# ====================================================================

class TestQueryCallGraph:

    async def test_valid_request(self, client: AsyncClient, mock_engine):
        resp = await client.post("/query/call-graph", json={
            "symbol": "ns::foo",
            "repo_root": "/tmp/project",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "edges" in data
        mock_engine.query_call_graph.assert_awaited_once()

    async def test_with_direction(self, client: AsyncClient, mock_engine):
        resp = await client.post("/query/call-graph", json={
            "symbol": "ns::foo",
            "repo_root": "/tmp/project",
            "direction": "outgoing",
        })
        assert resp.status_code == 200

    async def test_invalid_direction(self, client: AsyncClient):
        resp = await client.post("/query/call-graph", json={
            "symbol": "ns::foo",
            "repo_root": "/tmp/project",
            "direction": "invalid_dir",
        })
        assert resp.status_code == 422


# ====================================================================
# POST /query/file-symbols
# ====================================================================

class TestQueryFileSymbols:

    async def test_valid_request(self, client: AsyncClient, mock_engine):
        resp = await client.post("/query/file-symbols", json={
            "file_path": "F:/projects/test/main.cpp",
            "repo_root": "F:/projects/test",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "symbols" in data
        mock_engine.query_file_symbols.assert_awaited_once()


# ====================================================================
# POST /cache/invalidate
# ====================================================================

class TestCacheInvalidate:

    async def test_invalidate_all(self, client: AsyncClient, mock_engine):
        resp = await client.post("/cache/invalidate", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert "invalidated_files" in data
        mock_engine.invalidate_cache.assert_awaited_once()

    async def test_invalidate_specific(self, client: AsyncClient, mock_engine):
        resp = await client.post("/cache/invalidate", json={
            "file_paths": ["a.cpp", "b.cpp"],
        })
        assert resp.status_code == 200
        mock_engine.invalidate_cache.assert_awaited_once()
