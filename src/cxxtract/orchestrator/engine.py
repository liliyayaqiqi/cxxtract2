"""Workspace-aware orchestration engine with baseline/overlay semantics (v3-only)."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from cxxtract.cache import repository as repo
from cxxtract.config import Settings
from cxxtract.models import (
    CacheInvalidateRequest,
    CacheInvalidateResponse,
    CallGraphRequest,
    CallGraphResponse,
    ConfidenceEnvelope,
    ContextCreateOverlayRequest,
    ContextCreateOverlayResponse,
    ContextExpireResponse,
    DefinitionResponse,
    FileSymbolsRequest,
    FileSymbolsResponse,
    OverlayMode,
    ReferencesResponse,
    SymbolQueryRequest,
    WebhookGitLabRequest,
    WebhookGitLabResponse,
    WorkspaceInfoResponse,
    WorkspaceRefreshResponse,
    WorkspaceRegisterRequest,
)
from cxxtract.orchestrator.services.candidate_service import CandidateService
from cxxtract.orchestrator.services.freshness_service import FreshnessService
from cxxtract.orchestrator.services.query_read_service import QueryReadService
from cxxtract.orchestrator.services.workspace_context_service import WorkspaceContextService
from cxxtract.orchestrator.workspace import file_key_to_abs_path
from cxxtract.orchestrator.writer import SingleWriterService

logger = logging.getLogger(__name__)


class _InlineWriter:
    """Fallback writer for tests."""

    queue_depth = 0
    lag_ms = 0.0

    async def enqueue(self, payload) -> None:
        await repo.upsert_parse_payload(payload)

    async def flush(self) -> None:
        return


class OrchestratorEngine:
    """Multi-repo query engine."""

    def __init__(self, settings: Settings, writer: SingleWriterService | None = None) -> None:
        self._settings = settings
        self._writer = writer or _InlineWriter()
        self._workspace_context = WorkspaceContextService(settings)
        self._candidate = CandidateService(settings)
        self._freshness = FreshnessService(settings, self._writer)
        self._reader = QueryReadService()

    @staticmethod
    def _confidence(
        verified: list[str],
        stale: list[str],
        unparsed: list[str],
        warnings: list[str],
        overlay_mode: OverlayMode,
    ) -> ConfidenceEnvelope:
        total = len(verified) + len(stale) + len(unparsed)
        verified_ratio = len(verified) / total if total else 0.0

        repo_total: dict[str, int] = {}
        repo_verified: dict[str, int] = {}
        for fk in verified + stale + unparsed:
            repo_id = fk.split(":", 1)[0] if ":" in fk else "unknown"
            repo_total[repo_id] = repo_total.get(repo_id, 0) + 1
        for fk in verified:
            repo_id = fk.split(":", 1)[0] if ":" in fk else "unknown"
            repo_verified[repo_id] = repo_verified.get(repo_id, 0) + 1

        return ConfidenceEnvelope(
            verified_files=verified,
            stale_files=stale,
            unparsed_files=unparsed,
            total_candidates=total,
            verified_ratio=round(verified_ratio, 4),
            warnings=sorted(set(warnings)),
            overlay_mode=overlay_mode,
            repo_coverage={
                repo_id: round(repo_verified.get(repo_id, 0) / count, 4)
                for repo_id, count in repo_total.items()
                if count > 0
            },
        )

    async def _prepare(self, req: SymbolQueryRequest | CallGraphRequest):
        ws, manifest = await self._workspace_context.resolve_workspace(req.workspace_id)
        workspace_root = ws["root_path"]
        context_id, baseline_id, overlay_mode = await self._workspace_context.resolve_contexts(req)
        await repo.touch_context(context_id)

        repo_ids = self._workspace_context.candidate_repos(manifest, req.scope.entry_repos, req.scope.max_repo_hops)
        compile_dbs = self._workspace_context.resolve_compile_dbs(
            req.workspace_id,
            workspace_root,
            manifest,
            repo_ids,
            req.repo_overrides,
        )
        return workspace_root, manifest, context_id, baseline_id, overlay_mode, repo_ids, compile_dbs

    async def query_references(self, request: SymbolQueryRequest) -> ReferencesResponse:
        workspace_root, manifest, context_id, baseline_id, overlay_mode, repo_ids, compile_dbs = await self._prepare(request)

        max_files = request.max_recall_files or self._settings.max_recall_files
        workers = request.max_parse_workers or self._settings.max_parse_workers

        candidates, deleted, recall_warnings = await self._candidate.resolve_candidates(
            request.symbol,
            context_id,
            baseline_id,
            repo_ids,
            workspace_root,
            manifest,
            max_files,
        )

        fresh, _stale, unparsed, tasks = await self._freshness.classify(
            context_id,
            candidates,
            compile_dbs,
            workspace_root,
            manifest,
        )
        parsed, failed, parse_warnings = await self._freshness.parse(tasks, workspace_root, manifest, workers)

        chain = [context_id] if context_id == baseline_id else [context_id, baseline_id]
        definition = await self._reader.load_definition(
            request.symbol,
            context_chain=chain,
            candidate_file_keys=set(candidates),
            excluded_file_keys=deleted,
        )
        references = await self._reader.load_references(
            request.symbol,
            context_chain=chain,
            candidate_file_keys=set(candidates),
            excluded_file_keys=deleted,
        )

        return ReferencesResponse(
            symbol=request.symbol,
            definition=definition,
            references=references,
            confidence=self._confidence(fresh + parsed, failed, unparsed, recall_warnings + parse_warnings, overlay_mode),
        )

    async def query_definition(self, request: SymbolQueryRequest) -> DefinitionResponse:
        workspace_root, manifest, context_id, baseline_id, overlay_mode, repo_ids, compile_dbs = await self._prepare(request)

        max_files = request.max_recall_files or self._settings.max_recall_files
        workers = request.max_parse_workers or self._settings.max_parse_workers

        candidates, deleted, recall_warnings = await self._candidate.resolve_candidates(
            request.symbol,
            context_id,
            baseline_id,
            repo_ids,
            workspace_root,
            manifest,
            max_files,
        )

        fresh, _stale, unparsed, tasks = await self._freshness.classify(
            context_id,
            candidates,
            compile_dbs,
            workspace_root,
            manifest,
        )
        parsed, failed, parse_warnings = await self._freshness.parse(tasks, workspace_root, manifest, workers)

        chain = [context_id] if context_id == baseline_id else [context_id, baseline_id]
        definitions = await self._reader.load_definitions(
            request.symbol,
            context_chain=chain,
            candidate_file_keys=set(candidates),
            excluded_file_keys=deleted,
        )

        return DefinitionResponse(
            symbol=request.symbol,
            definitions=definitions,
            confidence=self._confidence(fresh + parsed, failed, unparsed, recall_warnings + parse_warnings, overlay_mode),
        )

    async def query_call_graph(self, request: CallGraphRequest) -> CallGraphResponse:
        workspace_root, manifest, context_id, baseline_id, overlay_mode, repo_ids, compile_dbs = await self._prepare(request)

        max_files = request.max_recall_files or self._settings.max_recall_files
        workers = request.max_parse_workers or self._settings.max_parse_workers

        candidates, deleted, recall_warnings = await self._candidate.resolve_candidates(
            request.symbol,
            context_id,
            baseline_id,
            repo_ids,
            workspace_root,
            manifest,
            max_files,
        )

        fresh, _stale, unparsed, tasks = await self._freshness.classify(
            context_id,
            candidates,
            compile_dbs,
            workspace_root,
            manifest,
        )
        parsed, failed, parse_warnings = await self._freshness.parse(tasks, workspace_root, manifest, workers)

        chain = [context_id] if context_id == baseline_id else [context_id, baseline_id]
        edges = await self._reader.load_call_edges(
            request.symbol,
            request.direction,
            context_chain=chain,
            candidate_file_keys=set(candidates),
            excluded_file_keys=deleted,
        )

        return CallGraphResponse(
            symbol=request.symbol,
            edges=edges,
            confidence=self._confidence(fresh + parsed, failed, unparsed, recall_warnings + parse_warnings, overlay_mode),
        )

    async def query_file_symbols(self, request: FileSymbolsRequest) -> FileSymbolsResponse:
        ws, manifest = await self._workspace_context.resolve_workspace(request.workspace_id)
        workspace_root = ws["root_path"]
        context_id, baseline_id, overlay_mode = await self._workspace_context.resolve_contexts(request)

        chain = [context_id] if context_id == baseline_id else [context_id, baseline_id]
        resolved = file_key_to_abs_path(workspace_root, manifest, request.file_key)
        if resolved is None:
            confidence = self._confidence([], [], [request.file_key], ["invalid_file_key"], overlay_mode)
            return FileSymbolsResponse(file_key=request.file_key, symbols=[], confidence=confidence)

        repo_id, _rel_path, abs_path = resolved
        cfg = manifest.repo_map().get(repo_id)
        compile_dbs = {repo_id: None}
        if cfg:
            compile_dbs = self._workspace_context.resolve_compile_dbs(
                request.workspace_id,
                workspace_root,
                manifest,
                [repo_id],
                request.repo_overrides,
            )

        fresh, _stale, unparsed, tasks = await self._freshness.classify(
            context_id,
            [request.file_key],
            compile_dbs,
            workspace_root,
            manifest,
        )

        parse_warnings: list[str] = []
        parsed: list[str] = []
        failed: list[str] = []
        if tasks:
            parsed, failed, parse_warnings = await self._freshness.parse(tasks, workspace_root, manifest, 1)

        symbols = await self._reader.load_file_symbols(request.file_key, context_chain=chain)
        return FileSymbolsResponse(
            file_key=request.file_key,
            symbols=symbols,
            confidence=self._confidence(fresh + parsed, failed, unparsed, parse_warnings, overlay_mode),
        )

    async def invalidate_cache(self, request: CacheInvalidateRequest) -> CacheInvalidateResponse:
        await self._workspace_context.resolve_workspace(request.workspace_id)
        context_id = request.context_id or f"{request.workspace_id}:baseline"

        if request.file_keys is None:
            count = await repo.clear_context(context_id)
            return CacheInvalidateResponse(
                invalidated_files=count,
                message=f"Invalidated context cache {context_id} ({count} files)",
            )

        count = 0
        for file_key in request.file_keys:
            tracked = await repo.get_tracked_file(context_id, file_key)
            if tracked:
                await repo.delete_tracked_file(context_id, file_key)
                count += 1

        return CacheInvalidateResponse(
            invalidated_files=count,
            message=f"Invalidated {count} of {len(request.file_keys)} requested file keys",
        )

    async def register_workspace(self, request: WorkspaceRegisterRequest) -> WorkspaceInfoResponse:
        manifest_path = request.manifest_path or str((Path(request.root_path) / self._settings.workspace_manifest_name).resolve())
        await repo.upsert_workspace(request.workspace_id, request.root_path, manifest_path)

        ws, manifest = await self._workspace_context.resolve_workspace(request.workspace_id, reload_manifest=True)
        baseline = await repo.ensure_baseline_context(request.workspace_id)
        return WorkspaceInfoResponse(
            workspace_id=request.workspace_id,
            root_path=ws["root_path"],
            manifest_path=ws["manifest_path"],
            repos=[r.repo_id for r in manifest.repos],
            contexts=[baseline],
        )

    async def get_workspace_info(self, workspace_id: str) -> WorkspaceInfoResponse:
        ws, manifest = await self._workspace_context.resolve_workspace(workspace_id)
        contexts = [c["context_id"] for c in await repo.list_active_contexts(workspace_id)]
        return WorkspaceInfoResponse(
            workspace_id=workspace_id,
            root_path=ws["root_path"],
            manifest_path=ws["manifest_path"],
            repos=[r.repo_id for r in manifest.repos],
            contexts=contexts,
        )

    async def refresh_workspace_manifest(self, workspace_id: str) -> WorkspaceRefreshResponse:
        _, manifest = await self._workspace_context.resolve_workspace(workspace_id, reload_manifest=True)
        return WorkspaceRefreshResponse(
            workspace_id=workspace_id,
            repos_synced=len(manifest.repos),
            message=f"Synced {len(manifest.repos)} repos from workspace manifest",
        )

    async def create_pr_overlay_context(self, request: ContextCreateOverlayRequest) -> ContextCreateOverlayResponse:
        baseline = await repo.ensure_baseline_context(request.workspace_id)
        context_id = request.context_id or f"{request.workspace_id}:pr:{request.pr_id or uuid4().hex[:8]}"

        await repo.upsert_analysis_context(
            context_id,
            request.workspace_id,
            "pr",
            base_context_id=baseline,
            overlay_mode=OverlayMode.SPARSE.value,
        )
        ctx = await repo.get_analysis_context(context_id)
        assert ctx is not None

        return ContextCreateOverlayResponse(
            context_id=context_id,
            workspace_id=request.workspace_id,
            base_context_id=baseline,
            overlay_mode=OverlayMode(ctx["overlay_mode"]),
            overlay_file_count=ctx["overlay_file_count"],
            overlay_row_count=ctx["overlay_row_count"],
            partial_overlay=ctx["overlay_mode"] == OverlayMode.PARTIAL_OVERLAY.value,
        )

    async def expire_context(self, context_id: str) -> ContextExpireResponse:
        expired = await repo.expire_context(context_id)
        return ContextExpireResponse(
            context_id=context_id,
            expired=expired,
            message="expired" if expired else "context not found",
        )

    async def ingest_gitlab_webhook(self, request: WebhookGitLabRequest) -> WebhookGitLabResponse:
        job_id = uuid4().hex
        await repo.insert_index_job(
            job_id=job_id,
            workspace_id=request.payload.get("workspace_id", ""),
            repo_id=request.payload.get("repo_id", ""),
            context_id=request.payload.get("context_id", ""),
            event_type=request.event_type,
            event_sha=request.payload.get("event_sha", ""),
        )
        return WebhookGitLabResponse(
            accepted=True,
            index_job_id=job_id,
            message="Webhook accepted and index job created",
        )
