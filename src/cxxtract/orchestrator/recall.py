"""Ripgrep-based recall engine for fast heuristic candidate search.

This module wraps the ``rg`` binary as an async subprocess and parses
its JSON output into typed ``RecallHit`` / ``RecallResult`` objects.

Production features:
  - Structured ``RecallResult`` with error, exit code, and timing.
  - Multi-symbol batched queries via OR alternation.
  - Path normalisation (forward slashes) for cache-key consistency.
  - Cooperative cancellation via ``asyncio.Event``.
  - Context-line support for richer recall hits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path, PurePosixPath
from typing import Optional

from cxxtract.models import RecallHit, RecallResult

logger = logging.getLogger(__name__)

# Default C++ file extensions used for recall searches.
_DEFAULT_CPP_GLOBS = [
    "*.cpp", "*.cxx", "*.cc", "*.c",
    "*.h", "*.hpp", "*.hxx", "*.inl",
]


# ====================================================================
# Pattern builders
# ====================================================================

def build_symbol_pattern(symbol: str) -> str:
    r"""Convert a qualified C++ symbol name into a ripgrep regex pattern.

    Examples
    --------
    >>> build_symbol_pattern("Session::Auth")
    '\\bSession\\s*::\\s*Auth\\b'
    >>> build_symbol_pattern("doLogin")
    '\\bdoLogin\\b'
    """
    parts = [p.strip() for p in symbol.split("::") if p.strip()]
    escaped = [re.escape(part) for part in parts]
    pattern = r"\s*::\s*".join(escaped)
    return rf"\b{pattern}\b"


def build_multi_pattern(symbols: list[str]) -> str:
    r"""Build a single alternation regex for multiple symbols.

    This is more efficient than multiple rg invocations: the regex
    engine matches all symbols in a single pass.

    Examples
    --------
    >>> build_multi_pattern(["Session::Auth", "doLogin"])
    '(\\bSession\\s*::\\s*Auth\\b)|(\\bdoLogin\\b)'
    """
    sub_patterns = [f"({build_symbol_pattern(s)})" for s in symbols]
    return "|".join(sub_patterns)


# ====================================================================
# Path normalisation
# ====================================================================

def _normalise_path(path: str) -> str:
    """Normalise a file path to forward slashes for cache-key consistency.

    This matches the convention used by cpp-extractor output and the
    SQLite cache keys.
    """
    return path.replace("\\", "/")


# ====================================================================
# Core recall functions
# ====================================================================

async def run_recall(
    symbol: str,
    repo_root: str,
    *,
    rg_binary: str = "rg",
    max_files: int = 200,
    timeout_s: int = 30,
    file_globs: Optional[list[str]] = None,
    context_lines: int = 0,
    cancel_event: Optional[asyncio.Event] = None,
) -> RecallResult:
    """Execute ripgrep to find candidate files containing *symbol*.

    Parameters
    ----------
    symbol:
        Qualified or unqualified C++ symbol name (e.g. ``Session::Auth``).
    repo_root:
        Absolute path to the repository root to search.
    rg_binary:
        Path to the ripgrep binary.
    max_files:
        Maximum number of candidate *files* to return.
    timeout_s:
        Subprocess timeout in seconds.
    file_globs:
        Glob patterns to restrict the search.  Defaults to common C++
        extensions.
    context_lines:
        Number of context lines around each match (``rg -C``).  0 = none.
    cancel_event:
        Optional event for cooperative cancellation.

    Returns
    -------
    RecallResult
        Structured result with hits, error info, exit code, and timing.
    """
    pattern = build_symbol_pattern(symbol)
    return await _run_rg(
        pattern=pattern,
        repo_root=repo_root,
        rg_binary=rg_binary,
        max_files=max_files,
        timeout_s=timeout_s,
        file_globs=file_globs,
        context_lines=context_lines,
        cancel_event=cancel_event,
    )


async def run_recall_multi(
    symbols: list[str],
    repo_root: str,
    *,
    rg_binary: str = "rg",
    max_files: int = 200,
    timeout_s: int = 30,
    file_globs: Optional[list[str]] = None,
    context_lines: int = 0,
    cancel_event: Optional[asyncio.Event] = None,
) -> RecallResult:
    """Execute a single ripgrep invocation for multiple symbols.

    Uses OR alternation (``pattern1|pattern2|...``) so the regex engine
    matches all symbols in a single pass over the codebase.

    Parameters
    ----------
    symbols:
        List of qualified or unqualified C++ symbol names.
    repo_root:
        Absolute path to the repository root.
    rg_binary:
        Path to the ripgrep binary.
    max_files:
        Maximum number of candidate files.
    timeout_s:
        Subprocess timeout in seconds.
    file_globs:
        Glob patterns to restrict the search.
    context_lines:
        Context lines around each match.
    cancel_event:
        Optional event for cooperative cancellation.

    Returns
    -------
    RecallResult
        Combined result for all symbols.
    """
    if not symbols:
        return RecallResult(pattern="(empty)")

    if len(symbols) == 1:
        return await run_recall(
            symbols[0], repo_root,
            rg_binary=rg_binary, max_files=max_files,
            timeout_s=timeout_s, file_globs=file_globs,
            context_lines=context_lines, cancel_event=cancel_event,
        )

    pattern = build_multi_pattern(symbols)
    return await _run_rg(
        pattern=pattern,
        repo_root=repo_root,
        rg_binary=rg_binary,
        max_files=max_files,
        timeout_s=timeout_s,
        file_globs=file_globs,
        context_lines=context_lines,
        cancel_event=cancel_event,
    )


# ====================================================================
# Internal implementation
# ====================================================================

async def _run_rg(
    *,
    pattern: str,
    repo_root: str,
    rg_binary: str,
    max_files: int,
    timeout_s: int,
    file_globs: Optional[list[str]],
    context_lines: int,
    cancel_event: Optional[asyncio.Event],
) -> RecallResult:
    """Low-level ripgrep invocation and result parsing."""
    t0 = time.monotonic()

    if file_globs is None:
        file_globs = _DEFAULT_CPP_GLOBS

    cmd: list[str] = [
        rg_binary,
        "--json",
        "--no-heading",
        "--max-count", "5",
    ]

    # File type filtering — use one --type-add per glob because
    # ripgrep's comma-separated type-add syntax is unreliable.
    for g in file_globs:
        glob = f"*{g.lstrip('*')}"  # normalise: "*.cpp" -> "*.cpp"
        cmd.extend(["--type-add", f"cxx:{glob}"])
    cmd.extend(["--type", "cxx"])

    # Context lines
    if context_lines > 0:
        cmd.extend(["-C", str(context_lines)])

    cmd.extend(["--", pattern, repo_root])

    logger.debug("Recall command: %s", " ".join(cmd))

    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repo_root,
        )

        # Wait with cancellation support
        if cancel_event is not None:
            stdout_bytes, stderr_bytes = await _communicate_with_cancel(
                proc, timeout_s, cancel_event
            )
        else:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )

    except asyncio.TimeoutError:
        elapsed = (time.monotonic() - t0) * 1000
        logger.warning("ripgrep timed out after %ds for pattern '%s'", timeout_s, pattern[:100])
        _kill_proc(proc)
        return RecallResult(
            error=f"ripgrep timed out after {timeout_s}s",
            elapsed_ms=elapsed,
            pattern=pattern[:200],
        )

    except asyncio.CancelledError:
        elapsed = (time.monotonic() - t0) * 1000
        logger.info("Recall cancelled for pattern '%s'", pattern[:100])
        _kill_proc(proc)
        return RecallResult(
            error="cancelled",
            elapsed_ms=elapsed,
            pattern=pattern[:200],
        )

    except FileNotFoundError:
        elapsed = (time.monotonic() - t0) * 1000
        logger.error("ripgrep binary not found: %s", rg_binary)
        return RecallResult(
            error=f"ripgrep binary not found: {rg_binary}",
            elapsed_ms=elapsed,
            pattern=pattern[:200],
        )

    except OSError as exc:
        elapsed = (time.monotonic() - t0) * 1000
        logger.error("OS error spawning ripgrep: %s", exc)
        return RecallResult(
            error=f"OS error: {exc}",
            elapsed_ms=elapsed,
            pattern=pattern[:200],
        )

    elapsed = (time.monotonic() - t0) * 1000
    exit_code = proc.returncode if proc else None

    # rg exit code 1 = no matches (normal), 2 = error
    if exit_code is not None and exit_code not in (0, 1):
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        logger.warning("ripgrep exited with code %d: %s", exit_code, stderr_text[:500])
        return RecallResult(
            error=f"ripgrep exited with code {exit_code}: {stderr_text[:300]}",
            rg_exit_code=exit_code,
            elapsed_ms=elapsed,
            pattern=pattern[:200],
        )

    hits = _parse_rg_json(stdout_bytes.decode("utf-8", errors="replace"))
    deduped = _deduplicate_hits(hits, max_files)

    logger.debug(
        "Recall completed: %d raw hits -> %d unique files in %.0fms",
        len(hits), len(deduped), elapsed,
    )

    return RecallResult(
        hits=deduped,
        rg_exit_code=exit_code,
        elapsed_ms=round(elapsed, 1),
        pattern=pattern[:200],
    )


async def _communicate_with_cancel(
    proc: asyncio.subprocess.Process,
    timeout_s: int,
    cancel_event: asyncio.Event,
) -> tuple[bytes, bytes]:
    """Wait for process with both timeout and cooperative cancellation."""
    comm_task = asyncio.create_task(proc.communicate())
    cancel_task = asyncio.create_task(cancel_event.wait())

    done, pending = await asyncio.wait(
        {comm_task, cancel_task},
        timeout=timeout_s,
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Clean up pending tasks
    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    if comm_task in done:
        return comm_task.result()

    if cancel_task in done:
        # Cancellation was requested
        _kill_proc(proc)
        raise asyncio.CancelledError("Recall cancelled by caller")

    # Timeout
    _kill_proc(proc)
    raise asyncio.TimeoutError()


def _kill_proc(proc: Optional[asyncio.subprocess.Process]) -> None:
    """Safely kill a subprocess."""
    if proc is None:
        return
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


# ====================================================================
# JSON parsing
# ====================================================================

def _parse_rg_json(output: str) -> list[RecallHit]:
    """Parse ripgrep's ``--json`` output into RecallHit objects."""
    hits: list[RecallHit] = []

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if msg.get("type") != "match":
            continue

        data = msg.get("data", {})
        path_info = data.get("path", {})
        file_path = path_info.get("text", "")

        line_number = data.get("line_number", 0)
        lines_info = data.get("lines", {})
        line_text = lines_info.get("text", "").rstrip("\n")

        if file_path:
            hits.append(RecallHit(
                file_path=_normalise_path(file_path),
                line_number=line_number,
                line_text=line_text,
            ))

    return hits


# ====================================================================
# Deduplication
# ====================================================================

def _deduplicate_hits(hits: list[RecallHit], max_files: int) -> list[RecallHit]:
    """Keep only the first hit per unique file, capped at *max_files*.

    Paths are resolved and normalised for consistent deduplication.
    """
    seen: set[str] = set()
    result: list[RecallHit] = []

    for hit in hits:
        try:
            normalized = _normalise_path(str(Path(hit.file_path).resolve()))
        except (OSError, ValueError):
            normalized = _normalise_path(hit.file_path)

        if normalized not in seen:
            seen.add(normalized)
            # Store the normalised path back into the hit
            result.append(RecallHit(
                file_path=normalized,
                line_number=hit.line_number,
                line_text=hit.line_text,
            ))
            if len(result) >= max_files:
                break

    return result
