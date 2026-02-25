"""FastAPI router — all HTTP endpoints consumed by the AI Agent."""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from cxxtract import __version__
from cxxtract.cache import repository as repo
from cxxtract.models import (
    CacheInvalidateRequest,
    CacheInvalidateResponse,
    CallGraphRequest,
    CallGraphResponse,
    DefinitionResponse,
    FileSymbolsRequest,
    FileSymbolsResponse,
    HealthResponse,
    ReferencesResponse,
    SymbolQueryRequest,
)
from cxxtract.orchestrator.engine import OrchestratorEngine

logger = logging.getLogger(__name__)

router = APIRouter()


# ------------------------------------------------------------------
# Dependency: retrieve the OrchestratorEngine from app state
# ------------------------------------------------------------------

def _get_engine(request: Request) -> OrchestratorEngine:
    return request.app.state.engine  # type: ignore[return-value]


EngineDepends = Annotated[OrchestratorEngine, Depends(_get_engine)]


# ------------------------------------------------------------------
# Query endpoints
# ------------------------------------------------------------------

@router.post(
    "/query/references",
    response_model=ReferencesResponse,
    summary="Find all references to a symbol",
    tags=["query"],
)
async def query_references(
    body: SymbolQueryRequest,
    engine: EngineDepends,
) -> ReferencesResponse:
    """Run the full recall-then-proof pipeline to find all references
    to the given C++ symbol across the repository."""
    return await engine.query_references(body)


@router.post(
    "/query/definition",
    response_model=DefinitionResponse,
    summary="Find the definition(s) of a symbol",
    tags=["query"],
)
async def query_definition(
    body: SymbolQueryRequest,
    engine: EngineDepends,
) -> DefinitionResponse:
    """Locate one or more definition sites for the given symbol."""
    return await engine.query_definition(body)


@router.post(
    "/query/call-graph",
    response_model=CallGraphResponse,
    summary="Get call graph edges for a function",
    tags=["query"],
)
async def query_call_graph(
    body: CallGraphRequest,
    engine: EngineDepends,
) -> CallGraphResponse:
    """Return outgoing and/or incoming call edges for a function."""
    return await engine.query_call_graph(body)


@router.post(
    "/query/file-symbols",
    response_model=FileSymbolsResponse,
    summary="List all symbols defined in a file",
    tags=["query"],
)
async def query_file_symbols(
    body: FileSymbolsRequest,
    engine: EngineDepends,
) -> FileSymbolsResponse:
    """Parse (if needed) and list every symbol defined in the file."""
    return await engine.query_file_symbols(body)


# ------------------------------------------------------------------
# Cache management
# ------------------------------------------------------------------

@router.post(
    "/cache/invalidate",
    response_model=CacheInvalidateResponse,
    summary="Invalidate cached facts",
    tags=["cache"],
)
async def cache_invalidate(
    body: CacheInvalidateRequest,
    engine: EngineDepends,
) -> CacheInvalidateResponse:
    """Force invalidation for specific files or the entire cache."""
    return await engine.invalidate_cache(body)


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------

@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health check",
    tags=["health"],
)
async def health(request: Request) -> HealthResponse:
    """Return service health, cache stats, and tool availability."""
    settings = request.app.state.settings

    # Use the rg_version cached at startup rather than re-probing
    rg_version: str = getattr(request.app.state, "rg_version", "")
    rg_available = bool(rg_version) or shutil.which(settings.rg_binary) is not None

    # Check extractor availability
    extractor_available = shutil.which(settings.extractor_binary) is not None

    # Cache stats
    try:
        file_count = await repo.count_tracked_files()
        symbol_count = await repo.count_symbols()
    except Exception:
        file_count = 0
        symbol_count = 0

    return HealthResponse(
        status="ok",
        version=__version__,
        cache_file_count=file_count,
        cache_symbol_count=symbol_count,
        rg_available=rg_available,
        rg_version=rg_version,
        extractor_available=extractor_available,
    )
