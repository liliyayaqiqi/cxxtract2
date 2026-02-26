"""Async parser worker pool for invoking cpp-extractor.exe."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cxxtract.cache.hasher import compute_composite_hash, compute_content_hash, compute_includes_hash
from cxxtract.models import ExtractorOutput, ParsePayload, ResolvedIncludeDep
from cxxtract.orchestrator.compile_db import CompileEntry
from cxxtract.orchestrator.workspace import WorkspaceManifest, resolve_include_dep

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ParseTask:
    """A single parse unit describing canonical workspace identity."""

    context_id: str
    file_key: str
    repo_id: str
    rel_path: str
    abs_path: str


def _build_vfs_overlay_file(workspace_root: str, manifest: WorkspaceManifest) -> str:
    """Build a best-effort VFS overlay file for include path remapping."""
    roots = []
    workspace_root_p = Path(workspace_root).resolve()
    for remap in manifest.path_remaps:
        mapped_dir = (workspace_root_p / remap.to_prefix).resolve()
        roots.append(
            {
                "name": remap.from_prefix.replace("\\", "/"),
                "type": "directory",
                "external-contents": str(mapped_dir).replace("\\", "/"),
            }
        )
    if not roots:
        return ""

    payload = {
        "version": 0,
        "case-sensitive": "false",
        "roots": roots,
    }
    fh = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False)
    with fh:
        json.dump(payload, fh)
    return fh.name


def _parse_extractor_json(raw: str, file_path: str) -> Optional[ExtractorOutput]:
    """Parse JSON output from cpp-extractor into an ExtractorOutput model."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse JSON from extractor for %s: %s", file_path, exc)
        return None

    if not isinstance(data, dict):
        logger.error("Extractor output for %s is not a JSON object", file_path)
        return None

    try:
        return ExtractorOutput.model_validate(data)
    except Exception as exc:
        logger.error("Extractor output validation failed for %s: %s", file_path, exc)
        return None


async def parse_file(
    task: ParseTask,
    entry: CompileEntry,
    *,
    extractor_binary: str,
    workspace_root: str,
    manifest: WorkspaceManifest,
    timeout_s: int = 120,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> Optional[ParsePayload]:
    """Run cpp-extractor on a single file and return a parse payload."""
    if semaphore:
        await semaphore.acquire()

    proc: Optional[asyncio.subprocess.Process] = None
    overlay_file = ""
    try:
        cmd = [
            extractor_binary,
            "--action",
            "extract-all",
            "--file",
            task.abs_path,
        ]

        overlay_file = _build_vfs_overlay_file(workspace_root, manifest)
        args = list(entry.arguments)
        if overlay_file:
            args = ["-ivfsoverlay", overlay_file, *args]
        cmd.extend(["--", *args])

        logger.debug("Spawning extractor: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=entry.directory or None,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)

        if proc.returncode != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            logger.warning(
                "cpp-extractor failed for %s (exit %d): %s",
                task.abs_path,
                proc.returncode,
                stderr_text[:500],
            )
            return None

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        output = _parse_extractor_json(stdout_text, task.abs_path)
        if output is None:
            return None

        content_hash = compute_content_hash(task.abs_path)
        flags_hash = entry.flags_hash

        resolved_deps: list[ResolvedIncludeDep] = []
        include_hashes: list[str] = []
        warnings: list[str] = []

        for dep in output.include_deps:
            resolved = resolve_include_dep(workspace_root, manifest, dep.path, dep.depth)
            resolved_deps.append(resolved)
            source_path = resolved.resolved_abs_path if resolved.resolved and resolved.resolved_abs_path else dep.path
            include_hashes.append(compute_content_hash(source_path))

        if any(not d.resolved for d in resolved_deps):
            warnings.append("external_unresolved_include")

        includes_hash = compute_includes_hash(include_hashes)
        composite_hash = compute_composite_hash(content_hash, includes_hash, flags_hash)

        return ParsePayload(
            context_id=task.context_id,
            file_key=task.file_key,
            repo_id=task.repo_id,
            rel_path=task.rel_path,
            abs_path=task.abs_path,
            output=output,
            resolved_include_deps=resolved_deps,
            content_hash=content_hash,
            flags_hash=flags_hash,
            includes_hash=includes_hash,
            composite_hash=composite_hash,
            warnings=warnings,
        )

    except asyncio.TimeoutError:
        logger.warning("cpp-extractor timed out after %ss for %s", timeout_s, task.abs_path)
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return None
    except FileNotFoundError:
        logger.error("cpp-extractor binary not found: %s", extractor_binary)
        return None
    except Exception:
        logger.exception("Unexpected parser failure for %s", task.abs_path)
        return None
    finally:
        if overlay_file:
            try:
                Path(overlay_file).unlink(missing_ok=True)
            except OSError:
                pass
        if semaphore:
            semaphore.release()


async def parse_files_concurrent(
    tasks_and_entries: list[tuple[ParseTask, CompileEntry]],
    *,
    extractor_binary: str,
    workspace_root: str,
    manifest: WorkspaceManifest,
    max_workers: int = 4,
    timeout_s: int = 120,
) -> dict[str, Optional[ParsePayload]]:
    """Parse multiple files concurrently with bounded parallelism."""
    if not tasks_and_entries:
        return {}

    semaphore = asyncio.Semaphore(max_workers)
    jobs = [
        parse_file(
            task,
            entry,
            extractor_binary=extractor_binary,
            workspace_root=workspace_root,
            manifest=manifest,
            timeout_s=timeout_s,
            semaphore=semaphore,
        )
        for task, entry in tasks_and_entries
    ]
    results = await asyncio.gather(*jobs, return_exceptions=False)
    return {task.file_key: payload for (task, _), payload in zip(tasks_and_entries, results)}
