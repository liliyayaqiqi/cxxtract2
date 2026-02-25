"""Comprehensive tests for the cache layer (db, hasher, repository).

Covers:
  - db.py:         init_db, close_db, get_connection, PRAGMA enforcement
  - hasher.py:     compute_content_hash, compute_flags_hash,
                   compute_includes_hash, compute_composite_hash
  - repository.py: All CRUD for tracked_files, symbols, references_,
                   call_edges, include_deps, parse_runs, plus
                   upsert_extractor_output atomicity and FK CASCADE.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from cxxtract.cache.db import close_db, get_connection, init_db
from cxxtract.cache.hasher import (
    compute_composite_hash,
    compute_content_hash,
    compute_flags_hash,
    compute_includes_hash,
)
from cxxtract.cache import repository as repo
from cxxtract.models import (
    ExtractedCallEdge,
    ExtractedIncludeDep,
    ExtractedReference,
    ExtractedSymbol,
    ExtractorOutput,
)


# ====================================================================
# db.py tests
# ====================================================================

class TestInitDb:
    """Tests for init_db() and connection management."""

    async def test_init_memory(self, db_conn: aiosqlite.Connection):
        """init_db(':memory:') should return a usable connection."""
        assert db_conn is not None
        # Verify we can query (tables exist)
        cursor = await db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]
        assert "tracked_files" in tables
        assert "symbols" in tables
        assert "references_" in tables
        assert "call_edges" in tables
        assert "include_deps" in tables
        assert "parse_runs" in tables

    async def test_init_file_based(self, tmp_path: Path):
        """init_db with a file path should create the database file."""
        db_path = str(tmp_path / "test.db")
        conn = await init_db(db_path)
        try:
            assert Path(db_path).exists()
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = [row[0] for row in await cursor.fetchall()]
            assert "tracked_files" in tables
        finally:
            await conn.close()

    async def test_pragma_foreign_keys_on(self, db_conn: aiosqlite.Connection):
        """PRAGMA foreign_keys should be ON after init."""
        cursor = await db_conn.execute("PRAGMA foreign_keys")
        row = await cursor.fetchone()
        assert row[0] == 1, "foreign_keys PRAGMA should be ON"

    async def test_migration_idempotent(self, db_conn: aiosqlite.Connection):
        """Running init_db again on the same DB should not fail."""
        # The fixture already ran init_db; running the DDL again should be safe
        from cxxtract.cache.db import _load_migration_sql
        ddl = _load_migration_sql()
        await db_conn.executescript(ddl)
        await db_conn.commit()
        # Tables still exist
        cursor = await db_conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        )
        row = await cursor.fetchone()
        assert row[0] >= 6

    async def test_get_connection_before_init_raises(self):
        """get_connection should raise RuntimeError if DB not initialised."""
        # Temporarily clear the global
        import cxxtract.cache.db as db_mod
        saved = db_mod._connection
        db_mod._connection = None
        try:
            with pytest.raises(RuntimeError, match="not initialised"):
                get_connection()
        finally:
            db_mod._connection = saved

    async def test_close_db_idempotent(self):
        """close_db should not raise if called when no connection exists."""
        import cxxtract.cache.db as db_mod
        saved = db_mod._connection
        db_mod._connection = None
        await close_db()  # should not raise
        db_mod._connection = saved


# ====================================================================
# hasher.py tests
# ====================================================================

class TestContentHash:
    """Tests for compute_content_hash()."""

    def test_hash_known_content(self, tmp_path: Path):
        f = tmp_path / "hello.cpp"
        content = b"int main() { return 0; }\n"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert compute_content_hash(str(f)) == expected

    def test_hash_missing_file(self):
        result = compute_content_hash("/nonexistent/file.cpp")
        assert result == ""

    def test_hash_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty.cpp"
        f.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        assert compute_content_hash(str(f)) == expected

    def test_different_content_different_hash(self, tmp_path: Path):
        f1 = tmp_path / "a.cpp"
        f2 = tmp_path / "b.cpp"
        f1.write_bytes(b"aaa")
        f2.write_bytes(b"bbb")
        assert compute_content_hash(str(f1)) != compute_content_hash(str(f2))


class TestFlagsHash:
    """Tests for compute_flags_hash()."""

    def test_deterministic(self):
        flags = ["-std=c++17", "-Wall", "-O2"]
        h1 = compute_flags_hash(flags)
        h2 = compute_flags_hash(flags)
        assert h1 == h2

    def test_order_independent(self):
        """Flag reordering should NOT change the hash."""
        h1 = compute_flags_hash(["-Wall", "-O2", "-std=c++17"])
        h2 = compute_flags_hash(["-std=c++17", "-O2", "-Wall"])
        assert h1 == h2

    def test_different_flags_different_hash(self):
        h1 = compute_flags_hash(["-O2"])
        h2 = compute_flags_hash(["-O3"])
        assert h1 != h2

    def test_empty_flags(self):
        h = compute_flags_hash([])
        assert len(h) == 64  # SHA-256 hex is 64 chars


class TestIncludesHash:
    """Tests for compute_includes_hash()."""

    def test_deterministic(self):
        hashes = ["aaa", "bbb", "ccc"]
        assert compute_includes_hash(hashes) == compute_includes_hash(hashes)

    def test_order_independent(self):
        h1 = compute_includes_hash(["aaa", "bbb"])
        h2 = compute_includes_hash(["bbb", "aaa"])
        assert h1 == h2

    def test_empty_list(self):
        h = compute_includes_hash([])
        assert len(h) == 64


class TestCompositeHash:
    """Tests for compute_composite_hash()."""

    def test_deterministic(self):
        h = compute_composite_hash("c", "i", "f")
        assert h == compute_composite_hash("c", "i", "f")

    def test_any_component_change_changes_composite(self):
        base = compute_composite_hash("c1", "i1", "f1")
        assert compute_composite_hash("c2", "i1", "f1") != base
        assert compute_composite_hash("c1", "i2", "f1") != base
        assert compute_composite_hash("c1", "i1", "f2") != base

    def test_hash_length(self):
        h = compute_composite_hash("a", "b", "c")
        assert len(h) == 64


# ====================================================================
# Helpers for repository tests
# ====================================================================

def _make_symbols(n: int = 2, prefix: str = "Sym") -> list[ExtractedSymbol]:
    return [
        ExtractedSymbol(
            name=f"{prefix}{i}",
            qualified_name=f"ns::{prefix}{i}",
            kind="Function",
            line=10 + i,
            col=1,
            extent_end_line=20 + i,
        )
        for i in range(n)
    ]


def _make_references(n: int = 2, prefix: str = "Sym") -> list[ExtractedReference]:
    return [
        ExtractedReference(
            symbol=f"ns::{prefix}{i}",
            line=30 + i,
            col=5,
            kind="call",
        )
        for i in range(n)
    ]


def _make_call_edges(n: int = 2) -> list[ExtractedCallEdge]:
    return [
        ExtractedCallEdge(
            caller=f"ns::Caller{i}",
            callee=f"ns::Callee{i}",
            line=50 + i,
        )
        for i in range(n)
    ]


def _make_include_deps(n: int = 2) -> list[ExtractedIncludeDep]:
    return [
        ExtractedIncludeDep(path=f"include/header{i}.h", depth=i + 1)
        for i in range(n)
    ]


async def _insert_tracked(
    conn: aiosqlite.Connection,
    file_path: str = "src/main.cpp",
) -> None:
    """Helper: insert a tracked file so FK constraints are satisfied."""
    await repo.upsert_tracked_file(
        file_path, "ch", "fh", "ih", "comp", conn=conn,
    )


# ====================================================================
# repository.py — tracked_files
# ====================================================================

class TestTrackedFiles:
    """CRUD tests for the tracked_files table."""

    async def test_upsert_and_get(self, db_conn: aiosqlite.Connection):
        await repo.upsert_tracked_file(
            "src/a.cpp", "c1", "f1", "i1", "comp1", conn=db_conn,
        )
        row = await repo.get_tracked_file("src/a.cpp", conn=db_conn)
        assert row is not None
        assert row["file_path"] == "src/a.cpp"
        assert row["content_hash"] == "c1"
        assert row["composite_hash"] == "comp1"
        assert row["last_parsed_at"]  # non-empty timestamp

    async def test_upsert_overwrites(self, db_conn: aiosqlite.Connection):
        await repo.upsert_tracked_file(
            "src/a.cpp", "c1", "f1", "i1", "comp1", conn=db_conn,
        )
        await repo.upsert_tracked_file(
            "src/a.cpp", "c2", "f2", "i2", "comp2", conn=db_conn,
        )
        row = await repo.get_tracked_file("src/a.cpp", conn=db_conn)
        assert row["content_hash"] == "c2"
        assert row["composite_hash"] == "comp2"
        # Should still be one row total
        count = await repo.count_tracked_files(conn=db_conn)
        assert count == 1

    async def test_get_nonexistent(self, db_conn: aiosqlite.Connection):
        row = await repo.get_tracked_file("nonexistent.cpp", conn=db_conn)
        assert row is None

    async def test_get_composite_hash(self, db_conn: aiosqlite.Connection):
        await repo.upsert_tracked_file(
            "src/a.cpp", "c1", "f1", "i1", "comp1", conn=db_conn,
        )
        h = await repo.get_composite_hash("src/a.cpp", conn=db_conn)
        assert h == "comp1"

    async def test_get_composite_hash_missing(self, db_conn: aiosqlite.Connection):
        h = await repo.get_composite_hash("nope.cpp", conn=db_conn)
        assert h is None

    async def test_delete_tracked_file(self, db_conn: aiosqlite.Connection):
        await repo.upsert_tracked_file(
            "src/a.cpp", "c1", "f1", "i1", "comp1", conn=db_conn,
        )
        await repo.delete_tracked_file("src/a.cpp", conn=db_conn)
        row = await repo.get_tracked_file("src/a.cpp", conn=db_conn)
        assert row is None

    async def test_count_tracked_files(self, db_conn: aiosqlite.Connection):
        assert await repo.count_tracked_files(conn=db_conn) == 0
        await repo.upsert_tracked_file("a.cpp", "c", "f", "i", "x", conn=db_conn)
        await repo.upsert_tracked_file("b.cpp", "c", "f", "i", "x", conn=db_conn)
        assert await repo.count_tracked_files(conn=db_conn) == 2

    async def test_clear_all(self, db_conn: aiosqlite.Connection):
        await repo.upsert_tracked_file("a.cpp", "c", "f", "i", "x", conn=db_conn)
        await repo.upsert_tracked_file("b.cpp", "c", "f", "i", "x", conn=db_conn)
        count = await repo.clear_all(conn=db_conn)
        assert count == 2
        assert await repo.count_tracked_files(conn=db_conn) == 0


# ====================================================================
# repository.py — symbols
# ====================================================================

class TestSymbols:
    """CRUD tests for the symbols table."""

    async def test_upsert_and_get_by_file(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        symbols = _make_symbols(3)
        await repo.upsert_symbols("src/main.cpp", symbols, conn=db_conn)
        rows = await repo.get_symbols_by_file("src/main.cpp", conn=db_conn)
        assert len(rows) == 3
        names = {r["name"] for r in rows}
        assert names == {"Sym0", "Sym1", "Sym2"}

    async def test_upsert_replaces_old_symbols(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_symbols("src/main.cpp", _make_symbols(5), conn=db_conn)
        await repo.upsert_symbols("src/main.cpp", _make_symbols(2), conn=db_conn)
        rows = await repo.get_symbols_by_file("src/main.cpp", conn=db_conn)
        assert len(rows) == 2

    async def test_upsert_empty_list_clears(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_symbols("src/main.cpp", _make_symbols(3), conn=db_conn)
        await repo.upsert_symbols("src/main.cpp", [], conn=db_conn)
        rows = await repo.get_symbols_by_file("src/main.cpp", conn=db_conn)
        assert len(rows) == 0

    async def test_get_definitions_for_symbol(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_symbols("src/main.cpp", _make_symbols(3), conn=db_conn)
        defs = await repo.get_definitions_for_symbol("ns::Sym1", conn=db_conn)
        assert len(defs) == 1
        assert defs[0]["qualified_name"] == "ns::Sym1"

    async def test_search_symbols_by_name(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_symbols("src/main.cpp", _make_symbols(3), conn=db_conn)
        results = await repo.search_symbols_by_name("Sym", conn=db_conn)
        assert len(results) == 3  # all match

    async def test_search_symbols_no_match(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_symbols("src/main.cpp", _make_symbols(3), conn=db_conn)
        results = await repo.search_symbols_by_name("ZZZ_NOPE", conn=db_conn)
        assert len(results) == 0

    async def test_count_symbols(self, db_conn: aiosqlite.Connection):
        assert await repo.count_symbols(conn=db_conn) == 0
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_symbols("src/main.cpp", _make_symbols(4), conn=db_conn)
        assert await repo.count_symbols(conn=db_conn) == 4

    async def test_symbols_across_files(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "a.cpp")
        await _insert_tracked(db_conn, "b.cpp")
        await repo.upsert_symbols("a.cpp", _make_symbols(2, "A"), conn=db_conn)
        await repo.upsert_symbols("b.cpp", _make_symbols(3, "B"), conn=db_conn)
        assert await repo.count_symbols(conn=db_conn) == 5
        a_rows = await repo.get_symbols_by_file("a.cpp", conn=db_conn)
        b_rows = await repo.get_symbols_by_file("b.cpp", conn=db_conn)
        assert len(a_rows) == 2
        assert len(b_rows) == 3


# ====================================================================
# repository.py — references_
# ====================================================================

class TestReferences:
    """CRUD tests for the references_ table."""

    async def test_upsert_and_get(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        refs = _make_references(3)
        await repo.upsert_references("src/main.cpp", refs, conn=db_conn)
        rows = await repo.get_references_for_symbol("ns::Sym0", conn=db_conn)
        assert len(rows) == 1
        assert rows[0]["line"] == 30

    async def test_upsert_replaces(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_references("src/main.cpp", _make_references(5), conn=db_conn)
        await repo.upsert_references("src/main.cpp", _make_references(2), conn=db_conn)
        rows = await repo.search_references_by_symbol("ns::Sym", conn=db_conn)
        assert len(rows) == 2

    async def test_search_references_by_symbol(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_references("src/main.cpp", _make_references(3), conn=db_conn)
        rows = await repo.search_references_by_symbol("Sym", conn=db_conn)
        assert len(rows) == 3

    async def test_search_references_no_match(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_references("src/main.cpp", _make_references(3), conn=db_conn)
        rows = await repo.search_references_by_symbol("ZZZ_NOPE", conn=db_conn)
        assert len(rows) == 0


# ====================================================================
# repository.py — call_edges
# ====================================================================

class TestCallEdges:
    """CRUD tests for the call_edges table."""

    async def test_upsert_and_get_by_caller(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        edges = _make_call_edges(3)
        await repo.upsert_call_edges("src/main.cpp", edges, conn=db_conn)
        rows = await repo.get_call_edges_for_caller("ns::Caller0", conn=db_conn)
        assert len(rows) == 1
        assert rows[0]["callee_qualified_name"] == "ns::Callee0"

    async def test_get_by_callee(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_call_edges("src/main.cpp", _make_call_edges(3), conn=db_conn)
        rows = await repo.get_call_edges_for_callee("ns::Callee1", conn=db_conn)
        assert len(rows) == 1
        assert rows[0]["caller_qualified_name"] == "ns::Caller1"

    async def test_upsert_replaces(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_call_edges("src/main.cpp", _make_call_edges(5), conn=db_conn)
        await repo.upsert_call_edges("src/main.cpp", _make_call_edges(1), conn=db_conn)
        # Only 1 caller should remain
        all_rows = await repo.get_call_edges_for_caller("ns::Caller0", conn=db_conn)
        assert len(all_rows) == 1
        # Old ones should be gone
        old_rows = await repo.get_call_edges_for_caller("ns::Caller4", conn=db_conn)
        assert len(old_rows) == 0


# ====================================================================
# repository.py — include_deps
# ====================================================================

class TestIncludeDeps:
    """CRUD tests for the include_deps table."""

    async def test_upsert_and_get(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        deps = _make_include_deps(3)
        await repo.upsert_include_deps("src/main.cpp", deps, conn=db_conn)
        rows = await repo.get_include_deps("src/main.cpp", conn=db_conn)
        assert len(rows) == 3
        paths = {r["included_path"] for r in rows}
        assert "include/header0.h" in paths

    async def test_upsert_replaces(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_include_deps("src/main.cpp", _make_include_deps(5), conn=db_conn)
        await repo.upsert_include_deps("src/main.cpp", _make_include_deps(2), conn=db_conn)
        rows = await repo.get_include_deps("src/main.cpp", conn=db_conn)
        assert len(rows) == 2

    async def test_get_empty(self, db_conn: aiosqlite.Connection):
        await _insert_tracked(db_conn, "src/main.cpp")
        rows = await repo.get_include_deps("src/main.cpp", conn=db_conn)
        assert rows == []


# ====================================================================
# repository.py — parse_runs
# ====================================================================

class TestParseRuns:
    """Tests for parse run audit logging."""

    async def test_insert_and_finish(self, db_conn: aiosqlite.Connection):
        started = datetime.now(timezone.utc).isoformat()
        run_id = await repo.insert_parse_run("src/a.cpp", started, conn=db_conn)
        assert isinstance(run_id, int) and run_id > 0

        await repo.finish_parse_run(run_id, success=True, conn=db_conn)
        runs = await repo.get_parse_runs("src/a.cpp", conn=db_conn)
        assert len(runs) == 1
        assert runs[0]["success"] == 1
        assert runs[0]["finished_at"] is not None

    async def test_insert_failure_run(self, db_conn: aiosqlite.Connection):
        started = datetime.now(timezone.utc).isoformat()
        run_id = await repo.insert_parse_run("src/a.cpp", started, conn=db_conn)
        await repo.finish_parse_run(
            run_id, success=False, error_msg="timeout", conn=db_conn,
        )
        runs = await repo.get_parse_runs("src/a.cpp", conn=db_conn)
        assert runs[0]["success"] == 0
        assert runs[0]["error_msg"] == "timeout"

    async def test_multiple_runs(self, db_conn: aiosqlite.Connection):
        for _ in range(3):
            started = datetime.now(timezone.utc).isoformat()
            run_id = await repo.insert_parse_run("src/a.cpp", started, conn=db_conn)
            await repo.finish_parse_run(run_id, success=True, conn=db_conn)
        runs = await repo.get_parse_runs("src/a.cpp", conn=db_conn)
        assert len(runs) == 3

    async def test_get_parse_runs_empty(self, db_conn: aiosqlite.Connection):
        runs = await repo.get_parse_runs("nonexistent.cpp", conn=db_conn)
        assert runs == []


# ====================================================================
# repository.py — FK CASCADE
# ====================================================================

class TestForeignKeyCascade:
    """Verify ON DELETE CASCADE propagation."""

    async def test_delete_tracked_file_cascades_symbols(
        self, db_conn: aiosqlite.Connection,
    ):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_symbols("src/main.cpp", _make_symbols(3), conn=db_conn)
        assert await repo.count_symbols(conn=db_conn) == 3

        await repo.delete_tracked_file("src/main.cpp", conn=db_conn)
        assert await repo.count_symbols(conn=db_conn) == 0

    async def test_delete_tracked_file_cascades_references(
        self, db_conn: aiosqlite.Connection,
    ):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_references("src/main.cpp", _make_references(2), conn=db_conn)
        await repo.delete_tracked_file("src/main.cpp", conn=db_conn)
        rows = await repo.search_references_by_symbol("ns::Sym", conn=db_conn)
        assert len(rows) == 0

    async def test_delete_tracked_file_cascades_call_edges(
        self, db_conn: aiosqlite.Connection,
    ):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_call_edges("src/main.cpp", _make_call_edges(2), conn=db_conn)
        await repo.delete_tracked_file("src/main.cpp", conn=db_conn)
        rows = await repo.get_call_edges_for_caller("ns::Caller0", conn=db_conn)
        assert len(rows) == 0

    async def test_delete_tracked_file_cascades_include_deps(
        self, db_conn: aiosqlite.Connection,
    ):
        await _insert_tracked(db_conn, "src/main.cpp")
        await repo.upsert_include_deps("src/main.cpp", _make_include_deps(2), conn=db_conn)
        await repo.delete_tracked_file("src/main.cpp", conn=db_conn)
        rows = await repo.get_include_deps("src/main.cpp", conn=db_conn)
        assert len(rows) == 0

    async def test_clear_all_cascades_everything(
        self, db_conn: aiosqlite.Connection,
    ):
        await _insert_tracked(db_conn, "a.cpp")
        await _insert_tracked(db_conn, "b.cpp")
        await repo.upsert_symbols("a.cpp", _make_symbols(2, "A"), conn=db_conn)
        await repo.upsert_symbols("b.cpp", _make_symbols(3, "B"), conn=db_conn)
        await repo.upsert_references("a.cpp", _make_references(1, "A"), conn=db_conn)
        await repo.upsert_call_edges("a.cpp", _make_call_edges(1), conn=db_conn)
        await repo.upsert_include_deps("b.cpp", _make_include_deps(1), conn=db_conn)

        deleted = await repo.clear_all(conn=db_conn)
        assert deleted == 2
        assert await repo.count_tracked_files(conn=db_conn) == 0
        assert await repo.count_symbols(conn=db_conn) == 0

    async def test_cascade_only_affects_target_file(
        self, db_conn: aiosqlite.Connection,
    ):
        """Deleting file A should not touch file B's facts."""
        await _insert_tracked(db_conn, "a.cpp")
        await _insert_tracked(db_conn, "b.cpp")
        await repo.upsert_symbols("a.cpp", _make_symbols(2, "A"), conn=db_conn)
        await repo.upsert_symbols("b.cpp", _make_symbols(3, "B"), conn=db_conn)

        await repo.delete_tracked_file("a.cpp", conn=db_conn)

        assert await repo.count_symbols(conn=db_conn) == 3
        rows = await repo.get_symbols_by_file("b.cpp", conn=db_conn)
        assert len(rows) == 3


# ====================================================================
# repository.py — upsert_extractor_output (atomic bulk)
# ====================================================================

class TestUpsertExtractorOutput:
    """Tests for the atomic bulk upsert."""

    def _make_output(self, file_path: str = "src/main.cpp") -> ExtractorOutput:
        return ExtractorOutput(
            file=file_path,
            symbols=_make_symbols(2),
            references=_make_references(2),
            call_edges=_make_call_edges(1),
            include_deps=_make_include_deps(1),
            success=True,
            diagnostics=[],
        )

    async def test_basic_upsert(self, db_conn: aiosqlite.Connection):
        output = self._make_output()
        await repo.upsert_extractor_output(
            output, "ch", "fh", "ih", "comp", conn=db_conn,
        )
        # tracked file created
        tracked = await repo.get_tracked_file("src/main.cpp", conn=db_conn)
        assert tracked is not None
        assert tracked["composite_hash"] == "comp"

        # symbols created
        syms = await repo.get_symbols_by_file("src/main.cpp", conn=db_conn)
        assert len(syms) == 2

        # references created
        refs = await repo.search_references_by_symbol("ns::Sym", conn=db_conn)
        assert len(refs) == 2

        # call edges created
        edges = await repo.get_call_edges_for_caller("ns::Caller0", conn=db_conn)
        assert len(edges) == 1

        # include deps created
        deps = await repo.get_include_deps("src/main.cpp", conn=db_conn)
        assert len(deps) == 1

    async def test_upsert_replaces_previous(self, db_conn: aiosqlite.Connection):
        # First upsert
        output1 = self._make_output()
        await repo.upsert_extractor_output(
            output1, "ch1", "fh1", "ih1", "comp1", conn=db_conn,
        )
        # Second upsert with different data
        output2 = ExtractorOutput(
            file="src/main.cpp",
            symbols=_make_symbols(5, "New"),
            references=[],
            call_edges=[],
            include_deps=[],
        )
        await repo.upsert_extractor_output(
            output2, "ch2", "fh2", "ih2", "comp2", conn=db_conn,
        )
        # Old symbols gone, new ones present
        syms = await repo.get_symbols_by_file("src/main.cpp", conn=db_conn)
        assert len(syms) == 5
        assert syms[0]["name"].startswith("New")

        # Old refs/edges/deps cleared
        refs = await repo.search_references_by_symbol("ns::Sym", conn=db_conn)
        assert len(refs) == 0

    async def test_upsert_empty_facts(self, db_conn: aiosqlite.Connection):
        output = ExtractorOutput(
            file="src/empty.cpp",
            symbols=[],
            references=[],
            call_edges=[],
            include_deps=[],
        )
        await repo.upsert_extractor_output(
            output, "ch", "fh", "ih", "comp", conn=db_conn,
        )
        tracked = await repo.get_tracked_file("src/empty.cpp", conn=db_conn)
        assert tracked is not None
        assert await repo.count_symbols(conn=db_conn) == 0

    async def test_multiple_files(self, db_conn: aiosqlite.Connection):
        for name in ["a.cpp", "b.cpp", "c.cpp"]:
            output = self._make_output(name)
            await repo.upsert_extractor_output(
                output, "ch", "fh", "ih", f"comp_{name}", conn=db_conn,
            )
        assert await repo.count_tracked_files(conn=db_conn) == 3
        assert await repo.count_symbols(conn=db_conn) == 6  # 2 per file


# ====================================================================
# End-to-end: hash + cache invalidation scenario
# ====================================================================

class TestCacheInvalidationFlow:
    """Simulate the orchestrator's cache-check -> stale detection flow."""

    async def test_fresh_file_has_matching_hash(
        self, db_conn: aiosqlite.Connection, tmp_path: Path,
    ):
        f = tmp_path / "main.cpp"
        f.write_text("int main() {}")
        fp = str(f)

        content_hash = compute_content_hash(fp)
        flags_hash = compute_flags_hash(["-O2"])
        includes_hash = compute_includes_hash([])
        composite = compute_composite_hash(content_hash, includes_hash, flags_hash)

        await repo.upsert_tracked_file(
            fp, content_hash, flags_hash, includes_hash, composite, conn=db_conn,
        )

        # Re-check: hash should still match (file unchanged)
        cached = await repo.get_composite_hash(fp, conn=db_conn)
        recomputed = compute_composite_hash(
            compute_content_hash(fp),
            includes_hash,
            flags_hash,
        )
        assert cached == recomputed

    async def test_modified_file_detected_as_stale(
        self, db_conn: aiosqlite.Connection, tmp_path: Path,
    ):
        f = tmp_path / "main.cpp"
        f.write_text("int main() {}")
        fp = str(f)

        content_hash = compute_content_hash(fp)
        flags_hash = compute_flags_hash(["-O2"])
        includes_hash = compute_includes_hash([])
        composite = compute_composite_hash(content_hash, includes_hash, flags_hash)

        await repo.upsert_tracked_file(
            fp, content_hash, flags_hash, includes_hash, composite, conn=db_conn,
        )

        # Modify the file
        f.write_text("int main() { return 1; }")
        new_content_hash = compute_content_hash(fp)
        new_composite = compute_composite_hash(new_content_hash, includes_hash, flags_hash)

        cached = await repo.get_composite_hash(fp, conn=db_conn)
        assert cached != new_composite  # stale!

    async def test_flags_change_detected_as_stale(
        self, db_conn: aiosqlite.Connection, tmp_path: Path,
    ):
        f = tmp_path / "main.cpp"
        f.write_text("int main() {}")
        fp = str(f)

        content_hash = compute_content_hash(fp)
        old_flags_hash = compute_flags_hash(["-O2"])
        includes_hash = compute_includes_hash([])
        composite = compute_composite_hash(content_hash, includes_hash, old_flags_hash)

        await repo.upsert_tracked_file(
            fp, content_hash, old_flags_hash, includes_hash, composite, conn=db_conn,
        )

        # Same file, different flags
        new_flags_hash = compute_flags_hash(["-O3", "-DNDEBUG"])
        new_composite = compute_composite_hash(content_hash, includes_hash, new_flags_hash)

        cached = await repo.get_composite_hash(fp, conn=db_conn)
        assert cached != new_composite  # stale due to flags change
