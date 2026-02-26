"""Workspace-aware orchestration engine with v3 query + v4 sync/vector APIs."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from cxxtract.cache import repository as repo
from cxxtract.config import Settings
from cxxtract.models import (
    CacheInvalidateRequest,
    CacheInvalidateResponse,
    ClassifyFreshnessRequest,
    CommitDiffSummaryGetResponse,
    CommitDiffSummarySearchRequest,
    CommitDiffSummarySearchResponse,
    CommitDiffSummaryUpsertRequest,
    CommitDiffSummaryRecord,
    CallGraphRequest,
    CallGraphResponse,
    ContextCreateOverlayRequest,
    ContextCreateOverlayResponse,
    ContextExpireResponse,
    DefinitionResponse,
    FetchCallEdgesRequest,
    FetchCallEdgesResponse,
    FetchReferencesRequest,
    FetchReferencesResponse,
    FetchSymbolsRequest,
    FetchSymbolsResponse,
    FileSymbolsRequest,
    FileSymbolsResponse,
    GetCompileCommandRequest,
    GetCompileCommandResponse,
    GetConfidenceRequest,
    GetConfidenceResponse,
    ListCandidatesRequest,
    ListCandidatesResponse,
    OverlayMode,
    ParseFileRequest,
    ParseFileResponse,
    ReadFileRequest,
    ReadFileResponse,
    RepoSyncBatchRequest,
    RepoSyncBatchResponse,
    RepoSyncAllRequest,
    RepoSyncAllResponse,
    RepoSyncJobResponse,
    RepoSyncJobStatus,
    RepoSyncRequest,
    RepoSyncStatusResponse,
    ReferencesResponse,
    RgSearchRequest,
    RgSearchResponse,
    SymbolQueryRequest,
    WebhookGitLabRequest,
    WebhookGitLabResponse,
    WorkspaceInfoResponse,
    WorkspaceRefreshResponse,
    WorkspaceRegisterRequest,
)
from cxxtract.orchestrator.services.commit_summary_service import CommitSummaryService
from cxxtract.orchestrator.services.candidate_service import CandidateService
from cxxtract.orchestrator.services.freshness_service import FreshnessService
from cxxtract.orchestrator.services.exploration_service import ExplorationService
from cxxtract.orchestrator.services.query_read_service import QueryReadService
from cxxtract.orchestrator.services.workspace_context_service import WorkspaceContextService
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
        self._explore = ExplorationService(settings, self._workspace_context, self._candidate, self._freshness, self._reader)
        self._commit_summaries = CommitSummaryService(settings)

    @property
    def workspace_context_service(self) -> WorkspaceContextService:
        """Expose workspace service for background workers."""
        return self._workspace_context

    async def explore_rg_search(self, request: RgSearchRequest) -> RgSearchResponse:
        return await self._explore.rg_search(request)

    async def explore_read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        return await self._explore.read_file(request)

    async def explore_get_compile_command(self, request: GetCompileCommandRequest) -> GetCompileCommandResponse:
        return await self._explore.get_compile_command(request)

    async def explore_list_candidates(self, request: ListCandidatesRequest) -> ListCandidatesResponse:
        return await self._explore.list_candidates(request)

    async def explore_classify_freshness(self, request: ClassifyFreshnessRequest) -> ClassifyFreshnessResponse:
        return await self._explore.classify_freshness(request)

    async def explore_parse_file(self, request: ParseFileRequest) -> ParseFileResponse:
        return await self._explore.parse_file(request)

    async def explore_fetch_symbols(self, request: FetchSymbolsRequest) -> FetchSymbolsResponse:
        return await self._explore.fetch_symbols(request)

    async def explore_fetch_references(self, request: FetchReferencesRequest) -> FetchReferencesResponse:
        return await self._explore.fetch_references(request)

    async def explore_fetch_call_edges(self, request: FetchCallEdgesRequest) -> FetchCallEdgesResponse:
        return await self._explore.fetch_call_edges(request)

    async def explore_get_confidence(self, request: GetConfidenceRequest) -> GetConfidenceResponse:
        return await self._explore.get_confidence(request)

    async def query_references(self, request: SymbolQueryRequest) -> ReferencesResponse:
        max_files = request.max_recall_files or self._settings.max_recall_files
        workers = request.max_parse_workers or self._settings.max_parse_workers

        candidates = await self.explore_list_candidates(
            ListCandidatesRequest(
                workspace_id=request.workspace_id,
                symbol=request.symbol,
                analysis_context=request.analysis_context,
                scope=request.scope,
                repo_overrides=request.repo_overrides,
                max_files=max_files,
                include_rg=True,
            )
        )
        freshness = await self.explore_classify_freshness(
            ClassifyFreshnessRequest(
                workspace_id=request.workspace_id,
                analysis_context=request.analysis_context,
                repo_overrides=request.repo_overrides,
                candidate_file_keys=candidates.candidates,
                max_files=max_files,
            )
        )
        parse = await self.explore_parse_file(
            ParseFileRequest(
                workspace_id=request.workspace_id,
                analysis_context=request.analysis_context,
                repo_overrides=request.repo_overrides,
                file_keys=freshness.stale,
                max_parse_workers=workers,
                timeout_s=self._settings.parse_timeout_s,
                skip_if_fresh=True,
            )
        )
        symbols = await self.explore_fetch_symbols(
            FetchSymbolsRequest(
                workspace_id=request.workspace_id,
                analysis_context=request.analysis_context,
                symbol=request.symbol,
                candidate_file_keys=candidates.candidates,
                excluded_file_keys=candidates.deleted_file_keys,
                limit=1,
            )
        )
        refs = await self.explore_fetch_references(
            FetchReferencesRequest(
                workspace_id=request.workspace_id,
                analysis_context=request.analysis_context,
                symbol=request.symbol,
                candidate_file_keys=candidates.candidates,
                excluded_file_keys=candidates.deleted_file_keys,
                limit=20000,
            )
        )
        confidence_resp = await self.explore_get_confidence(
            GetConfidenceRequest(
                verified_files=sorted(set(freshness.fresh + parse.parsed_file_keys)),
                stale_files=sorted(set(parse.failed_file_keys)),
                unparsed_files=sorted(set(freshness.unparsed + parse.unparsed_file_keys)),
                warnings=sorted(set(candidates.warnings + parse.parse_warnings)),
                overlay_mode=candidates.overlay_mode,
            )
        )

        return ReferencesResponse(
            symbol=request.symbol,
            definition=symbols.symbols[0] if symbols.symbols else None,
            references=refs.references,
            confidence=confidence_resp.confidence,
        )

    async def query_definition(self, request: SymbolQueryRequest) -> DefinitionResponse:
        max_files = request.max_recall_files or self._settings.max_recall_files
        workers = request.max_parse_workers or self._settings.max_parse_workers

        candidates = await self.explore_list_candidates(
            ListCandidatesRequest(
                workspace_id=request.workspace_id,
                symbol=request.symbol,
                analysis_context=request.analysis_context,
                scope=request.scope,
                repo_overrides=request.repo_overrides,
                max_files=max_files,
                include_rg=True,
            )
        )
        freshness = await self.explore_classify_freshness(
            ClassifyFreshnessRequest(
                workspace_id=request.workspace_id,
                analysis_context=request.analysis_context,
                repo_overrides=request.repo_overrides,
                candidate_file_keys=candidates.candidates,
                max_files=max_files,
            )
        )
        parse = await self.explore_parse_file(
            ParseFileRequest(
                workspace_id=request.workspace_id,
                analysis_context=request.analysis_context,
                repo_overrides=request.repo_overrides,
                file_keys=freshness.stale,
                max_parse_workers=workers,
                timeout_s=self._settings.parse_timeout_s,
                skip_if_fresh=True,
            )
        )
        symbols = await self.explore_fetch_symbols(
            FetchSymbolsRequest(
                workspace_id=request.workspace_id,
                analysis_context=request.analysis_context,
                symbol=request.symbol,
                candidate_file_keys=candidates.candidates,
                excluded_file_keys=candidates.deleted_file_keys,
                limit=20000,
            )
        )
        confidence_resp = await self.explore_get_confidence(
            GetConfidenceRequest(
                verified_files=sorted(set(freshness.fresh + parse.parsed_file_keys)),
                stale_files=sorted(set(parse.failed_file_keys)),
                unparsed_files=sorted(set(freshness.unparsed + parse.unparsed_file_keys)),
                warnings=sorted(set(candidates.warnings + parse.parse_warnings)),
                overlay_mode=candidates.overlay_mode,
            )
        )

        return DefinitionResponse(
            symbol=request.symbol,
            definitions=symbols.symbols,
            confidence=confidence_resp.confidence,
        )

    async def query_call_graph(self, request: CallGraphRequest) -> CallGraphResponse:
        max_files = request.max_recall_files or self._settings.max_recall_files
        workers = request.max_parse_workers or self._settings.max_parse_workers

        candidates = await self.explore_list_candidates(
            ListCandidatesRequest(
                workspace_id=request.workspace_id,
                symbol=request.symbol,
                analysis_context=request.analysis_context,
                scope=request.scope,
                repo_overrides=request.repo_overrides,
                max_files=max_files,
                include_rg=True,
            )
        )
        freshness = await self.explore_classify_freshness(
            ClassifyFreshnessRequest(
                workspace_id=request.workspace_id,
                analysis_context=request.analysis_context,
                repo_overrides=request.repo_overrides,
                candidate_file_keys=candidates.candidates,
                max_files=max_files,
            )
        )
        parse = await self.explore_parse_file(
            ParseFileRequest(
                workspace_id=request.workspace_id,
                analysis_context=request.analysis_context,
                repo_overrides=request.repo_overrides,
                file_keys=freshness.stale,
                max_parse_workers=workers,
                timeout_s=self._settings.parse_timeout_s,
                skip_if_fresh=True,
            )
        )
        edges_resp = await self.explore_fetch_call_edges(
            FetchCallEdgesRequest(
                workspace_id=request.workspace_id,
                analysis_context=request.analysis_context,
                symbol=request.symbol,
                direction=request.direction,
                candidate_file_keys=candidates.candidates,
                excluded_file_keys=candidates.deleted_file_keys,
                limit=20000,
            )
        )
        confidence_resp = await self.explore_get_confidence(
            GetConfidenceRequest(
                verified_files=sorted(set(freshness.fresh + parse.parsed_file_keys)),
                stale_files=sorted(set(parse.failed_file_keys)),
                unparsed_files=sorted(set(freshness.unparsed + parse.unparsed_file_keys)),
                warnings=sorted(set(candidates.warnings + parse.parse_warnings)),
                overlay_mode=candidates.overlay_mode,
            )
        )

        return CallGraphResponse(
            symbol=request.symbol,
            edges=edges_resp.edges,
            confidence=confidence_resp.confidence,
        )

    async def query_file_symbols(self, request: FileSymbolsRequest) -> FileSymbolsResponse:
        parse = await self.explore_parse_file(
            ParseFileRequest(
                workspace_id=request.workspace_id,
                analysis_context=request.analysis_context,
                repo_overrides=request.repo_overrides,
                file_keys=[request.file_key],
                max_parse_workers=1,
                timeout_s=self._settings.parse_timeout_s,
                skip_if_fresh=True,
            )
        )
        symbols_resp = await self.explore_fetch_symbols(
            FetchSymbolsRequest(
                workspace_id=request.workspace_id,
                analysis_context=request.analysis_context,
                symbol="",
                candidate_file_keys=[request.file_key],
                excluded_file_keys=[],
                limit=20000,
            )
        )
        confidence_resp = await self.explore_get_confidence(
            GetConfidenceRequest(
                verified_files=sorted(set(parse.parsed_file_keys + parse.skipped_fresh_file_keys)),
                stale_files=sorted(set(parse.failed_file_keys)),
                unparsed_files=sorted(set(parse.unparsed_file_keys)),
                warnings=parse.parse_warnings,
                overlay_mode=parse.overlay_mode,
            )
        )
        return FileSymbolsResponse(
            file_key=request.file_key,
            symbols=symbols_resp.symbols,
            confidence=confidence_resp.confidence,
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

    @staticmethod
    def _sync_job_model(row: dict) -> RepoSyncJobResponse:
        return RepoSyncJobResponse(
            job_id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            repo_id=str(row["repo_id"]),
            requested_commit_sha=str(row["requested_commit_sha"]),
            requested_branch=str(row.get("requested_branch", "")),
            requested_force_clean=bool(int(row.get("requested_force_clean", 1))),
            resolved_commit_sha=str(row.get("resolved_commit_sha", "")),
            status=RepoSyncJobStatus(str(row.get("status", RepoSyncJobStatus.PENDING.value))),
            attempts=int(row.get("attempts", 0)),
            max_attempts=int(row.get("max_attempts", 0)),
            error_code=str(row.get("error_code", "")),
            error_message=str(row.get("error_message", "")),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
            started_at=str(row.get("started_at", "")),
            finished_at=str(row.get("finished_at", "")),
        )

    async def sync_repo(self, workspace_id: str, request: RepoSyncRequest) -> RepoSyncJobResponse:
        _ws, manifest = await self._workspace_context.resolve_workspace(workspace_id)
        repo_cfg = manifest.repo_map().get(request.repo_id)
        if repo_cfg is None:
            raise ValueError(f"repo not found in manifest: {request.repo_id}")
        if not repo_cfg.remote_url:
            raise ValueError(f"repo {request.repo_id} is not sync-enabled (remote_url missing)")

        job_id = uuid4().hex
        await repo.insert_repo_sync_job(
            job_id=job_id,
            workspace_id=workspace_id,
            repo_id=request.repo_id,
            requested_commit_sha=request.commit_sha,
            requested_branch=request.branch,
            requested_force_clean=request.force_clean,
            max_attempts=self._settings.git_sync_retry_attempts,
        )
        row = await repo.get_repo_sync_job(job_id)
        assert row is not None
        return self._sync_job_model(row)

    async def sync_batch(self, workspace_id: str, request: RepoSyncBatchRequest) -> RepoSyncBatchResponse:
        jobs: list[RepoSyncJobResponse] = []
        for target in request.targets:
            jobs.append(await self.sync_repo(workspace_id, target))
        return RepoSyncBatchResponse(jobs=jobs)

    async def sync_all_repos(self, workspace_id: str, request: RepoSyncAllRequest) -> RepoSyncAllResponse:
        _ws, manifest = await self._workspace_context.resolve_workspace(workspace_id)
        jobs: list[RepoSyncJobResponse] = []
        skipped: list[str] = []
        for repo_cfg in manifest.repos:
            if not repo_cfg.remote_url:
                skipped.append(repo_cfg.repo_id)
                continue
            sync_req = RepoSyncRequest(
                repo_id=repo_cfg.repo_id,
                commit_sha=repo_cfg.commit_sha,
                branch=repo_cfg.default_branch,
                force_clean=request.force_clean,
            )
            jobs.append(await self.sync_repo(workspace_id, sync_req))
        return RepoSyncAllResponse(workspace_id=workspace_id, jobs=jobs, skipped_repos=skipped)

    async def get_sync_job(self, job_id: str) -> RepoSyncJobResponse:
        row = await repo.get_repo_sync_job(job_id)
        if row is None:
            raise ValueError(f"sync job not found: {job_id}")
        return self._sync_job_model(row)

    async def get_repo_sync_status(self, workspace_id: str, repo_id: str) -> RepoSyncStatusResponse:
        await self._workspace_context.resolve_workspace(workspace_id)
        row = await repo.get_repo_sync_state(workspace_id, repo_id)
        if row is None:
            return RepoSyncStatusResponse(workspace_id=workspace_id, repo_id=repo_id)
        return RepoSyncStatusResponse(
            workspace_id=workspace_id,
            repo_id=repo_id,
            last_synced_commit_sha=str(row.get("last_synced_commit_sha", "")),
            last_synced_branch=str(row.get("last_synced_branch", "")),
            last_success_at=str(row.get("last_success_at", "")),
            last_failure_at=str(row.get("last_failure_at", "")),
            last_error_code=str(row.get("last_error_code", "")),
            last_error_message=str(row.get("last_error_message", "")),
        )

    async def upsert_commit_diff_summary(
        self,
        request: CommitDiffSummaryUpsertRequest,
    ) -> CommitDiffSummaryRecord:
        await self._workspace_context.resolve_workspace(request.workspace_id)
        return await self._commit_summaries.upsert_summary_with_embedding(request)

    async def search_commit_diff_summaries(
        self,
        request: CommitDiffSummarySearchRequest,
    ) -> CommitDiffSummarySearchResponse:
        if request.workspace_id:
            await self._workspace_context.resolve_workspace(request.workspace_id)
        return await self._commit_summaries.search_summaries(request)

    async def get_commit_diff_summary(
        self,
        workspace_id: str,
        repo_id: str,
        commit_sha: str,
        *,
        include_embedding: bool = False,
    ) -> CommitDiffSummaryGetResponse:
        await self._workspace_context.resolve_workspace(workspace_id)
        return await self._commit_summaries.get_summary(
            workspace_id,
            repo_id,
            commit_sha,
            include_embedding=include_embedding,
        )

    async def ingest_gitlab_webhook(self, request: WebhookGitLabRequest) -> WebhookGitLabResponse:
        job_id = uuid4().hex
        workspace_id = str(request.payload.get("workspace_id", ""))
        repo_id = str(request.payload.get("repo_id", ""))
        event_sha = str(
            request.payload.get("event_sha")
            or request.payload.get("commit_sha")
            or request.payload.get("checkout_sha")
            or ""
        )
        branch = str(
            request.payload.get("branch")
            or request.payload.get("ref")
            or ""
        )
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/") :]

        await repo.insert_index_job(
            job_id=job_id,
            workspace_id=workspace_id,
            repo_id=repo_id,
            context_id=request.payload.get("context_id", ""),
            event_type=request.event_type,
            event_sha=event_sha,
        )

        sync_job_id = ""
        if workspace_id and repo_id and len(event_sha) == 40:
            try:
                sync_req = RepoSyncRequest(
                    repo_id=repo_id,
                    commit_sha=event_sha,
                    branch=branch,
                    force_clean=self._settings.git_sync_default_force_clean,
                )
                sync_job = await self.sync_repo(workspace_id, sync_req)
                sync_job_id = sync_job.job_id
            except Exception:
                logger.exception(
                    "Failed to enqueue sync job from webhook workspace=%s repo=%s sha=%s",
                    workspace_id,
                    repo_id,
                    event_sha,
                )

        return WebhookGitLabResponse(
            accepted=True,
            index_job_id=job_id,
            sync_job_id=sync_job_id,
            message="Webhook accepted and index job created",
        )
