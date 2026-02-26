"""FastAPI router for query, workspace, context, and health endpoints."""

from __future__ import annotations

import logging
import shutil
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from cxxtract import __version__
from cxxtract.cache import repository as repo
from cxxtract.cache.db import is_sqlite_vec_loaded
from cxxtract.models import (
    CacheInvalidateRequest,
    CacheInvalidateResponse,
    CallGraphRequest,
    CallGraphResponse,
    CommitDiffSummaryGetResponse,
    CommitDiffSummaryRecord,
    CommitDiffSummarySearchRequest,
    CommitDiffSummarySearchResponse,
    CommitDiffSummaryUpsertRequest,
    ContextCreateOverlayRequest,
    ContextCreateOverlayResponse,
    ContextExpireResponse,
    DefinitionResponse,
    FileSymbolsRequest,
    FileSymbolsResponse,
    HealthResponse,
    RepoSyncBatchRequest,
    RepoSyncBatchResponse,
    RepoSyncAllRequest,
    RepoSyncAllResponse,
    RepoSyncJobResponse,
    RepoSyncRequest,
    RepoSyncStatusResponse,
    ReferencesResponse,
    SymbolQueryRequest,
    WebhookGitLabRequest,
    WebhookGitLabResponse,
    WorkspaceInfoResponse,
    WorkspaceRefreshResponse,
    WorkspaceRegisterRequest,
)
from cxxtract.orchestrator.engine import OrchestratorEngine

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_engine(request: Request) -> OrchestratorEngine:
    return request.app.state.engine  # type: ignore[return-value]


EngineDepends = Annotated[OrchestratorEngine, Depends(_get_engine)]


@router.post("/query/references", response_model=ReferencesResponse, tags=["query"])
async def query_references(body: SymbolQueryRequest, engine: EngineDepends) -> ReferencesResponse:
    return await engine.query_references(body)


@router.post("/query/definition", response_model=DefinitionResponse, tags=["query"])
async def query_definition(body: SymbolQueryRequest, engine: EngineDepends) -> DefinitionResponse:
    return await engine.query_definition(body)


@router.post("/query/call-graph", response_model=CallGraphResponse, tags=["query"])
async def query_call_graph(body: CallGraphRequest, engine: EngineDepends) -> CallGraphResponse:
    return await engine.query_call_graph(body)


@router.post("/query/file-symbols", response_model=FileSymbolsResponse, tags=["query"])
async def query_file_symbols(body: FileSymbolsRequest, engine: EngineDepends) -> FileSymbolsResponse:
    return await engine.query_file_symbols(body)


@router.post("/cache/invalidate", response_model=CacheInvalidateResponse, tags=["cache"])
async def cache_invalidate(body: CacheInvalidateRequest, engine: EngineDepends) -> CacheInvalidateResponse:
    return await engine.invalidate_cache(body)


@router.post("/workspace/register", response_model=WorkspaceInfoResponse, tags=["workspace"])
async def workspace_register(body: WorkspaceRegisterRequest, engine: EngineDepends) -> WorkspaceInfoResponse:
    return await engine.register_workspace(body)


@router.get("/workspace/{workspace_id}", response_model=WorkspaceInfoResponse, tags=["workspace"])
async def workspace_get(workspace_id: str, engine: EngineDepends) -> WorkspaceInfoResponse:
    return await engine.get_workspace_info(workspace_id)


@router.post(
    "/workspace/{workspace_id}/refresh-manifest",
    response_model=WorkspaceRefreshResponse,
    tags=["workspace"],
)
async def workspace_refresh(workspace_id: str, engine: EngineDepends) -> WorkspaceRefreshResponse:
    return await engine.refresh_workspace_manifest(workspace_id)


@router.post("/context/create-pr-overlay", response_model=ContextCreateOverlayResponse, tags=["context"])
async def context_create_overlay(
    body: ContextCreateOverlayRequest,
    engine: EngineDepends,
) -> ContextCreateOverlayResponse:
    return await engine.create_pr_overlay_context(body)


@router.post("/context/{context_id}/expire", response_model=ContextExpireResponse, tags=["context"])
async def context_expire(context_id: str, engine: EngineDepends) -> ContextExpireResponse:
    return await engine.expire_context(context_id)


@router.post("/webhooks/gitlab", response_model=WebhookGitLabResponse, tags=["webhook"])
async def webhook_gitlab(body: WebhookGitLabRequest, engine: EngineDepends) -> WebhookGitLabResponse:
    return await engine.ingest_gitlab_webhook(body)


@router.post("/workspace/{workspace_id}/sync-repo", response_model=RepoSyncJobResponse, tags=["sync"])
async def sync_repo(
    workspace_id: str,
    body: RepoSyncRequest,
    engine: EngineDepends,
) -> RepoSyncJobResponse:
    try:
        return await engine.sync_repo(workspace_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/workspace/{workspace_id}/sync-batch", response_model=RepoSyncBatchResponse, tags=["sync"])
async def sync_batch(
    workspace_id: str,
    body: RepoSyncBatchRequest,
    engine: EngineDepends,
) -> RepoSyncBatchResponse:
    try:
        return await engine.sync_batch(workspace_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/workspace/{workspace_id}/sync-all-repos", response_model=RepoSyncAllResponse, tags=["sync"])
async def sync_all_repos(
    workspace_id: str,
    body: RepoSyncAllRequest,
    engine: EngineDepends,
) -> RepoSyncAllResponse:
    try:
        return await engine.sync_all_repos(workspace_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sync-jobs/{job_id}", response_model=RepoSyncJobResponse, tags=["sync"])
async def get_sync_job(job_id: str, engine: EngineDepends) -> RepoSyncJobResponse:
    try:
        return await engine.get_sync_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/workspace/{workspace_id}/repos/{repo_id}/sync-status",
    response_model=RepoSyncStatusResponse,
    tags=["sync"],
)
async def get_sync_status(workspace_id: str, repo_id: str, engine: EngineDepends) -> RepoSyncStatusResponse:
    try:
        return await engine.get_repo_sync_status(workspace_id, repo_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/commit-diff-summaries/upsert", response_model=CommitDiffSummaryRecord, tags=["vector"])
async def upsert_commit_diff_summary(
    body: CommitDiffSummaryUpsertRequest,
    engine: EngineDepends,
) -> CommitDiffSummaryRecord:
    try:
        return await engine.upsert_commit_diff_summary(body)
    except RuntimeError as exc:
        if str(exc) in {"vector_disabled", "vector_unavailable"}:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/commit-diff-summaries/search", response_model=CommitDiffSummarySearchResponse, tags=["vector"])
async def search_commit_diff_summaries(
    body: CommitDiffSummarySearchRequest,
    engine: EngineDepends,
) -> CommitDiffSummarySearchResponse:
    try:
        return await engine.search_commit_diff_summaries(body)
    except RuntimeError as exc:
        if str(exc) in {"vector_disabled", "vector_unavailable"}:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/commit-diff-summaries/{workspace_id}/{repo_id}/{commit_sha}",
    response_model=CommitDiffSummaryGetResponse,
    tags=["vector"],
)
async def get_commit_diff_summary(
    workspace_id: str,
    repo_id: str,
    commit_sha: str,
    engine: EngineDepends,
    include_embedding: bool = Query(default=False),
) -> CommitDiffSummaryGetResponse:
    try:
        return await engine.get_commit_diff_summary(
            workspace_id,
            repo_id,
            commit_sha,
            include_embedding=include_embedding,
        )
    except RuntimeError as exc:
        if str(exc) in {"vector_disabled", "vector_unavailable"}:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    writer = getattr(request.app.state, "writer", None)

    rg_version: str = getattr(request.app.state, "rg_version", "")
    rg_available = bool(rg_version) or shutil.which(settings.rg_binary) is not None
    extractor_available = shutil.which(settings.extractor_binary) is not None

    try:
        file_count = await repo.count_tracked_files()
    except Exception:
        file_count = 0
    try:
        symbol_count = await repo.count_symbols()
    except Exception:
        symbol_count = 0
    try:
        active_context_count = await repo.count_active_contexts()
    except Exception:
        active_context_count = 0
    try:
        overlay_disk = await repo.get_overlay_disk_usage_bytes()
    except Exception:
        overlay_disk = 0
    try:
        index_depth = await repo.get_index_queue_depth()
    except Exception:
        index_depth = 0
    try:
        oldest_pending = await repo.get_oldest_pending_job_age_s()
    except Exception:
        oldest_pending = 0.0
    try:
        sync_queue_depth = await repo.get_repo_sync_queue_depth()
    except Exception:
        sync_queue_depth = 0
    try:
        active_sync_jobs = await repo.get_active_sync_jobs()
    except Exception:
        active_sync_jobs = 0
    try:
        sync_failures_1h = await repo.get_sync_failures_last_hour()
    except Exception:
        sync_failures_1h = 0

    return HealthResponse(
        status="ok",
        version=__version__,
        cache_file_count=file_count,
        cache_symbol_count=symbol_count,
        rg_available=rg_available,
        rg_version=rg_version,
        extractor_available=extractor_available,
        writer_queue_depth=getattr(writer, "queue_depth", 0),
        writer_lag_ms=getattr(writer, "lag_ms", 0.0),
        active_context_count=active_context_count,
        overlay_disk_usage_bytes=overlay_disk,
        index_queue_depth=index_depth,
        oldest_pending_job_age_s=oldest_pending,
        sync_queue_depth=sync_queue_depth,
        active_sync_jobs=active_sync_jobs,
        last_sync_failure_count_1h=sync_failures_1h,
        sqlite_vec_loaded=is_sqlite_vec_loaded(),
    )
