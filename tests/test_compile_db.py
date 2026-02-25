"""Tests for the compilation database loader and query interface.

Covers:
  - CompileEntry: construction, repr, flags_hash
  - CompilationDatabase.load: valid/invalid JSON, missing file, non-array
  - Flag extraction: strips compiler, source file, -o pairs, command string
  - Path normalisation: relative -> absolute, case-insensitive Windows lookup
  - Query methods: get, has, all_files, __len__, __contains__
  - Edge cases: entries with no arguments, duplicate files
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cxxtract.orchestrator.compile_db import (
    CompilationDatabase,
    CompileEntry,
    _extract_flags,
    _normalise,
    _split_command,
)


# ====================================================================
# CompileEntry
# ====================================================================

class TestCompileEntry:

    def test_construction(self):
        entry = CompileEntry(
            file="src/main.cpp",
            directory="/project",
            arguments=["-std=c++17", "-Wall"],
        )
        assert entry.file == "src/main.cpp"
        assert entry.directory == "/project"
        assert entry.arguments == ["-std=c++17", "-Wall"]
        assert len(entry.flags_hash) == 64  # SHA-256 hex

    def test_flags_hash_deterministic(self):
        e1 = CompileEntry("f.cpp", "/d", ["-O2", "-Wall"])
        e2 = CompileEntry("f.cpp", "/d", ["-O2", "-Wall"])
        assert e1.flags_hash == e2.flags_hash

    def test_flags_hash_order_independent(self):
        e1 = CompileEntry("f.cpp", "/d", ["-Wall", "-O2"])
        e2 = CompileEntry("f.cpp", "/d", ["-O2", "-Wall"])
        assert e1.flags_hash == e2.flags_hash

    def test_repr(self):
        entry = CompileEntry("src/main.cpp", "/d", ["-O2"])
        r = repr(entry)
        assert "src/main.cpp" in r
        assert "nflags=1" in r


# ====================================================================
# CompilationDatabase.load
# ====================================================================

class TestLoad:

    def _write_compile_db(self, tmp_path: Path, entries: list[dict]) -> Path:
        p = tmp_path / "compile_commands.json"
        p.write_text(json.dumps(entries), encoding="utf-8")
        return p

    def test_load_valid(self, tmp_path: Path):
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        entries = [
            {
                "directory": str(tmp_path),
                "arguments": ["clang++", "-std=c++17", str(src)],
                "file": str(src),
            }
        ]
        p = self._write_compile_db(tmp_path, entries)
        db = CompilationDatabase.load(p)
        assert len(db) == 1
        assert db.has(src)

    def test_load_empty_array(self, tmp_path: Path):
        p = self._write_compile_db(tmp_path, [])
        db = CompilationDatabase.load(p)
        assert len(db) == 0

    def test_load_missing_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            CompilationDatabase.load(tmp_path / "nonexistent.json")

    def test_load_non_array(self, tmp_path: Path):
        p = tmp_path / "compile_commands.json"
        p.write_text('{"not": "an array"}', encoding="utf-8")
        with pytest.raises(ValueError, match="JSON array"):
            CompilationDatabase.load(p)

    def test_load_malformed_json(self, tmp_path: Path):
        p = tmp_path / "compile_commands.json"
        p.write_text("not valid json {{{{", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            CompilationDatabase.load(p)

    def test_load_with_command_string(self, tmp_path: Path):
        """Entries can use 'command' instead of 'arguments'."""
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        entries = [
            {
                "directory": str(tmp_path),
                "command": f'clang++ -std=c++17 -Wall "{src}"',
                "file": str(src),
            }
        ]
        p = self._write_compile_db(tmp_path, entries)
        db = CompilationDatabase.load(p)
        assert len(db) == 1
        entry = db.get(src)
        assert entry is not None
        # Should have extracted flags (minus compiler and source file)
        assert "-std=c++17" in entry.arguments

    def test_load_entry_no_arguments_or_command(self, tmp_path: Path):
        """Entries with neither 'arguments' nor 'command' are skipped."""
        entries = [
            {"directory": str(tmp_path), "file": "orphan.cpp"},
        ]
        p = self._write_compile_db(tmp_path, entries)
        db = CompilationDatabase.load(p)
        assert len(db) == 0

    def test_load_relative_file_path(self, tmp_path: Path):
        """Relative file paths are resolved relative to 'directory'."""
        src = tmp_path / "src" / "main.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() {}")
        entries = [
            {
                "directory": str(tmp_path),
                "arguments": ["clang++", "-O2", "src/main.cpp"],
                "file": "src/main.cpp",
            }
        ]
        p = self._write_compile_db(tmp_path, entries)
        db = CompilationDatabase.load(p)
        assert len(db) == 1
        # Should be able to find via the absolute path
        assert db.has(src)

    def test_load_duplicate_files_last_wins(self, tmp_path: Path):
        """If the same file appears twice, the last entry wins."""
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        entries = [
            {
                "directory": str(tmp_path),
                "arguments": ["clang++", "-O0", str(src)],
                "file": str(src),
            },
            {
                "directory": str(tmp_path),
                "arguments": ["clang++", "-O3", str(src)],
                "file": str(src),
            },
        ]
        p = self._write_compile_db(tmp_path, entries)
        db = CompilationDatabase.load(p)
        assert len(db) == 1
        entry = db.get(src)
        assert entry is not None
        assert "-O3" in entry.arguments

    def test_load_multiple_entries(self, tmp_path: Path):
        files = []
        for name in ["a.cpp", "b.cpp", "c.cpp"]:
            f = tmp_path / name
            f.write_text(f"// {name}")
            files.append(f)
        entries = [
            {
                "directory": str(tmp_path),
                "arguments": ["clang++", "-O2", str(f)],
                "file": str(f),
            }
            for f in files
        ]
        p = self._write_compile_db(tmp_path, entries)
        db = CompilationDatabase.load(p)
        assert len(db) == 3
        for f in files:
            assert db.has(f)


# ====================================================================
# Query methods
# ====================================================================

class TestQuery:

    @pytest.fixture
    def db(self, tmp_path: Path) -> CompilationDatabase:
        src_a = tmp_path / "a.cpp"
        src_b = tmp_path / "b.cpp"
        src_a.write_text("// a")
        src_b.write_text("// b")
        entries = {
            _normalise(str(src_a)): CompileEntry(str(src_a), str(tmp_path), ["-O2"]),
            _normalise(str(src_b)): CompileEntry(str(src_b), str(tmp_path), ["-O3"]),
        }
        return CompilationDatabase(entries)

    def test_get_existing(self, db: CompilationDatabase, tmp_path: Path):
        entry = db.get(tmp_path / "a.cpp")
        assert entry is not None
        assert "-O2" in entry.arguments

    def test_get_nonexistent(self, db: CompilationDatabase, tmp_path: Path):
        assert db.get(tmp_path / "z.cpp") is None

    def test_has(self, db: CompilationDatabase, tmp_path: Path):
        assert db.has(tmp_path / "a.cpp")
        assert not db.has(tmp_path / "z.cpp")

    def test_all_files(self, db: CompilationDatabase, tmp_path: Path):
        files = db.all_files()
        assert len(files) == 2

    def test_len(self, db: CompilationDatabase):
        assert len(db) == 2

    def test_contains(self, db: CompilationDatabase, tmp_path: Path):
        assert (tmp_path / "a.cpp") in db
        assert (tmp_path / "z.cpp") not in db


# ====================================================================
# _extract_flags helper
# ====================================================================

class TestExtractFlags:

    def test_strips_compiler(self):
        flags = _extract_flags(["clang++", "-O2", "-Wall"], "nonexistent.cpp")
        assert "clang++" not in flags
        assert "-O2" in flags
        assert "-Wall" in flags

    def test_strips_source_file(self, tmp_path: Path):
        src = tmp_path / "main.cpp"
        src.write_text("")
        flags = _extract_flags(
            ["clang++", "-std=c++17", str(src)], str(src)
        )
        assert str(src) not in flags
        assert "-std=c++17" in flags

    def test_strips_output_pair_dash_o(self):
        flags = _extract_flags(
            ["g++", "-O2", "-o", "main.o", "-Wall"], "nonexistent.cpp"
        )
        assert "-o" not in flags
        assert "main.o" not in flags
        assert "-Wall" in flags

    def test_strips_output_pair_slash_fo(self):
        flags = _extract_flags(
            ["cl.exe", "/O2", "/Fo", "main.obj", "/W4"], "nonexistent.cpp"
        )
        assert "/Fo" not in flags
        assert "main.obj" not in flags
        assert "/W4" in flags

    def test_empty_arguments(self):
        flags = _extract_flags([], "main.cpp")
        assert flags == []

    def test_only_compiler(self):
        flags = _extract_flags(["clang++"], "main.cpp")
        assert flags == []


# ====================================================================
# _split_command helper
# ====================================================================

class TestSplitCommand:

    def test_simple_command(self):
        parts = _split_command("clang++ -O2 -Wall main.cpp")
        assert parts[0] == "clang++"
        assert "-O2" in parts

    def test_quoted_path(self):
        parts = _split_command('clang++ -I"C:\\Program Files\\include" main.cpp')
        assert len(parts) >= 3  # should preserve the quoted path

    def test_malformed_quotes(self):
        """Malformed commands should fall back to simple split."""
        parts = _split_command('clang++ "unterminated')
        assert len(parts) >= 2


# ====================================================================
# _normalise helper
# ====================================================================

class TestNormalise:

    def test_lowercases(self, tmp_path: Path):
        f = tmp_path / "Test.cpp"
        f.write_text("")
        result = _normalise(str(f))
        assert result == result.lower()

    def test_resolves_to_absolute(self):
        result = _normalise("relative/path/test.cpp")
        assert Path(result).is_absolute()
