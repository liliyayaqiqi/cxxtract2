"""Layered exploration primitives composed from existing orchestration internals."""

from __future__ import annotations

from pathlib import Path

from cxxtract.config import Settings
from cxxtract.models import (
    CandidateProvenance,
    ClassifyFreshnessRequest,
    ClassifyFreshnessResponse,
    CompileMatchType,
    CostEnvelope,
    CoverageEnvelope,
    EvidenceItem,
    FetchCallEdgesRequest,
    FetchCallEdgesResponse,
    FetchReferencesRequest,
    FetchReferencesResponse,
    FetchSymbolsRequest,
    FetchSymbolsResponse,
    FreshnessParseTask,
    GetCompileCommandRequest,
    GetCompileCommandResponse,
    GetConfidenceRequest,
    GetConfidenceResponse,
    ListCandidatesRequest,
    ListCandidatesResponse,
    ParseFileRequest,
    ParseFileResponse,
    ReadFileRequest,
    ReadFileResponse,
    RgSearchHit,
    RgSearchRequest,
    RgSearchResponse,
)
from cxxtract.orchestrator.recall import run_recall_query
from cxxtract.orchestrator.services.candidate_service import CandidateService
from cxxtract.orchestrator.services.confidence_service import build_confidence
from cxxtract.orchestrator.services.freshness_service import FreshnessService
from cxxtract.orchestrator.services.query_read_service import QueryReadService
from cxxtract.orchestrator.services.workspace_context_service import WorkspaceContextService
from cxxtract.orchestrator.workspace import file_key_to_abs_path, resolve_file_key


class ExplorationService:
    """High-level primitive exploration API over stable core internals."""

    _HARD_MAX_RG_HITS = 5000
    _HARD_MAX_FETCH_LIMIT = 20000
    _HARD_MAX_READ_BYTES = 512 * 1024
    _HARD_MAX_PARSE_FILES = 5000

    def __init__(
        self,
        settings: Settings,
        workspace_context: WorkspaceContextService,
        candidate: CandidateService,
        freshness: FreshnessService,
        reader: QueryReadService,
    ) -> None:
        self._settings = settings
        self._workspace_context = workspace_context
        self._candidate = candidate
        self._freshness = freshness
        self._reader = reader

    @staticmethod
    def _apply_cap(value: int, hard_cap: int, name: str, reasons: list[str]) -> int:
        applied = min(max(1, value), max(1, hard_cap))
        if value > hard_cap:
            reasons.append(name)
        return applied

    @staticmethod
    def _cost(
        *,
        requested: dict[str, int],
        applied: dict[str, int],
        consumed: dict[str, int],
        truncation_reasons: list[str],
    ) -> CostEnvelope:
        return CostEnvelope(
            requested=requested,
            applied=applied,
            consumed=consumed,
            truncated=bool(truncation_reasons),
            truncation_reasons=sorted(set(truncation_reasons)),
        )

    @staticmethod
    def _coverage(
        *,
        total_candidates: int,
        considered_candidates: int,
        verified_candidates: int,
        partial_reasons: list[str],
    ) -> CoverageEnvelope:
        return CoverageEnvelope(
            total_candidates=total_candidates,
            considered_candidates=considered_candidates,
            verified_candidates=verified_candidates,
            partial=bool(partial_reasons),
            partial_reasons=sorted(set(partial_reasons)),
        )

    async def rg_search(self, request: RgSearchRequest) -> RgSearchResponse:
        ws, manifest = await self._workspace_context.resolve_workspace(request.workspace_id)
        workspace_root = ws["root_path"]
        context_id, baseline_id, overlay_mode = await self._workspace_context.resolve_contexts(request)
        del context_id, baseline_id, overlay_mode

        repo_ids = self._workspace_context.candidate_repos(manifest, request.scope.entry_repos, request.scope.max_repo_hops)

        truncation_reasons: list[str] = []
        applied_max_files = self._apply_cap(request.max_files, self._settings.max_recall_files, "max_files", truncation_reasons)
        applied_max_hits = self._apply_cap(request.max_hits, self._HARD_MAX_RG_HITS, "max_hits", truncation_reasons)
        applied_timeout = self._apply_cap(request.timeout_s, self._settings.recall_timeout_s, "timeout_s", truncation_reasons)

        hits: list[RgSearchHit] = []
        warnings: list[str] = []
        evidence: list[EvidenceItem] = []
        seen_hit_keys: set[tuple[str, int]] = set()
        seen_file_keys: set[str] = set()

        repo_map = manifest.repo_map()
        per_repo = max(20, applied_max_files // max(1, len(repo_ids)))
        for repo_id in repo_ids:
            cfg = repo_map.get(repo_id)
            if cfg is None:
                continue
            repo_root = str((Path(workspace_root) / cfg.root).resolve())
            result = await run_recall_query(
                request.query,
                repo_root,
                mode=request.mode.value,
                rg_binary=self._settings.rg_binary,
                max_files=per_repo,
                timeout_s=applied_timeout,
                context_lines=request.context_lines,
            )
            if result.error:
                warnings.append(f"recall[{repo_id}]: {result.error}")
            for h in result.hits:
                resolved = resolve_file_key(workspace_root, manifest, h.file_path)
                if resolved is None:
                    continue
                file_key, resolved_repo_id, _rel, abs_path = resolved
                hit_key = (file_key, h.line_number)
                if hit_key in seen_hit_keys:
                    continue
                seen_hit_keys.add(hit_key)
                seen_file_keys.add(file_key)
                hits.append(
                    RgSearchHit(
                        file_key=file_key,
                        repo_id=resolved_repo_id,
                        abs_path=abs_path,
                        line=h.line_number,
                        line_text=h.line_text,
                    )
                )
                evidence.append(
                    EvidenceItem(
                        source="rg",
                        file_key=file_key,
                        line=h.line_number,
                        snippet=h.line_text[:200],
                    )
                )
                if len(hits) >= applied_max_hits:
                    truncation_reasons.append("max_hits")
                    break
            if len(hits) >= applied_max_hits:
                break
            if len(seen_file_keys) >= applied_max_files:
                truncation_reasons.append("max_files")
                break

        cost = self._cost(
            requested={"max_hits": request.max_hits, "max_files": request.max_files, "timeout_s": request.timeout_s},
            applied={"max_hits": applied_max_hits, "max_files": applied_max_files, "timeout_s": applied_timeout},
            consumed={"hits": len(hits), "files": len(seen_file_keys)},
            truncation_reasons=truncation_reasons,
        )
        coverage = self._coverage(
            total_candidates=len(seen_file_keys),
            considered_candidates=len(seen_file_keys),
            verified_candidates=0,
            partial_reasons=cost.truncation_reasons,
        )
        return RgSearchResponse(
            hits=hits[:applied_max_hits],
            warnings=sorted(set(warnings)),
            cost=cost,
            evidence=evidence[:applied_max_hits],
            coverage=coverage,
        )

    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        ws, manifest = await self._workspace_context.resolve_workspace(request.workspace_id)
        workspace_root = Path(ws["root_path"]).resolve()

        truncation_reasons: list[str] = []
        applied_max_bytes = self._apply_cap(request.max_bytes, self._HARD_MAX_READ_BYTES, "max_bytes", truncation_reasons)

        resolved = file_key_to_abs_path(str(workspace_root), manifest, request.file_key)
        if resolved is None:
            return ReadFileResponse(
                file_key=request.file_key,
                warnings=["invalid_file_key"],
                cost=self._cost(
                    requested={"max_bytes": request.max_bytes},
                    applied={"max_bytes": applied_max_bytes},
                    consumed={"bytes": 0, "lines": 0},
                    truncation_reasons=truncation_reasons,
                ),
            )

        _repo_id, _rel, abs_path = resolved
        path_obj = Path(abs_path).resolve()
        warnings: list[str] = []
        if not path_obj.is_relative_to(workspace_root):
            warnings.append("file_outside_workspace")
            return ReadFileResponse(
                file_key=request.file_key,
                abs_path=str(path_obj).replace("\\", "/"),
                warnings=warnings,
                cost=self._cost(
                    requested={"max_bytes": request.max_bytes},
                    applied={"max_bytes": applied_max_bytes},
                    consumed={"bytes": 0, "lines": 0},
                    truncation_reasons=truncation_reasons,
                ),
            )

        try:
            raw = path_obj.read_bytes()
        except OSError:
            warnings.append("read_failed")
            raw = b""
        content_text = raw.decode("utf-8", errors="replace")
        lines = content_text.splitlines()

        start_line = max(1, request.start_line)
        end_line = request.end_line if request.end_line > 0 else max(1, len(lines))
        if end_line < start_line:
            end_line = start_line

        sliced_lines = lines[start_line - 1 : end_line]
        selected = "\n".join(sliced_lines)
        selected_bytes = selected.encode("utf-8")
        truncated = False
        if len(selected_bytes) > applied_max_bytes:
            selected = selected_bytes[:applied_max_bytes].decode("utf-8", errors="ignore")
            truncated = True
            truncation_reasons.append("max_bytes")

        line_end = start_line + len(sliced_lines) - 1 if sliced_lines else start_line
        content_hash = ""
        try:
            from cxxtract.cache.hasher import compute_content_hash

            content_hash = compute_content_hash(str(path_obj))
        except Exception:
            warnings.append("content_hash_failed")

        cost = self._cost(
            requested={"max_bytes": request.max_bytes},
            applied={"max_bytes": applied_max_bytes},
            consumed={"bytes": len(selected.encode("utf-8")), "lines": len(sliced_lines)},
            truncation_reasons=truncation_reasons,
        )
        return ReadFileResponse(
            file_key=request.file_key,
            abs_path=str(path_obj).replace("\\", "/"),
            content=selected,
            truncated=truncated or cost.truncated,
            line_range=[start_line, line_end],
            content_hash=content_hash,
            warnings=sorted(set(warnings)),
            cost=cost,
        )

    async def get_compile_command(self, request: GetCompileCommandRequest) -> GetCompileCommandResponse:
        ws, manifest = await self._workspace_context.resolve_workspace(request.workspace_id)
        workspace_root = ws["root_path"]
        context_id, baseline_id, overlay_mode = await self._workspace_context.resolve_contexts(request)
        del context_id, baseline_id, overlay_mode

        resolved = file_key_to_abs_path(workspace_root, manifest, request.file_key)
        if resolved is None:
            return GetCompileCommandResponse(
                file_key=request.file_key,
                match_type=CompileMatchType.MISSING,
                warnings=["invalid_file_key"],
            )

        repo_id, _rel_path, abs_path = resolved
        repo_cfg = manifest.repo_map().get(repo_id)
        cc_path = ""
        if repo_cfg is not None:
            override = request.repo_overrides.get(repo_id)
            cc_cfg = override.compile_commands if override else repo_cfg.compile_commands
            if cc_cfg:
                cc_path = str((Path(workspace_root) / cc_cfg).resolve())

        compile_dbs = self._workspace_context.resolve_compile_dbs(
            request.workspace_id,
            workspace_root,
            manifest,
            [repo_id],
            request.repo_overrides,
        )
        cdb = compile_dbs.get(repo_id)
        if cdb is None:
            return GetCompileCommandResponse(
                file_key=request.file_key,
                compile_db_path=cc_path,
                match_type=CompileMatchType.MISSING,
                warnings=["missing_compile_db"],
            )

        entry = cdb.get(abs_path)
        match_type = CompileMatchType.EXACT
        if entry is None:
            entry = cdb.fallback_entry(abs_path)
            match_type = CompileMatchType.FALLBACK
        if entry is None:
            return GetCompileCommandResponse(
                file_key=request.file_key,
                compile_db_path=cc_path,
                match_type=CompileMatchType.MISSING,
                warnings=["missing_compile_entry"],
            )

        return GetCompileCommandResponse(
            file_key=request.file_key,
            compile_db_path=cc_path,
            match_type=match_type,
            cwd=entry.directory,
            args=list(entry.arguments),
            flags_hash=entry.flags_hash,
        )

    async def list_candidates(self, request: ListCandidatesRequest) -> ListCandidatesResponse:
        ws, manifest = await self._workspace_context.resolve_workspace(request.workspace_id)
        workspace_root = ws["root_path"]
        context_id, baseline_id, overlay_mode = await self._workspace_context.resolve_contexts(request)
        repo_ids = self._workspace_context.candidate_repos(manifest, request.scope.entry_repos, request.scope.max_repo_hops)

        truncation_reasons: list[str] = []
        applied_max_files = self._apply_cap(request.max_files, self._settings.max_recall_files, "max_files", truncation_reasons)

        candidates, deleted, provenance, warnings, truncated, candidate_reasons = await self._candidate.resolve_candidates_detailed(
            request.symbol,
            context_id,
            baseline_id,
            repo_ids,
            workspace_root,
            manifest,
            applied_max_files,
            include_rg=request.include_rg,
        )
        if truncated:
            truncation_reasons.extend(candidate_reasons)

        cost = self._cost(
            requested={"max_files": request.max_files},
            applied={"max_files": applied_max_files},
            consumed={"candidates": len(candidates), "deleted": len(deleted)},
            truncation_reasons=truncation_reasons,
        )
        coverage = self._coverage(
            total_candidates=len(candidates) + len(deleted),
            considered_candidates=len(candidates),
            verified_candidates=0,
            partial_reasons=cost.truncation_reasons,
        )
        provenance_rows = [
            CandidateProvenance(file_key=fk, sources=provenance.get(fk, []))
            for fk in candidates
        ]
        return ListCandidatesResponse(
            workspace_id=request.workspace_id,
            context_id=context_id,
            baseline_context_id=baseline_id,
            overlay_mode=overlay_mode,
            symbol=request.symbol,
            candidates=candidates,
            deleted_file_keys=sorted(deleted),
            provenance=provenance_rows,
            warnings=sorted(set(warnings)),
            cost=cost,
            coverage=coverage,
        )

    async def classify_freshness(self, request: ClassifyFreshnessRequest) -> ClassifyFreshnessResponse:
        ws, manifest = await self._workspace_context.resolve_workspace(request.workspace_id)
        workspace_root = ws["root_path"]
        context_id, baseline_id, overlay_mode = await self._workspace_context.resolve_contexts(request)

        truncation_reasons: list[str] = []
        applied_max_files = self._apply_cap(request.max_files, self._settings.max_recall_files, "max_files", truncation_reasons)
        candidate_file_keys = list(dict.fromkeys(request.candidate_file_keys))[:applied_max_files]
        if len(request.candidate_file_keys) > len(candidate_file_keys):
            truncation_reasons.append("max_files")

        repo_ids = sorted({fk.split(":", 1)[0] for fk in candidate_file_keys if ":" in fk})
        compile_dbs = self._workspace_context.resolve_compile_dbs(
            request.workspace_id,
            workspace_root,
            manifest,
            repo_ids,
            request.repo_overrides,
        )
        fresh, stale, unparsed, tasks, task_meta, warnings = await self._freshness.classify_detailed(
            context_id,
            candidate_file_keys,
            compile_dbs,
            workspace_root,
            manifest,
        )
        parse_queue = [
            FreshnessParseTask(
                file_key=t.file_key,
                repo_id=task_meta.get(t.file_key, ("", CompileMatchType.MISSING, ""))[0],
                compile_match_type=task_meta.get(t.file_key, ("", CompileMatchType.MISSING, ""))[1],
                flags_hash=task_meta.get(t.file_key, ("", CompileMatchType.MISSING, ""))[2],
            )
            for t, _entry in tasks
        ]
        partial_reasons = list(truncation_reasons)
        if unparsed:
            partial_reasons.append("unparsed")
        cost = self._cost(
            requested={"max_files": request.max_files},
            applied={"max_files": applied_max_files},
            consumed={"fresh": len(fresh), "stale": len(stale), "unparsed": len(unparsed)},
            truncation_reasons=truncation_reasons,
        )
        coverage = self._coverage(
            total_candidates=len(candidate_file_keys),
            considered_candidates=len(candidate_file_keys),
            verified_candidates=len(fresh),
            partial_reasons=partial_reasons,
        )
        return ClassifyFreshnessResponse(
            workspace_id=request.workspace_id,
            context_id=context_id,
            baseline_context_id=baseline_id,
            overlay_mode=overlay_mode,
            fresh=fresh,
            stale=stale,
            unparsed=unparsed,
            parse_queue=parse_queue,
            warnings=sorted(set(warnings)),
            cost=cost,
            coverage=coverage,
        )

    async def parse_file(self, request: ParseFileRequest) -> ParseFileResponse:
        ws, manifest = await self._workspace_context.resolve_workspace(request.workspace_id)
        workspace_root = ws["root_path"]
        context_id, baseline_id, overlay_mode = await self._workspace_context.resolve_contexts(request)

        truncation_reasons: list[str] = []
        applied_max_files = self._apply_cap(len(request.file_keys) or 1, self._HARD_MAX_PARSE_FILES, "max_files", truncation_reasons)
        file_keys = list(dict.fromkeys(request.file_keys))[:applied_max_files]
        if len(request.file_keys) > len(file_keys):
            truncation_reasons.append("max_files")

        repo_ids = sorted({fk.split(":", 1)[0] for fk in file_keys if ":" in fk})
        compile_dbs = self._workspace_context.resolve_compile_dbs(
            request.workspace_id,
            workspace_root,
            manifest,
            repo_ids,
            request.repo_overrides,
        )
        fresh, stale, unparsed, tasks, _task_meta, classify_warnings = await self._freshness.classify_detailed(
            context_id,
            file_keys,
            compile_dbs,
            workspace_root,
            manifest,
        )

        applied_workers = self._apply_cap(request.max_parse_workers, self._settings.max_parse_workers, "max_parse_workers", truncation_reasons)
        applied_timeout = self._apply_cap(request.timeout_s, self._settings.parse_timeout_s, "timeout_s", truncation_reasons)

        parsed, failed, parse_warnings, persisted_rows = await self._freshness.parse_detailed(
            tasks,
            workspace_root,
            manifest,
            applied_workers,
            timeout_s=applied_timeout,
        )
        skipped_fresh = fresh if request.skip_if_fresh else []
        partial_reasons = list(truncation_reasons)
        if failed:
            partial_reasons.append("parse_failed")
        if unparsed:
            partial_reasons.append("unparsed")

        cost = self._cost(
            requested={
                "file_count": len(request.file_keys),
                "max_parse_workers": request.max_parse_workers,
                "timeout_s": request.timeout_s,
            },
            applied={
                "file_count": len(file_keys),
                "max_parse_workers": applied_workers,
                "timeout_s": applied_timeout,
            },
            consumed={"parsed": len(parsed), "failed": len(failed), "skipped_fresh": len(skipped_fresh)},
            truncation_reasons=truncation_reasons,
        )
        coverage = self._coverage(
            total_candidates=len(file_keys),
            considered_candidates=len(file_keys),
            verified_candidates=len(parsed) + len(fresh),
            partial_reasons=partial_reasons,
        )
        return ParseFileResponse(
            workspace_id=request.workspace_id,
            context_id=context_id,
            baseline_context_id=baseline_id,
            overlay_mode=overlay_mode,
            parsed_file_keys=parsed,
            failed_file_keys=failed,
            skipped_fresh_file_keys=skipped_fresh,
            unparsed_file_keys=unparsed,
            parse_warnings=sorted(set(classify_warnings + parse_warnings)),
            persisted_fact_rows=persisted_rows,
            cost=cost,
            coverage=coverage,
        )

    async def fetch_symbols(self, request: FetchSymbolsRequest) -> FetchSymbolsResponse:
        context_id, baseline_id, symbols, cost, coverage = await self._fetch_semantic_rows(
            request.workspace_id,
            request.analysis_context,
            request.limit,
            request.candidate_file_keys,
            request.excluded_file_keys,
            fetch_fn=lambda chain, cand, excl: self._reader.load_definitions(
                request.symbol,
                context_chain=chain,
                candidate_file_keys=cand,
                excluded_file_keys=excl,
            ),
        )
        del context_id, baseline_id
        evidence = [
            EvidenceItem(source="cache", file_key=s.file_key, line=s.line, col=s.col, snippet=s.qualified_name[:200])
            for s in symbols
        ]
        return FetchSymbolsResponse(
            symbol=request.symbol,
            symbols=symbols,
            evidence=evidence,
            cost=cost,
            coverage=coverage,
        )

    async def fetch_references(self, request: FetchReferencesRequest) -> FetchReferencesResponse:
        context_id, baseline_id, refs, cost, coverage = await self._fetch_semantic_rows(
            request.workspace_id,
            request.analysis_context,
            request.limit,
            request.candidate_file_keys,
            request.excluded_file_keys,
            fetch_fn=lambda chain, cand, excl: self._reader.load_references(
                request.symbol,
                context_chain=chain,
                candidate_file_keys=cand,
                excluded_file_keys=excl,
            ),
        )
        del context_id, baseline_id
        evidence = [
            EvidenceItem(source="cache", file_key=r.file_key, line=r.line, col=r.col, snippet=r.kind[:200])
            for r in refs
        ]
        return FetchReferencesResponse(
            symbol=request.symbol,
            references=refs,
            evidence=evidence,
            cost=cost,
            coverage=coverage,
        )

    async def fetch_call_edges(self, request: FetchCallEdgesRequest) -> FetchCallEdgesResponse:
        context_id, baseline_id, edges, cost, coverage = await self._fetch_semantic_rows(
            request.workspace_id,
            request.analysis_context,
            request.limit,
            request.candidate_file_keys,
            request.excluded_file_keys,
            fetch_fn=lambda chain, cand, excl: self._reader.load_call_edges(
                request.symbol,
                request.direction,
                context_chain=chain,
                candidate_file_keys=cand,
                excluded_file_keys=excl,
            ),
        )
        del context_id, baseline_id
        evidence = [
            EvidenceItem(
                source="cache",
                file_key=e.file_key,
                line=e.line,
                snippet=f"{e.caller}->{e.callee}"[:200],
            )
            for e in edges
        ]
        return FetchCallEdgesResponse(
            symbol=request.symbol,
            direction=request.direction,
            edges=edges,
            evidence=evidence,
            cost=cost,
            coverage=coverage,
        )

    async def get_confidence(self, request: GetConfidenceRequest) -> GetConfidenceResponse:
        confidence = build_confidence(
            verified=request.verified_files,
            stale=request.stale_files,
            unparsed=request.unparsed_files,
            warnings=request.warnings,
            overlay_mode=request.overlay_mode,
        )
        coverage = self._coverage(
            total_candidates=confidence.total_candidates,
            considered_candidates=confidence.total_candidates,
            verified_candidates=len(confidence.verified_files),
            partial_reasons=(["partial"] if confidence.stale_files or confidence.unparsed_files else []),
        )
        return GetConfidenceResponse(confidence=confidence, coverage=coverage)

    async def _fetch_semantic_rows(
        self,
        workspace_id: str,
        analysis_context,
        requested_limit: int,
        candidate_file_keys: list[str],
        excluded_file_keys: list[str],
        *,
        fetch_fn,
    ):
        class _Req:
            def __init__(self, ws_id, analysis_ctx):
                self.workspace_id = ws_id
                self.analysis_context = analysis_ctx

        req = _Req(workspace_id, analysis_context)
        _ws, _manifest = await self._workspace_context.resolve_workspace(workspace_id)
        context_id, baseline_id, _overlay_mode = await self._workspace_context.resolve_contexts(req)

        truncation_reasons: list[str] = []
        applied_limit = self._apply_cap(requested_limit, self._HARD_MAX_FETCH_LIMIT, "limit", truncation_reasons)
        candidate = set(candidate_file_keys)
        excluded = set(excluded_file_keys)
        chain = [context_id] if context_id == baseline_id else [context_id, baseline_id]
        rows = await fetch_fn(chain, candidate, excluded)
        if len(rows) > applied_limit:
            rows = rows[:applied_limit]
            truncation_reasons.append("limit")
        cost = self._cost(
            requested={"limit": requested_limit},
            applied={"limit": applied_limit},
            consumed={"returned": len(rows)},
            truncation_reasons=truncation_reasons,
        )
        total_candidates = len(candidate) if candidate else len(rows)
        coverage = self._coverage(
            total_candidates=total_candidates,
            considered_candidates=total_candidates,
            verified_candidates=len(rows),
            partial_reasons=cost.truncation_reasons,
        )
        return context_id, baseline_id, rows, cost, coverage

