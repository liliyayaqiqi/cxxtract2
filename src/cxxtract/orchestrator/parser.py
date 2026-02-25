"""Async subprocess pool for invoking cpp-extractor.exe.

Each invocation parses a single translation unit and emits structured
AST facts as JSON on stdout.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from cxxtract.cache import repository as repo
from cxxtract.cache.hasher import (
    compute_composite_hash,
    compute_content_hash,
    compute_flags_hash,
    compute_includes_hash,
)
from cxxtract.models import ExtractorOutput
from cxxtract.orchestrator.compile_db import CompileEntry

logger = logging.getLogger(__name__)


async def parse_file(
    file_path: str,
    entry: CompileEntry,
    *,
    extractor_binary: str,
    timeout_s: int = 120,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> Optional[ExtractorOutput]:
    """Run cpp-extractor on a single file and return parsed facts.

    Parameters
    ----------
    file_path:
        Absolute path to the source file.
    entry:
        The compile_commands.json entry with build flags.
    extractor_binary:
        Path to the cpp-extractor executable.
    timeout_s:
        Maximum time in seconds for the subprocess.
    semaphore:
        Optional asyncio.Semaphore to limit concurrency.

    Returns
    -------
    ExtractorOutput | None
        Parsed facts, or *None* on failure.
    """
    if semaphore:
        await semaphore.acquire()

    started_at = datetime.now(timezone.utc).isoformat()
    run_id: Optional[int] = None

    try:
        run_id = await repo.insert_parse_run(file_path, started_at)

        cmd = [
            extractor_binary,
            "--action", "extract-all",
            "--file", file_path,
            "--",
            *entry.arguments,
        ]

        logger.debug("Spawning extractor: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=entry.directory or None,
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )

        if proc.returncode != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            logger.warning(
                "cpp-extractor failed for %s (exit %d): %s",
                file_path, proc.returncode, stderr_text[:500],
            )
            if run_id is not None:
                await repo.finish_parse_run(run_id, success=False, error_msg=stderr_text[:1000])
            return None

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        output = _parse_extractor_json(stdout_text, file_path)

        if output is None:
            if run_id is not None:
                await repo.finish_parse_run(run_id, success=False, error_msg="Invalid JSON output")
            return None

        # Compute hashes and persist
        content_hash = compute_content_hash(file_path)
        flags_hash = entry.flags_hash

        # Compute includes hash from the reported include deps
        include_hashes = [compute_content_hash(d.path) for d in output.include_deps]
        includes_hash = compute_includes_hash(include_hashes)
        composite_hash = compute_composite_hash(content_hash, includes_hash, flags_hash)

        await repo.upsert_extractor_output(
            output,
            content_hash=content_hash,
            flags_hash=flags_hash,
            includes_hash=includes_hash,
            composite_hash=composite_hash,
        )

        if run_id is not None:
            await repo.finish_parse_run(run_id, success=True)

        logger.info(
            "Parsed %s: %d symbols, %d refs, %d call edges",
            file_path, len(output.symbols), len(output.references), len(output.call_edges),
        )
        return output

    except asyncio.TimeoutError:
        logger.warning("cpp-extractor timed out after %ds for %s", timeout_s, file_path)
        try:
            proc.kill()  # type: ignore[union-attr]
        except (ProcessLookupError, NameError):
            pass
        if run_id is not None:
            await repo.finish_parse_run(run_id, success=False, error_msg=f"Timeout after {timeout_s}s")
        return None

    except FileNotFoundError:
        logger.error("cpp-extractor binary not found: %s", extractor_binary)
        if run_id is not None:
            await repo.finish_parse_run(run_id, success=False, error_msg="Binary not found")
        return None

    except Exception as exc:
        logger.exception("Unexpected error parsing %s", file_path)
        if run_id is not None:
            await repo.finish_parse_run(run_id, success=False, error_msg=str(exc)[:1000])
        return None

    finally:
        if semaphore:
            semaphore.release()


async def parse_files_concurrent(
    files_and_entries: list[tuple[str, CompileEntry]],
    *,
    extractor_binary: str,
    max_workers: int = 4,
    timeout_s: int = 120,
) -> dict[str, Optional[ExtractorOutput]]:
    """Parse multiple files concurrently with bounded parallelism.

    Parameters
    ----------
    files_and_entries:
        List of (file_path, CompileEntry) pairs to parse.
    extractor_binary:
        Path to the cpp-extractor executable.
    max_workers:
        Maximum number of concurrent extractor subprocesses.
    timeout_s:
        Per-file timeout in seconds.

    Returns
    -------
    dict[str, ExtractorOutput | None]
        Mapping from file_path to its extraction result (None on failure).
    """
    semaphore = asyncio.Semaphore(max_workers)

    tasks = [
        parse_file(
            file_path,
            entry,
            extractor_binary=extractor_binary,
            timeout_s=timeout_s,
            semaphore=semaphore,
        )
        for file_path, entry in files_and_entries
    ]

    results = await asyncio.gather(*tasks, return_exceptions=False)

    return {
        file_path: result
        for (file_path, _), result in zip(files_and_entries, results)
    }


def _parse_extractor_json(
    raw: str,
    file_path: str,
) -> Optional[ExtractorOutput]:
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
