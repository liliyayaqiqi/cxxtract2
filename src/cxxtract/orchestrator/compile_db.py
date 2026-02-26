"""Loader and query interface for compile_commands.json.

This module is the *Single Source of Truth* for compiler flags: every
flag used to invoke cpp-extractor must originate from the compilation
database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
from pathlib import Path, PurePosixPath
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
        self._fallback_cache: dict[str, Optional[CompileEntry]] = {}

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
            flags = _extract_flags(arguments, file_path, directory)

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

    def fallback_entry(self, file_path: str | Path) -> Optional[CompileEntry]:
        """Return a best-effort compile entry for files not directly in the DB.

        This is primarily for headers or generated files that are not explicit
        translation units in ``compile_commands.json``.
        """
        key = _normalise(str(Path(file_path).resolve()))
        if key in self._fallback_cache:
            return self._fallback_cache[key]

        exact = self.get(file_path)
        if exact is not None:
            self._fallback_cache[key] = exact
            return exact

        target = Path(file_path).resolve()
        target_parts = _path_parts(key)
        target_stem = target.stem.lower()
        target_suffix = target.suffix.lower()
        source_exts = {".c", ".cc", ".cpp", ".cxx", ".m", ".mm"}
        header_exts = {".h", ".hh", ".hpp", ".hxx", ".inc", ".ipp", ".tpp"}

        best: Optional[CompileEntry] = None
        best_rank: tuple[int, str] | None = None

        for entry in self._entries.values():
            entry_key = _normalise(entry.file)
            entry_path = Path(entry.file).resolve()
            entry_parts = _path_parts(entry_key)

            common = _common_prefix_len(target_parts, entry_parts)
            distance = (len(target_parts) - common) + (len(entry_parts) - common)

            same_dir = int(target.parent == entry_path.parent)
            same_stem = int(target_stem and entry_path.stem.lower() == target_stem)
            same_suffix = int(target_suffix and entry_path.suffix.lower() == target_suffix)
            source_like = int(entry_path.suffix.lower() in source_exts)
            header_bonus = int(target_suffix in header_exts and same_stem and source_like) * 20

            score = (
                common * 10
                + same_dir * 8
                + same_stem * 6
                + source_like * 2
                + same_suffix
                + header_bonus
                - distance
            )
            rank = (score, entry_key)
            if best_rank is None or rank > best_rank:
                best = entry
                best_rank = rank

        self._fallback_cache[key] = best
        return best

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


def _path_parts(normalised_path: str) -> tuple[str, ...]:
    return tuple(part for part in normalised_path.replace("\\", "/").split("/") if part)


def _common_prefix_len(lhs: tuple[str, ...], rhs: tuple[str, ...]) -> int:
    count = 0
    for l, r in zip(lhs, rhs):
        if l != r:
            break
        count += 1
    return count


def _split_command(command: str) -> list[str]:
    """Split a shell command string into a list of arguments."""
    if os.name == "nt":
        win = _split_command_windows(command)
        if win:
            return win
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        # Fallback for malformed commands
        return command.split()


def _extract_flags(arguments: list[str], source_file: str, directory: str = "") -> list[str]:
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
    source_rel_norm = ""
    if directory:
        try:
            source_rel_norm = _normalise(str(Path(source_file).resolve().relative_to(Path(directory).resolve())))
        except ValueError:
            source_rel_norm = ""

    for i, flag in enumerate(flags):
        if skip_next:
            skip_next = False
            continue
        # Skip -o <output> pairs
        if flag in ("-o", "/Fo", "/Fe", "-c", "/c"):
            skip_next = True
            continue
        # Skip the source file itself
        if _looks_like_path(flag):
            flag_norm = _normalise(flag)
            if flag_norm == source_norm or (source_rel_norm and flag_norm == source_rel_norm):
                continue
        filtered.append(flag)

    return filtered


def rewrite_compile_commands_to_cache(
    source_path: str | Path,
    *,
    repo_root: str | Path,
    cache_dir: str | Path,
) -> str:
    """Create a rewritten compile_commands cache file mapped to *repo_root*.

    The original compile database is never modified.
    """
    source = Path(source_path).resolve()
    root = Path(repo_root).resolve()
    target_dir = Path(cache_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    payload_bytes = source.read_bytes()
    digest = hashlib.sha256(payload_bytes + str(root).encode("utf-8")).hexdigest()[:16]
    target = target_dir / f"{source.stem}.{digest}.rewritten.json"
    if target.exists():
        return str(target)

    raw = json.loads(payload_bytes.decode("utf-8"))
    if not isinstance(raw, list):
        raise ValueError("compile_commands.json must be a JSON array")

    rewritten: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        raw_file = str(item.get("file", "")).strip()
        raw_dir = str(item.get("directory", "")).strip()
        if not raw_file:
            continue

        old_abs_file = _resolve_file_path(raw_file, raw_dir)
        new_abs_file = _map_into_repo_root(raw_file, raw_dir, root, old_abs_file)
        if not new_abs_file:
            # Skip entries that cannot be mapped into this workspace repo root.
            continue

        mapped_dir = _map_directory_to_repo(raw_dir, root)
        legacy_roots = _derive_legacy_roots(old_abs_file, new_abs_file)
        arguments = _raw_item_arguments(item)
        rewritten_args = _rewrite_arguments(
            arguments,
            raw_file,
            raw_dir,
            old_abs_file,
            root,
            new_abs_file,
            legacy_roots,
        )

        rewritten.append(
            {
                "directory": mapped_dir,
                "file": new_abs_file,
                "arguments": rewritten_args,
            }
        )

    target.write_text(json.dumps(rewritten, ensure_ascii=False), encoding="utf-8")
    logger.info("Rewrote compile_commands cache: %s -> %s (%d entries)", source, target, len(rewritten))
    return str(target)


def _split_command_windows(command: str) -> list[str]:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []

    argc = ctypes.c_int(0)
    fn = ctypes.windll.shell32.CommandLineToArgvW
    fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    fn.restype = ctypes.POINTER(wintypes.LPWSTR)
    argv = fn(command, ctypes.byref(argc))
    if not argv:
        return []

    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def _raw_item_arguments(item: dict) -> list[str]:
    if "arguments" in item and isinstance(item["arguments"], list):
        return [str(x) for x in item["arguments"]]
    if "command" in item:
        return _split_command(str(item["command"]))
    return []


def _looks_like_path(value: str) -> bool:
    v = value.strip().strip('"').strip("'")
    if not v:
        return False
    if v.startswith(("/", "\\")):
        return True
    if len(v) >= 3 and v[1] == ":" and v[0].isalpha() and v[2] in {"\\", "/"}:
        return True
    if any(sep in v for sep in ("/", "\\")):
        return True
    return False


def _resolve_file_path(raw_file: str, raw_dir: str) -> str:
    fp = Path(raw_file)
    if fp.is_absolute():
        return str(fp.resolve())
    if raw_dir:
        return str((Path(raw_dir) / fp).resolve())
    return str(fp.resolve())


def _clean_path_parts(path_text: str) -> list[str]:
    parts = [p for p in path_text.replace("\\", "/").split("/") if p]
    out: list[str] = []
    for idx, part in enumerate(parts):
        if idx == 0 and part.endswith(":"):
            continue
        out.append(part)
    return out


def _map_into_repo_root(raw_file: str, raw_dir: str, repo_root: Path, old_abs_file: str) -> str:
    # 1) If already under repo root, keep.
    try:
        old_abs = Path(old_abs_file).resolve()
        old_abs.relative_to(repo_root)
        return str(old_abs)
    except ValueError:
        pass

    # 2) Relative file path often points to repo-relative source file.
    rel_parts = [p for p in PurePosixPath(raw_file.replace("\\", "/")).parts if p not in (".", "..")]
    if rel_parts:
        rel_candidate = (repo_root / Path(*rel_parts)).resolve()
        if rel_candidate.exists():
            return str(rel_candidate)

    # 3) Suffix matching against old absolute path.
    parts = _clean_path_parts(old_abs_file)
    for idx in range(len(parts)):
        suffix = parts[idx:]
        if len(suffix) < 2:
            continue
        candidate = (repo_root / Path(*suffix)).resolve()
        if candidate.exists():
            return str(candidate)
    return ""


def _map_directory_to_repo(raw_dir: str, repo_root: Path) -> str:
    if not raw_dir:
        return str(repo_root)
    dir_path = Path(raw_dir)
    if dir_path.exists():
        try:
            dir_path.resolve().relative_to(repo_root)
            return str(dir_path.resolve())
        except ValueError:
            pass
    mapped = _map_into_repo_root(raw_dir, "", repo_root, str(dir_path.resolve()))
    return mapped if mapped and Path(mapped).is_dir() else str(repo_root)


def _derive_legacy_roots(old_abs_file: str, new_abs_file: str) -> list[tuple[str, str]]:
    old_parts = _clean_path_parts(old_abs_file)
    new_parts = _clean_path_parts(new_abs_file)
    common_suffix = 0
    while common_suffix < len(old_parts) and common_suffix < len(new_parts):
        if old_parts[-1 - common_suffix].lower() != new_parts[-1 - common_suffix].lower():
            break
        common_suffix += 1

    if common_suffix == 0:
        return []

    old_path = Path(old_abs_file).resolve()
    new_path = Path(new_abs_file).resolve()
    old_root = old_path
    new_root = new_path
    for _ in range(common_suffix):
        old_root = old_root.parent
        new_root = new_root.parent

    if str(old_root) == str(old_path) or str(new_root) == str(new_path):
        return []
    old_root_norm = str(old_root).replace("\\", "/").lower()
    new_root_norm = str(new_root).replace("\\", "/")
    return [(old_root_norm, new_root_norm)]


def _rewrite_arguments(
    arguments: list[str],
    raw_file: str,
    raw_dir: str,
    old_abs_file: str,
    repo_root: Path,
    new_abs_file: str,
    legacy_roots: list[tuple[str, str]],
) -> list[str]:
    if not arguments:
        return []

    out: list[str] = []
    path_opts_separate = {"-I", "/I", "-isystem", "-imsvc", "-iquote", "/FI", "-include"}
    path_opts_attached = ["-I", "/I", "-isystem", "-imsvc", "-iquote", "/FI", "-include", "--sysroot="]

    i = 0
    while i < len(arguments):
        token = str(arguments[i])
        if token in path_opts_separate and i + 1 < len(arguments):
            out.append(token)
            out.append(_rewrite_path_token(arguments[i + 1], raw_dir, repo_root, legacy_roots, new_abs_file))
            i += 2
            continue

        replaced = False
        for prefix in path_opts_attached:
            if token.startswith(prefix) and len(token) > len(prefix):
                tail = token[len(prefix) :]
                mapped = _rewrite_path_token(tail, raw_dir, repo_root, legacy_roots, new_abs_file)
                out.append(prefix + mapped)
                replaced = True
                break
        if replaced:
            i += 1
            continue

        if _token_refers_to_source(token, raw_file, raw_dir, old_abs_file):
            out.append(new_abs_file)
        else:
            out.append(token)
        i += 1
    return out


def _rewrite_path_token(
    token: str,
    raw_dir: str,
    repo_root: Path,
    legacy_roots: list[tuple[str, str]],
    new_abs_file: str,
) -> str:
    text = token.strip()
    quote = ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        quote = text[0]
        text = text[1:-1]

    # Direct source-file rewrite.
    try:
        if _normalise(text) == _normalise(new_abs_file):
            mapped = new_abs_file
            return f"{quote}{mapped}{quote}" if quote else mapped
    except Exception:
        pass

    candidate_abs = ""
    if _looks_like_path(text):
        p = Path(text)
        if p.is_absolute():
            candidate_abs = str(p.resolve())
        elif raw_dir:
            candidate_abs = str((Path(raw_dir) / p).resolve())

    mapped = ""
    if candidate_abs:
        mapped = _map_legacy_prefix(candidate_abs, legacy_roots)
        if not mapped:
            mapped = _map_into_repo_root(candidate_abs, "", repo_root, candidate_abs)
    elif text.startswith("../") or text.startswith("..\\"):
        rel_parts = [p for p in PurePosixPath(text.replace("\\", "/")).parts if p not in (".", "..")]
        if rel_parts:
            rel_candidate = (repo_root / Path(*rel_parts)).resolve()
            if rel_candidate.exists():
                mapped = str(rel_candidate)

    final = mapped or text
    return f"{quote}{final}{quote}" if quote else final


def _map_legacy_prefix(path: str, legacy_roots: list[tuple[str, str]]) -> str:
    lowered = path.replace("\\", "/").lower()
    for old_root, new_root in legacy_roots:
        if lowered == old_root or lowered.startswith(old_root + "/"):
            suffix = path.replace("\\", "/")[len(old_root) :].lstrip("/")
            return str((Path(new_root) / Path(*suffix.split("/"))).resolve())
    return ""


def _token_refers_to_source(token: str, raw_file: str, raw_dir: str, old_abs_file: str) -> bool:
    text = token.strip().strip('"').strip("'")
    if not text:
        return False
    if text == raw_file:
        return True
    try:
        if _normalise(text) == _normalise(old_abs_file):
            return True
    except Exception:
        pass
    if raw_dir:
        try:
            resolved = str((Path(raw_dir) / text).resolve())
            if _normalise(resolved) == _normalise(old_abs_file):
                return True
        except Exception:
            pass
    return False
