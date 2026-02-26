"""Freshness classification and parse scheduling service."""

from __future__ import annotations

from typing import Protocol

from cxxtract.cache import repository as repo
from cxxtract.cache.hasher import compute_composite_hash, compute_content_hash
from cxxtract.config import Settings
from cxxtract.models import CompileMatchType
from cxxtract.orchestrator.compile_db import CompilationDatabase, CompileEntry
from cxxtract.orchestrator.parser import ParseTask, parse_files_concurrent
from cxxtract.orchestrator.workspace import WorkspaceManifest, file_key_to_abs_path


class PayloadWriter(Protocol):
    queue_depth: int
    lag_ms: float

    async def enqueue(self, payload) -> None: ...

    async def flush(self) -> None: ...


class FreshnessService:
    """Classifies file freshness and performs parse execution."""

    def __init__(self, settings: Settings, writer: PayloadWriter) -> None:
        self._settings = settings
        self._writer = writer

    async def classify(
        self,
        context_id: str,
        file_keys: list[str],
        compile_dbs: dict[str, CompilationDatabase | None],
        workspace_root: str,
        manifest: WorkspaceManifest,
    ) -> tuple[list[str], list[str], list[str], list[tuple[ParseTask, CompileEntry]]]:
        fresh, stale, unparsed, tasks, _task_meta, _warnings = await self.classify_detailed(
            context_id,
            file_keys,
            compile_dbs,
            workspace_root,
            manifest,
        )
        return fresh, stale, unparsed, tasks

    async def classify_detailed(
        self,
        context_id: str,
        file_keys: list[str],
        compile_dbs: dict[str, CompilationDatabase | None],
        workspace_root: str,
        manifest: WorkspaceManifest,
    ) -> tuple[
        list[str],
        list[str],
        list[str],
        list[tuple[ParseTask, CompileEntry]],
        dict[str, tuple[str, CompileMatchType, str]],
        list[str],
    ]:
        fresh: list[str] = []
        stale: list[str] = []
        unparsed: list[str] = []
        tasks: list[tuple[ParseTask, CompileEntry]] = []
        task_meta: dict[str, tuple[str, CompileMatchType, str]] = {}
        warnings: list[str] = []

        for file_key in file_keys:
            resolved = file_key_to_abs_path(workspace_root, manifest, file_key)
            if resolved is None:
                unparsed.append(file_key)
                warnings.append(f"{file_key}:invalid_file_key")
                continue

            repo_id, rel_path, abs_path = resolved
            cdb = compile_dbs.get(repo_id)
            if cdb is None:
                unparsed.append(file_key)
                warnings.append(f"{file_key}:missing_compile_db")
                continue

            compile_match_type = CompileMatchType.EXACT
            entry = cdb.get(abs_path)
            if entry is None:
                entry = cdb.fallback_entry(abs_path)
                compile_match_type = CompileMatchType.FALLBACK
            if entry is None:
                unparsed.append(file_key)
                warnings.append(f"{file_key}:missing_compile_entry")
                continue

            cached_hash = await repo.get_composite_hash(context_id, file_key)
            if cached_hash is None:
                stale.append(file_key)
                tasks.append((ParseTask(context_id, file_key, repo_id, rel_path, abs_path), entry))
                task_meta[file_key] = (repo_id, compile_match_type, entry.flags_hash)
                continue

            tracked = await repo.get_tracked_file(context_id, file_key)
            current_hash = compute_composite_hash(
                compute_content_hash(abs_path),
                tracked["includes_hash"] if tracked else "",
                entry.flags_hash,
            )
            if current_hash == cached_hash:
                fresh.append(file_key)
            else:
                stale.append(file_key)
                tasks.append((ParseTask(context_id, file_key, repo_id, rel_path, abs_path), entry))
                task_meta[file_key] = (repo_id, compile_match_type, entry.flags_hash)

        return fresh, stale, unparsed, tasks, task_meta, sorted(set(warnings))

    async def parse(
        self,
        tasks: list[tuple[ParseTask, CompileEntry]],
        workspace_root: str,
        manifest: WorkspaceManifest,
        workers: int,
    ) -> tuple[list[str], list[str], list[str]]:
        parsed, failed, warnings, _rows = await self.parse_detailed(
            tasks,
            workspace_root,
            manifest,
            workers,
            timeout_s=self._settings.parse_timeout_s,
        )
        return parsed, failed, warnings

    async def parse_detailed(
        self,
        tasks: list[tuple[ParseTask, CompileEntry]],
        workspace_root: str,
        manifest: WorkspaceManifest,
        workers: int,
        *,
        timeout_s: int,
    ) -> tuple[list[str], list[str], list[str], int]:
        if not tasks:
            return [], [], [], 0

        results = await parse_files_concurrent(
            tasks,
            extractor_binary=self._settings.extractor_binary,
            workspace_root=workspace_root,
            manifest=manifest,
            max_workers=workers,
            timeout_s=timeout_s,
        )

        parsed: list[str] = []
        failed: list[str] = []
        warnings: list[str] = []
        persisted_fact_rows = 0
        for file_key, payload in results.items():
            if payload is None:
                failed.append(file_key)
                continue
            parsed.append(file_key)
            warnings.extend(payload.warnings)
            persisted_fact_rows += (
                len(payload.output.symbols)
                + len(payload.output.references)
                + len(payload.output.call_edges)
                + len(payload.resolved_include_deps)
            )
            await self._writer.enqueue(payload)

        await self._writer.flush()
        return parsed, failed, warnings, persisted_fact_rows
