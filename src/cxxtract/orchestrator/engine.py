"""Core orchestration engine — the five-stage semantic query pipeline.

Stage 1: Cache Check — lookup existing facts in SQLite.
Stage 2: Recall — blast ripgrep across the codebase to find candidate files.
Stage 3: Hash Diff — compare candidate file hashes to detect stale cache entries.
Stage 4: Parse — spawn cpp-extractor workers for cache-miss files.
Stage 5: Merge — combine cached + fresh facts and build confidence envelope.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from cxxtract.cache import repository as repo
from cxxtract.cache.hasher import (
    compute_composite_hash,
    compute_content_hash,
    compute_flags_hash,
    compute_includes_hash,
)
from cxxtract.config import Settings
from cxxtract.models import (
    CallEdgeResponse,
    CallGraphRequest,
    CallGraphResponse,
    CacheInvalidateRequest,
    CacheInvalidateResponse,
    ConfidenceEnvelope,
    DefinitionResponse,
    FileSymbolsRequest,
    FileSymbolsResponse,
    ReferenceLocation,
    ReferencesResponse,
    SymbolLocation,
    SymbolQueryRequest,
)
from cxxtract.orchestrator.compile_db import CompilationDatabase
from cxxtract.orchestrator.parser import parse_files_concurrent
from cxxtract.orchestrator.recall import run_recall

logger = logging.getLogger(__name__)


class OrchestratorEngine:
    """Executes the five-stage semantic query pipeline."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._compile_dbs: dict[str, CompilationDatabase] = {}

    # ------------------------------------------------------------------
    # CompileDB management
    # ------------------------------------------------------------------

    def get_compile_db(self, compile_commands_path: Optional[str]) -> Optional[CompilationDatabase]:
        """Load (and cache in-memory) a CompilationDatabase.

        Falls back to ``settings.default_compile_commands`` when no
        explicit path is given.
        """
        path = compile_commands_path or self._settings.default_compile_commands
        if not path:
            return None

        resolved = str(Path(path).resolve())
        if resolved not in self._compile_dbs:
            try:
                self._compile_dbs[resolved] = CompilationDatabase.load(resolved)
            except (FileNotFoundError, ValueError) as exc:
                logger.error("Failed to load compile_commands.json: %s", exc)
                return None
        return self._compile_dbs[resolved]

    def invalidate_compile_db(self, path: Optional[str] = None) -> None:
        """Drop the cached CompilationDatabase so it's reloaded on next use."""
        if path:
            resolved = str(Path(path).resolve())
            self._compile_dbs.pop(resolved, None)
        else:
            self._compile_dbs.clear()

    # ------------------------------------------------------------------
    # Pipeline helpers
    # ------------------------------------------------------------------

    async def _recall_candidates(
        self,
        symbol: str,
        repo_root: str,
        max_files: int,
    ) -> tuple[list[str], list[str]]:
        """Stage 2: Run ripgrep recall and return unique file paths.

        Returns
        -------
        tuple of (file_paths, warnings)
            file_paths: list of candidate file paths.
            warnings: list of diagnostic strings from recall (may be empty).
        """
        result = await run_recall(
            symbol,
            repo_root,
            rg_binary=self._settings.rg_binary,
            max_files=max_files,
            timeout_s=self._settings.recall_timeout_s,
        )

        warnings: list[str] = []
        if result.error:
            warnings.append(f"recall: {result.error}")
        if result.elapsed_ms > 5000:
            warnings.append(f"recall: slow query ({result.elapsed_ms:.0f}ms)")

        return [h.file_path for h in result.hits], warnings

    async def _classify_files(
        self,
        candidate_files: list[str],
        compile_db: Optional[CompilationDatabase],
    ) -> tuple[list[str], list[str], list[str]]:
        """Stage 3: Classify candidate files into verified / stale / unparsed.

        Returns
        -------
        tuple of (fresh_files, stale_files, unparsed_files)
            fresh_files: cached and composite hash still matches
            stale_files: cached but hash is outdated → need re-parse
            unparsed_files: no compile flags → cannot parse
        """
        fresh: list[str] = []
        stale: list[str] = []
        unparsed: list[str] = []

        for fp in candidate_files:
            # Check if we have compile flags
            if compile_db is None or not compile_db.has(fp):
                unparsed.append(fp)
                continue

            entry = compile_db.get(fp)
            assert entry is not None

            # Check cache
            cached_hash = await repo.get_composite_hash(fp)
            if cached_hash is None:
                stale.append(fp)
                continue

            # Recompute hash
            content_hash = compute_content_hash(fp)
            if not content_hash:
                stale.append(fp)
                continue

            # For includes, we use a simplified check: if the file content
            # hash + flags hash match, we consider it fresh.  Full transitive
            # include checking is done post-parse and stored.
            tracked = await repo.get_tracked_file(fp)
            if tracked:
                current_composite = compute_composite_hash(
                    content_hash, tracked["includes_hash"], entry.flags_hash
                )
            else:
                current_composite = compute_composite_hash(content_hash, "", entry.flags_hash)

            if current_composite == cached_hash:
                fresh.append(fp)
            else:
                stale.append(fp)

        return fresh, stale, unparsed

    async def _parse_stale_files(
        self,
        stale_files: list[str],
        compile_db: CompilationDatabase,
        max_workers: int,
    ) -> list[str]:
        """Stage 4: Parse stale files via cpp-extractor. Return successfully parsed file paths."""
        files_and_entries = []
        for fp in stale_files:
            entry = compile_db.get(fp)
            if entry:
                files_and_entries.append((fp, entry))

        if not files_and_entries:
            return []

        results = await parse_files_concurrent(
            files_and_entries,
            extractor_binary=self._settings.extractor_binary,
            max_workers=max_workers,
            timeout_s=self._settings.parse_timeout_s,
        )

        return [fp for fp, output in results.items() if output is not None]

    def _build_confidence(
        self,
        fresh_files: list[str],
        newly_parsed: list[str],
        stale_files: list[str],
        unparsed_files: list[str],
        failed_files: list[str],
        warnings: Optional[list[str]] = None,
    ) -> ConfidenceEnvelope:
        """Stage 5a: Build the confidence envelope."""
        verified = fresh_files + newly_parsed
        total = len(verified) + len(stale_files) + len(unparsed_files) + len(failed_files)
        ratio = len(verified) / total if total > 0 else 0.0
        return ConfidenceEnvelope(
            verified_files=verified,
            stale_files=stale_files + failed_files,
            unparsed_files=unparsed_files,
            total_candidates=total,
            verified_ratio=round(ratio, 4),
            warnings=warnings or [],
        )

    # ------------------------------------------------------------------
    # Public query entry points
    # ------------------------------------------------------------------

    async def query_references(self, request: SymbolQueryRequest) -> ReferencesResponse:
        """Find all references to a symbol."""
        max_files = request.max_recall_files or self._settings.max_recall_files
        max_workers = request.max_parse_workers or self._settings.max_parse_workers
        compile_db = self.get_compile_db(request.compile_commands)

        # Stage 2: Recall
        candidate_files, recall_warnings = await self._recall_candidates(
            request.symbol, request.repo_root, max_files
        )

        # Stage 3: Hash diff
        fresh, stale, unparsed = await self._classify_files(candidate_files, compile_db)

        # Stage 4: Parse stale files
        newly_parsed: list[str] = []
        failed: list[str] = []
        if stale and compile_db:
            newly_parsed = await self._parse_stale_files(stale, compile_db, max_workers)
            failed = [f for f in stale if f not in newly_parsed]

        # Stage 5: Merge results from cache
        # Look up definitions
        definitions = await repo.search_symbols_by_name(request.symbol)
        definition = None
        if definitions:
            d = definitions[0]
            definition = SymbolLocation(
                file=d["file_path"],
                line=d["line"],
                col=d["col"],
                kind=d["kind"],
                qualified_name=d["qualified_name"],
                extent_end_line=d.get("extent_end_line", 0),
            )

        # Look up references
        ref_rows = await repo.search_references_by_symbol(request.symbol)
        references = [
            ReferenceLocation(
                file=r["file_path"],
                line=r["line"],
                col=r["col"],
                kind=r["ref_kind"],
            )
            for r in ref_rows
        ]

        confidence = self._build_confidence(
            fresh, newly_parsed, [], unparsed, failed, warnings=recall_warnings,
        )

        return ReferencesResponse(
            symbol=request.symbol,
            definition=definition,
            references=references,
            confidence=confidence,
        )

    async def query_definition(self, request: SymbolQueryRequest) -> DefinitionResponse:
        """Find the definition(s) of a symbol."""
        max_files = request.max_recall_files or self._settings.max_recall_files
        max_workers = request.max_parse_workers or self._settings.max_parse_workers
        compile_db = self.get_compile_db(request.compile_commands)

        # Stage 2: Recall
        candidate_files, recall_warnings = await self._recall_candidates(
            request.symbol, request.repo_root, max_files
        )

        # Stage 3: Hash diff
        fresh, stale, unparsed = await self._classify_files(candidate_files, compile_db)

        # Stage 4: Parse stale
        newly_parsed: list[str] = []
        failed: list[str] = []
        if stale and compile_db:
            newly_parsed = await self._parse_stale_files(stale, compile_db, max_workers)
            failed = [f for f in stale if f not in newly_parsed]

        # Stage 5: Merge
        definitions = await repo.search_symbols_by_name(request.symbol)
        locations = [
            SymbolLocation(
                file=d["file_path"],
                line=d["line"],
                col=d["col"],
                kind=d["kind"],
                qualified_name=d["qualified_name"],
                extent_end_line=d.get("extent_end_line", 0),
            )
            for d in definitions
        ]

        confidence = self._build_confidence(
            fresh, newly_parsed, [], unparsed, failed, warnings=recall_warnings,
        )

        return DefinitionResponse(
            symbol=request.symbol,
            definitions=locations,
            confidence=confidence,
        )

    async def query_call_graph(self, request: CallGraphRequest) -> CallGraphResponse:
        """Get call graph edges for a function."""
        max_files = request.max_recall_files or self._settings.max_recall_files
        max_workers = request.max_parse_workers or self._settings.max_parse_workers
        compile_db = self.get_compile_db(request.compile_commands)

        # Stage 2: Recall
        candidate_files, recall_warnings = await self._recall_candidates(
            request.symbol, request.repo_root, max_files
        )

        # Stage 3: Hash diff
        fresh, stale, unparsed = await self._classify_files(candidate_files, compile_db)

        # Stage 4: Parse stale
        newly_parsed: list[str] = []
        failed: list[str] = []
        if stale and compile_db:
            newly_parsed = await self._parse_stale_files(stale, compile_db, max_workers)
            failed = [f for f in stale if f not in newly_parsed]

        # Stage 5: Merge call edges
        edges: list[CallEdgeResponse] = []

        if request.direction in ("outgoing", "both"):
            outgoing = await repo.get_call_edges_for_caller(request.symbol)
            edges.extend(
                CallEdgeResponse(
                    caller=e["caller_qualified_name"],
                    callee=e["callee_qualified_name"],
                    file=e["file_path"],
                    line=e["line"],
                )
                for e in outgoing
            )

        if request.direction in ("incoming", "both"):
            incoming = await repo.get_call_edges_for_callee(request.symbol)
            edges.extend(
                CallEdgeResponse(
                    caller=e["caller_qualified_name"],
                    callee=e["callee_qualified_name"],
                    file=e["file_path"],
                    line=e["line"],
                )
                for e in incoming
            )

        confidence = self._build_confidence(
            fresh, newly_parsed, [], unparsed, failed, warnings=recall_warnings,
        )

        return CallGraphResponse(
            symbol=request.symbol,
            edges=edges,
            confidence=confidence,
        )

    async def query_file_symbols(self, request: FileSymbolsRequest) -> FileSymbolsResponse:
        """List all symbols defined in a given file."""
        compile_db = self.get_compile_db(request.compile_commands)
        file_path = str(Path(request.file_path).resolve())

        # Check if parsing is needed
        cached_hash = await repo.get_composite_hash(file_path)
        needs_parse = cached_hash is None

        if not needs_parse:
            # Verify freshness
            content_hash = compute_content_hash(file_path)
            tracked = await repo.get_tracked_file(file_path)
            if tracked and compile_db:
                entry = compile_db.get(file_path)
                if entry:
                    current = compute_composite_hash(
                        content_hash, tracked["includes_hash"], entry.flags_hash
                    )
                    needs_parse = current != cached_hash

        unparsed: list[str] = []
        verified: list[str] = []
        stale: list[str] = []

        if needs_parse and compile_db:
            entry = compile_db.get(file_path)
            if entry:
                parsed = await self._parse_stale_files([file_path], compile_db, 1)
                if parsed:
                    verified = [file_path]
                else:
                    stale = [file_path]
            else:
                unparsed = [file_path]
        elif needs_parse:
            unparsed = [file_path]
        else:
            verified = [file_path]

        symbols_rows = await repo.get_symbols_by_file(file_path)
        symbols = [
            SymbolLocation(
                file=s["file_path"],
                line=s["line"],
                col=s["col"],
                kind=s["kind"],
                qualified_name=s["qualified_name"],
                extent_end_line=s.get("extent_end_line", 0),
            )
            for s in symbols_rows
        ]

        confidence = self._build_confidence(verified, [], stale, unparsed, [])

        return FileSymbolsResponse(
            file=file_path,
            symbols=symbols,
            confidence=confidence,
        )

    async def invalidate_cache(self, request: CacheInvalidateRequest) -> CacheInvalidateResponse:
        """Invalidate cached facts for specific files or the entire cache."""
        if request.file_paths is None:
            count = await repo.clear_all()
            self._compile_dbs.clear()
            return CacheInvalidateResponse(
                invalidated_files=count,
                message=f"Invalidated entire cache ({count} files)",
            )

        count = 0
        for fp in request.file_paths:
            resolved = str(Path(fp).resolve())
            tracked = await repo.get_tracked_file(resolved)
            if tracked:
                await repo.delete_tracked_file(resolved)
                count += 1

        return CacheInvalidateResponse(
            invalidated_files=count,
            message=f"Invalidated {count} of {len(request.file_paths)} requested files",
        )
