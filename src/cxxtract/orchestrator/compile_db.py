"""Loader and query interface for compile_commands.json.

This module is the *Single Source of Truth* for compiler flags: every
flag used to invoke cpp-extractor must originate from the compilation
database.
"""

from __future__ import annotations

import json
import logging
import shlex
from pathlib import Path
from typing import Optional

from cxxtract.cache.hasher import compute_flags_hash

logger = logging.getLogger(__name__)


class CompileEntry:
    """Represents a single entry from compile_commands.json."""

    __slots__ = ("file", "directory", "arguments", "flags_hash", "repo_id", "file_key", "rel_path")

    def __init__(
        self,
        file: str,
        directory: str,
        arguments: list[str],
        *,
        repo_id: str = "",
        file_key: str = "",
        rel_path: str = "",
    ) -> None:
        self.file = file
        self.directory = directory
        self.arguments = arguments
        self.flags_hash = compute_flags_hash(arguments)
        self.repo_id = repo_id
        self.file_key = file_key
        self.rel_path = rel_path

    def __repr__(self) -> str:
        return (
            f"CompileEntry(file={self.file!r}, repo_id={self.repo_id!r}, "
            f"nflags={len(self.arguments)})"
        )


class CompilationDatabase:
    """In-memory index over a compile_commands.json file.

    Keys are **normalised absolute paths** so lookups are
    case-insensitive on Windows.
    """

    def __init__(self, entries: dict[str, CompileEntry]) -> None:
        self._entries = entries

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        repo_id: str = "",
        repo_root: Optional[str] = None,
    ) -> "CompilationDatabase":
        """Parse *path* and return a ready-to-query database.

        Parameters
        ----------
        path:
            Path to ``compile_commands.json``.

        Raises
        ------
        FileNotFoundError
            If *path* does not exist.
        ValueError
            If the JSON cannot be parsed or has an unexpected structure.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"compile_commands.json not found: {p}")

        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("compile_commands.json must be a JSON array")

        entries: dict[str, CompileEntry] = {}
        for item in raw:
            file_path = item.get("file", "")
            directory = item.get("directory", "")

            # Prefer "arguments" over "command"
            arguments: list[str]
            if "arguments" in item:
                arguments = list(item["arguments"])
            elif "command" in item:
                arguments = _split_command(item["command"])
            else:
                logger.warning("Entry with no arguments or command: %s", file_path)
                continue

            # Normalise to absolute path
            if not Path(file_path).is_absolute():
                file_path = str((Path(directory) / file_path).resolve())
            else:
                file_path = str(Path(file_path).resolve())

            # Strip the compiler executable (first arg) and the source file itself
            flags = _extract_flags(arguments, file_path)

            rel_path = ""
            if repo_root:
                try:
                    rel_path = str(Path(file_path).resolve().relative_to(Path(repo_root).resolve()))
                except ValueError:
                    rel_path = ""

            rel_posix = rel_path.replace("\\", "/")
            file_key = f"{repo_id}:{rel_posix}" if repo_id and rel_posix else ""

            entries[_normalise(file_path)] = CompileEntry(
                file=file_path,
                directory=directory,
                arguments=flags,
                repo_id=repo_id,
                file_key=file_key,
                rel_path=rel_posix,
            )

        logger.info("Loaded compilation database with %d entries from %s", len(entries), p)
        return cls(entries)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get(self, file_path: str | Path) -> Optional[CompileEntry]:
        """Look up compile flags for *file_path*."""
        key = _normalise(str(Path(file_path).resolve()))
        return self._entries.get(key)

    def has(self, file_path: str | Path) -> bool:
        """Check if *file_path* has compile flags in the database."""
        return self.get(file_path) is not None

    def all_files(self) -> list[str]:
        """Return all file paths present in the compilation database."""
        return [e.file for e in self._entries.values()]

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, file_path: str | Path) -> bool:
        return self.has(file_path)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _normalise(path: str) -> str:
    """Return a normalised, lower-case (Windows-safe) path string."""
    return str(Path(path).resolve()).lower()


def _split_command(command: str) -> list[str]:
    """Split a shell command string into a list of arguments."""
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        # Fallback for malformed commands
        return command.split()


def _extract_flags(arguments: list[str], source_file: str) -> list[str]:
    """Strip the compiler executable and source file from *arguments*.

    Returns only the flags that should be forwarded to Clang.
    """
    if not arguments:
        return []

    # First element is typically the compiler binary — skip it
    flags = arguments[1:]

    # Remove the source file itself from flags (it may appear as a positional arg)
    source_norm = _normalise(source_file)
    filtered: list[str] = []
    skip_next = False
    for i, flag in enumerate(flags):
        if skip_next:
            skip_next = False
            continue
        # Skip -o <output> pairs
        if flag in ("-o", "/Fo", "/Fe"):
            skip_next = True
            continue
        # Skip the source file itself
        if _normalise(flag) == source_norm:
            continue
        filtered.append(flag)

    return filtered
