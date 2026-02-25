"""Ripgrep-based recall engine for fast heuristic candidate search.

This module wraps the ``rg`` binary as an async subprocess and parses
its JSON output into typed ``RecallHit`` objects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Optional

from cxxtract.models import RecallHit

logger = logging.getLogger(__name__)


def build_symbol_pattern(symbol: str) -> str:
    r"""Convert a qualified C++ symbol name into a ripgrep regex pattern.

    Examples
    --------
    >>> build_symbol_pattern("Session::Auth")
    '\\bSession\\s*::\\s*Auth\\b'
    >>> build_symbol_pattern("doLogin")
    '\\bdoLogin\\b'
    """
    # Split on `::` and rebuild with optional whitespace around `::`
    parts = [p.strip() for p in symbol.split("::") if p.strip()]
    escaped = [re.escape(part) for part in parts]
    pattern = r"\s*::\s*".join(escaped)
    return rf"\b{pattern}\b"


async def run_recall(
    symbol: str,
    repo_root: str,
    *,
    rg_binary: str = "rg",
    max_files: int = 200,
    timeout_s: int = 30,
    glob_include: Optional[list[str]] = None,
) -> list[RecallHit]:
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
        Maximum number of candidate *files* to return.  We use ``rg``'s
        ``--max-count`` per-file and then limit the overall result set.
    timeout_s:
        Subprocess timeout in seconds.
    glob_include:
        Optional glob patterns to restrict the search (e.g. ``["*.cpp", "*.h"]``).
        Defaults to common C++ extensions.

    Returns
    -------
    list[RecallHit]
        Deduplicated candidate hits, capped at *max_files* unique files.
    """
    if glob_include is None:
        glob_include = ["*.cpp", "*.cxx", "*.cc", "*.c", "*.h", "*.hpp", "*.hxx", "*.inl"]

    pattern = build_symbol_pattern(symbol)

    cmd: list[str] = [
        rg_binary,
        "--json",              # Machine-readable JSON lines
        "--no-heading",
        "--max-count", "5",    # At most 5 matches per file (enough for recall)
        "--type-add", "cpp:*.cpp,*.cxx,*.cc,*.c,*.h,*.hpp,*.hxx,*.inl",
    ]

    for g in glob_include:
        cmd.extend(["--glob", g])

    cmd.extend(["--", pattern, repo_root])

    logger.debug("Recall command: %s", " ".join(cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repo_root,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        logger.warning("ripgrep timed out after %ds for symbol '%s'", timeout_s, symbol)
        try:
            proc.kill()  # type: ignore[union-attr]
        except ProcessLookupError:
            pass
        return []
    except FileNotFoundError:
        logger.error("ripgrep binary not found: %s", rg_binary)
        return []

    # rg returns exit code 1 when no matches found — that's normal
    if proc.returncode not in (0, 1):
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        logger.warning("ripgrep exited with code %d: %s", proc.returncode, stderr_text)
        return []

    hits = _parse_rg_json(stdout_bytes.decode("utf-8", errors="replace"))
    return _deduplicate_hits(hits, max_files)


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

        # Each match may have sub-matches; take the first line info
        line_number = data.get("line_number", 0)
        lines_info = data.get("lines", {})
        line_text = lines_info.get("text", "").rstrip("\n")

        if file_path:
            hits.append(RecallHit(
                file_path=file_path,
                line_number=line_number,
                line_text=line_text,
            ))

    return hits


def _deduplicate_hits(hits: list[RecallHit], max_files: int) -> list[RecallHit]:
    """Keep only the first hit per unique file, capped at *max_files*."""
    seen: set[str] = set()
    result: list[RecallHit] = []

    for hit in hits:
        normalized = str(Path(hit.file_path).resolve())
        if normalized not in seen:
            seen.add(normalized)
            result.append(hit)
            if len(result) >= max_files:
                break

    return result
