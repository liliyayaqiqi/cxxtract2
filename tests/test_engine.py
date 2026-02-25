"""Tests for the orchestration engine (engine.py).

Covers:
  - OrchestratorEngine construction and compile_db management
  - _build_confidence: ratio calculation, file classification
  - _recall_candidates: recall result -> (file_paths, warnings)
  - _classify_files: fresh / stale / unparsed classification
  - query_references: cache hit, cache miss with parsing, no compile_db
  - query_definition: found / not found / multiple definitions
  - query_call_graph: outgoing / incoming / both / empty
  - query_file_symbols: fresh / stale / no compile flags
  - invalidate_cache: specific files / entire cache
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from cxxtract.cache import repository as repo
from cxxtract.cache.db import close_db, init_db
from cxxtract.config import Settings
from cxxtract.models import (
    CacheInvalidateRequest,
    CallGraphDirection,
    CallGraphRequest,
    ConfidenceEnvelope,
    ExtractedCallEdge,
    ExtractedReference,
    ExtractedSymbol,
    ExtractorOutput,
    FileSymbolsRequest,
    RecallHit,
    RecallResult,
    SymbolQueryRequest,
)
from cxxtract.orchestrator.compile_db import CompilationDatabase, CompileEntry
from cxxtract.orchestrator.engine import OrchestratorEngine


# ====================================================================
# Fixtures
# ====================================================================

@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=":memory:",
        rg_binary="rg",
        extractor_binary="fake-extractor",
        max_parse_workers=2,
        max_recall_files=50,
        recall_timeout_s=10,
        parse_timeout_s=30,
    )


@pytest.fixture
def engine(settings: Settings) -> OrchestratorEngine:
    return OrchestratorEngine(settings)


def _make_compile_db(
    files: dict[str, list[str]], tmp_path: Path,
) -> CompilationDatabase:
    """Build a CompilationDatabase from {filepath: [flags]}."""
    from cxxtract.orchestrator.compile_db import _normalise

    entries = {}
    for fp, flags in files.items():
        resolved = str(Path(fp).resolve())
        entries[_normalise(resolved)] = CompileEntry(
            file=resolved, directory=str(tmp_path), arguments=flags,
        )
    return CompilationDatabase(entries)


# ====================================================================
# _build_confidence
# ====================================================================

class TestBuildConfidence:

    def test_all_verified(self, engine: OrchestratorEngine):
        c = engine._build_confidence(
            fresh_files=["a.cpp", "b.cpp"],
            newly_parsed=[],
            stale_files=[],
            unparsed_files=[],
            failed_files=[],
        )
        assert c.verified_ratio == 1.0
        assert c.total_candidates == 2
        assert c.verified_files == ["a.cpp", "b.cpp"]
        assert c.warnings == []

    def test_mixed(self, engine: OrchestratorEngine):
        c = engine._build_confidence(
            fresh_files=["a.cpp"],
            newly_parsed=["b.cpp"],
            stale_files=[],
            unparsed_files=["c.cpp"],
            failed_files=["d.cpp"],
            warnings=["slow query"],
        )
        assert c.total_candidates == 4
        assert c.verified_ratio == 0.5
        assert c.verified_files == ["a.cpp", "b.cpp"]
        assert c.stale_files == ["d.cpp"]
        assert c.unparsed_files == ["c.cpp"]
        assert c.warnings == ["slow query"]

    def test_empty(self, engine: OrchestratorEngine):
        c = engine._build_confidence([], [], [], [], [])
        assert c.total_candidates == 0
        assert c.verified_ratio == 0.0

    def test_all_failed(self, engine: OrchestratorEngine):
        c = engine._build_confidence([], [], [], [], ["a.cpp", "b.cpp"])
        assert c.verified_ratio == 0.0
        assert c.total_candidates == 2


# ====================================================================
# get_compile_db / invalidate_compile_db
# ====================================================================

class TestCompileDbManagement:

    def test_get_returns_none_no_path(self, engine: OrchestratorEngine):
        assert engine.get_compile_db(None) is None

    def test_get_returns_none_empty_default(self, engine: OrchestratorEngine):
        # default_compile_commands is "" in settings
        assert engine.get_compile_db(None) is None

    def test_get_caches_db(self, engine: OrchestratorEngine, tmp_path: Path):
        """Loading the same compile_commands.json twice returns the cached instance."""
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([
            {
                "directory": str(tmp_path),
                "arguments": ["clang++", str(src)],
                "file": str(src),
            }
        ]))
        db1 = engine.get_compile_db(str(cc))
        db2 = engine.get_compile_db(str(cc))
        assert db1 is db2

    def test_get_returns_none_invalid_path(self, engine: OrchestratorEngine):
        result = engine.get_compile_db("/nonexistent/path/compile_commands.json")
        assert result is None

    def test_invalidate_specific(self, engine: OrchestratorEngine, tmp_path: Path):
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([
            {
                "directory": str(tmp_path),
                "arguments": ["clang++", str(src)],
                "file": str(src),
            }
        ]))
        engine.get_compile_db(str(cc))  # populate cache
        engine.invalidate_compile_db(str(cc))
        # After invalidation, the internal dict should be empty for that path
        assert len(engine._compile_dbs) == 0

    def test_invalidate_all(self, engine: OrchestratorEngine):
        engine._compile_dbs["a"] = MagicMock()
        engine._compile_dbs["b"] = MagicMock()
        engine.invalidate_compile_db()
        assert len(engine._compile_dbs) == 0


# ====================================================================
# query_references (mocked pipeline)
# ====================================================================

class TestQueryReferences:

    async def test_no_candidates(self, engine: OrchestratorEngine):
        """No recall hits → empty results with empty confidence."""
        with patch(
            "cxxtract.orchestrator.engine.run_recall",
            AsyncMock(return_value=RecallResult(hits=[], rg_exit_code=1)),
        ), patch(
            "cxxtract.orchestrator.engine.repo.search_symbols_by_name",
            AsyncMock(return_value=[]),
        ), patch(
            "cxxtract.orchestrator.engine.repo.search_references_by_symbol",
            AsyncMock(return_value=[]),
        ):
            resp = await engine.query_references(
                SymbolQueryRequest(symbol="foo", repo_root="/tmp")
            )
        assert resp.symbol == "foo"
        assert resp.definition is None
        assert resp.references == []
        assert resp.confidence.total_candidates == 0

    async def test_with_recall_error(self, engine: OrchestratorEngine):
        """Recall error propagated to confidence warnings."""
        with patch(
            "cxxtract.orchestrator.engine.run_recall",
            AsyncMock(return_value=RecallResult(
                error="rg timed out", elapsed_ms=31000,
            )),
        ), patch(
            "cxxtract.orchestrator.engine.repo.search_symbols_by_name",
            AsyncMock(return_value=[]),
        ), patch(
            "cxxtract.orchestrator.engine.repo.search_references_by_symbol",
            AsyncMock(return_value=[]),
        ):
            resp = await engine.query_references(
                SymbolQueryRequest(symbol="foo", repo_root="/tmp")
            )
        assert any("recall" in w for w in resp.confidence.warnings)

    async def test_with_cached_results(self, engine: OrchestratorEngine, db_conn):
        """Files in cache with matching hash → verified, data returned."""
        await repo.upsert_tracked_file(
            "src/a.cpp", "ch", "fh", "ih", "comp", conn=db_conn,
        )
        await repo.upsert_symbols("src/a.cpp", [
            ExtractedSymbol(
                name="foo", qualified_name="ns::foo", kind="Function",
                line=10, col=1, extent_end_line=20,
            ),
        ], conn=db_conn)
        await repo.upsert_references("src/a.cpp", [
            ExtractedReference(symbol="ns::foo", line=30, col=5, kind="call"),
        ], conn=db_conn)

        with patch(
            "cxxtract.orchestrator.engine.run_recall",
            AsyncMock(return_value=RecallResult(
                hits=[RecallHit(file_path="src/a.cpp", line_number=10, line_text="foo")],
                rg_exit_code=0,
            )),
        ), patch.object(
            engine, "_classify_files",
            AsyncMock(return_value=(["src/a.cpp"], [], [])),
        ):
            resp = await engine.query_references(
                SymbolQueryRequest(symbol="foo", repo_root="/tmp")
            )

        assert len(resp.confidence.verified_files) == 1
        assert resp.definition is not None
        assert resp.definition.qualified_name == "ns::foo"
        assert len(resp.references) == 1


# ====================================================================
# query_definition
# ====================================================================

class TestQueryDefinition:

    async def test_no_definitions(self, engine: OrchestratorEngine):
        with patch(
            "cxxtract.orchestrator.engine.run_recall",
            AsyncMock(return_value=RecallResult(hits=[], rg_exit_code=1)),
        ), patch(
            "cxxtract.orchestrator.engine.repo.search_symbols_by_name",
            AsyncMock(return_value=[]),
        ):
            resp = await engine.query_definition(
                SymbolQueryRequest(symbol="nope", repo_root="/tmp")
            )
        assert resp.definitions == []

    async def test_finds_definition(self, engine: OrchestratorEngine, db_conn):
        await repo.upsert_tracked_file(
            "src/a.cpp", "ch", "fh", "ih", "comp", conn=db_conn,
        )
        await repo.upsert_symbols("src/a.cpp", [
            ExtractedSymbol(
                name="bar", qualified_name="ns::bar", kind="CXXMethod",
                line=5, col=3, extent_end_line=15,
            ),
        ], conn=db_conn)

        with patch(
            "cxxtract.orchestrator.engine.run_recall",
            AsyncMock(return_value=RecallResult(hits=[], rg_exit_code=1)),
        ):
            resp = await engine.query_definition(
                SymbolQueryRequest(symbol="bar", repo_root="/tmp")
            )

        assert len(resp.definitions) == 1
        assert resp.definitions[0].qualified_name == "ns::bar"


# ====================================================================
# query_call_graph
# ====================================================================

class TestQueryCallGraph:

    async def test_outgoing_edges(self, engine: OrchestratorEngine, db_conn):
        await repo.upsert_tracked_file(
            "src/a.cpp", "ch", "fh", "ih", "comp", conn=db_conn,
        )
        await repo.upsert_call_edges("src/a.cpp", [
            ExtractedCallEdge(caller="ns::foo", callee="ns::bar", line=10),
        ], conn=db_conn)

        with patch(
            "cxxtract.orchestrator.engine.run_recall",
            AsyncMock(return_value=RecallResult(hits=[], rg_exit_code=1)),
        ):
            resp = await engine.query_call_graph(
                CallGraphRequest(
                    symbol="ns::foo", repo_root="/tmp",
                    direction=CallGraphDirection.OUTGOING,
                )
            )

        assert len(resp.edges) == 1
        assert resp.edges[0].callee == "ns::bar"

    async def test_incoming_edges(self, engine: OrchestratorEngine, db_conn):
        await repo.upsert_tracked_file(
            "src/a.cpp", "ch", "fh", "ih", "comp", conn=db_conn,
        )
        await repo.upsert_call_edges("src/a.cpp", [
            ExtractedCallEdge(caller="ns::foo", callee="ns::bar", line=10),
        ], conn=db_conn)

        with patch(
            "cxxtract.orchestrator.engine.run_recall",
            AsyncMock(return_value=RecallResult(hits=[], rg_exit_code=1)),
        ):
            resp = await engine.query_call_graph(
                CallGraphRequest(
                    symbol="ns::bar", repo_root="/tmp",
                    direction=CallGraphDirection.INCOMING,
                )
            )

        assert len(resp.edges) == 1
        assert resp.edges[0].caller == "ns::foo"

    async def test_both_directions(self, engine: OrchestratorEngine, db_conn):
        await repo.upsert_tracked_file(
            "src/a.cpp", "ch", "fh", "ih", "comp", conn=db_conn,
        )
        await repo.upsert_call_edges("src/a.cpp", [
            ExtractedCallEdge(caller="ns::foo", callee="ns::bar", line=10),
            ExtractedCallEdge(caller="ns::baz", callee="ns::foo", line=20),
        ], conn=db_conn)

        with patch(
            "cxxtract.orchestrator.engine.run_recall",
            AsyncMock(return_value=RecallResult(hits=[], rg_exit_code=1)),
        ):
            resp = await engine.query_call_graph(
                CallGraphRequest(
                    symbol="ns::foo", repo_root="/tmp",
                    direction=CallGraphDirection.BOTH,
                )
            )

        # 1 outgoing (foo->bar) + 1 incoming (baz->foo)
        assert len(resp.edges) == 2

    async def test_empty_call_graph(self, engine: OrchestratorEngine):
        with patch(
            "cxxtract.orchestrator.engine.run_recall",
            AsyncMock(return_value=RecallResult(hits=[], rg_exit_code=1)),
        ), patch(
            "cxxtract.orchestrator.engine.repo.get_call_edges_for_caller",
            AsyncMock(return_value=[]),
        ), patch(
            "cxxtract.orchestrator.engine.repo.get_call_edges_for_callee",
            AsyncMock(return_value=[]),
        ):
            resp = await engine.query_call_graph(
                CallGraphRequest(
                    symbol="ns::orphan", repo_root="/tmp",
                    direction=CallGraphDirection.BOTH,
                )
            )
        assert resp.edges == []


# ====================================================================
# query_file_symbols
# ====================================================================

class TestQueryFileSymbols:

    async def test_no_cache_no_compile_db(self, engine: OrchestratorEngine, db_conn):
        """No cached data and no compile_db → unparsed."""
        resp = await engine.query_file_symbols(
            FileSymbolsRequest(
                file_path="nonexistent.cpp", repo_root="/tmp",
            )
        )
        assert resp.symbols == []
        assert len(resp.confidence.unparsed_files) == 1

    async def test_cached_fresh(self, engine: OrchestratorEngine, db_conn, tmp_path: Path):
        """File is in cache and not stale → verified, symbols returned."""
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        fp = str(src.resolve())

        from cxxtract.cache.hasher import (
            compute_composite_hash,
            compute_content_hash,
            compute_flags_hash,
        )
        ch = compute_content_hash(fp)
        fh = compute_flags_hash(["-O2"])
        comp = compute_composite_hash(ch, "", fh)

        await repo.upsert_tracked_file(fp, ch, fh, "", comp, conn=db_conn)
        await repo.upsert_symbols(fp, [
            ExtractedSymbol(
                name="main", qualified_name="main", kind="Function",
                line=1, col=1, extent_end_line=1,
            ),
        ], conn=db_conn)

        # No compile_db → can't verify staleness beyond content hash
        resp = await engine.query_file_symbols(
            FileSymbolsRequest(file_path=fp, repo_root=str(tmp_path))
        )
        assert len(resp.symbols) == 1
        assert resp.symbols[0].qualified_name == "main"


# ====================================================================
# invalidate_cache
# ====================================================================

class TestInvalidateCache:

    async def test_invalidate_entire_cache(self, engine: OrchestratorEngine, db_conn):
        await repo.upsert_tracked_file("a.cpp", "c", "f", "i", "x", conn=db_conn)
        await repo.upsert_tracked_file("b.cpp", "c", "f", "i", "x", conn=db_conn)

        resp = await engine.invalidate_cache(
            CacheInvalidateRequest(file_paths=None)
        )
        assert resp.invalidated_files == 2
        assert "entire cache" in resp.message.lower()

    async def test_invalidate_specific_files(self, engine: OrchestratorEngine, db_conn):
        # Need to use resolved absolute paths since engine resolves them
        fp_a = str(Path("a.cpp").resolve())
        fp_b = str(Path("b.cpp").resolve())
        await repo.upsert_tracked_file(fp_a, "c", "f", "i", "x", conn=db_conn)
        await repo.upsert_tracked_file(fp_b, "c", "f", "i", "x", conn=db_conn)

        resp = await engine.invalidate_cache(
            CacheInvalidateRequest(file_paths=[fp_a])
        )
        assert resp.invalidated_files == 1
        # b.cpp should still be there
        tracked = await repo.get_tracked_file(fp_b)
        assert tracked is not None

    async def test_invalidate_nonexistent_file(self, engine: OrchestratorEngine, db_conn):
        resp = await engine.invalidate_cache(
            CacheInvalidateRequest(file_paths=["nonexistent.cpp"])
        )
        assert resp.invalidated_files == 0


# ====================================================================
# _classify_files
# ====================================================================

class TestClassifyFiles:

    async def test_no_compile_db_all_unparsed(
        self, engine: OrchestratorEngine, db_conn,
    ):
        fresh, stale, unparsed = await engine._classify_files(
            ["a.cpp", "b.cpp"], None
        )
        assert fresh == []
        assert stale == []
        assert unparsed == ["a.cpp", "b.cpp"]

    async def test_no_flags_for_file(
        self, engine: OrchestratorEngine, db_conn, tmp_path: Path,
    ):
        """File not in compile_db → unparsed."""
        compile_db = _make_compile_db({}, tmp_path)
        fresh, stale, unparsed = await engine._classify_files(
            ["a.cpp"], compile_db,
        )
        assert unparsed == ["a.cpp"]

    async def test_not_in_cache_is_stale(
        self, engine: OrchestratorEngine, db_conn, tmp_path: Path,
    ):
        """File in compile_db but not in cache → stale."""
        src = tmp_path / "a.cpp"
        src.write_text("int main() {}")
        fp = str(src.resolve())
        compile_db = _make_compile_db({fp: ["-O2"]}, tmp_path)

        fresh, stale, unparsed = await engine._classify_files(
            [fp], compile_db,
        )
        assert stale == [fp]
        assert fresh == []

    async def test_matching_hash_is_fresh(
        self, engine: OrchestratorEngine, db_conn, tmp_path: Path,
    ):
        """Cached file with matching composite hash → fresh."""
        from cxxtract.cache.hasher import (
            compute_composite_hash,
            compute_content_hash,
            compute_flags_hash,
        )

        src = tmp_path / "a.cpp"
        src.write_text("int main() {}")
        fp = str(src.resolve())

        ch = compute_content_hash(fp)
        fh = compute_flags_hash(["-O2"])
        comp = compute_composite_hash(ch, "", fh)

        await repo.upsert_tracked_file(fp, ch, fh, "", comp, conn=db_conn)

        compile_db = _make_compile_db({fp: ["-O2"]}, tmp_path)

        fresh, stale, unparsed = await engine._classify_files(
            [fp], compile_db,
        )
        assert fresh == [fp]
        assert stale == []
