"""Tests for orchestration engine (v3-only contracts)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cxxtract.cache import repository as repo
from cxxtract.cache.hasher import (
    compute_composite_hash,
    compute_content_hash,
    compute_flags_hash,
    compute_includes_hash,
)
from cxxtract.config import Settings
from cxxtract.models import (
    CacheInvalidateRequest,
    CallGraphDirection,
    CallGraphRequest,
    ExtractedCallEdge,
    ExtractedReference,
    ExtractedSymbol,
    ExtractorOutput,
    FileSymbolsRequest,
    ParsePayload,
    SymbolQueryRequest,
    WorkspaceRegisterRequest,
)
from cxxtract.orchestrator.engine import OrchestratorEngine


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


async def _setup_workspace(engine: OrchestratorEngine, tmp_path: Path, workspace_id: str = "ws_main"):
    repo_root = tmp_path / "repos" / "repoA"
    src_dir = repo_root / "src"
    build_dir = repo_root / "build"
    src_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)

    src = src_dir / "a.cpp"
    src.write_text("int foo() { return 1; }")

    compile_commands = build_dir / "compile_commands.json"
    compile_commands.write_text(
        json.dumps(
            [
                {
                    "directory": str(repo_root),
                    "arguments": ["clang++", "-std=c++17", str(src)],
                    "file": str(src),
                }
            ]
        )
    )

    manifest = tmp_path / "workspace.yaml"
    manifest.write_text(
        "\n".join(
            [
                f"workspace_id: {workspace_id}",
                "repos:",
                "  - repo_id: repoA",
                "    root: repos/repoA",
                "    compile_commands: repos/repoA/build/compile_commands.json",
                "    default_branch: main",
                "    depends_on: []",
                "path_remaps: []",
            ]
        )
    )

    await engine.register_workspace(
        WorkspaceRegisterRequest(
            workspace_id=workspace_id,
            root_path=str(tmp_path),
            manifest_path=str(manifest),
        )
    )

    file_key = "repoA:src/a.cpp"
    return workspace_id, file_key, src


async def _seed_payload(context_id: str, file_key: str, src: Path):
    output = ExtractorOutput(
        file=str(src),
        symbols=[
            ExtractedSymbol(
                name="foo",
                qualified_name="ns::foo",
                kind="Function",
                line=1,
                col=1,
                extent_end_line=1,
            )
        ],
        references=[ExtractedReference(symbol="ns::foo", line=1, col=5, kind="call")],
        call_edges=[ExtractedCallEdge(caller="ns::caller", callee="ns::foo", line=1)],
        include_deps=[],
    )

    content_hash = compute_content_hash(str(src))
    flags_hash = compute_flags_hash(["-std=c++17"])
    includes_hash = compute_includes_hash([])
    composite_hash = compute_composite_hash(content_hash, includes_hash, flags_hash)

    payload = ParsePayload(
        context_id=context_id,
        file_key=file_key,
        repo_id="repoA",
        rel_path="src/a.cpp",
        abs_path=str(src).replace("\\", "/"),
        output=output,
        resolved_include_deps=[],
        content_hash=content_hash,
        flags_hash=flags_hash,
        includes_hash=includes_hash,
        composite_hash=composite_hash,
        warnings=[],
    )
    await repo.upsert_parse_payload(payload)


class TestEngineQueries:

    async def test_query_references(self, engine: OrchestratorEngine, db_conn, tmp_path: Path):
        ws, file_key, src = await _setup_workspace(engine, tmp_path)
        await _seed_payload(f"{ws}:baseline", file_key, src)

        with patch.object(engine._candidate, "resolve_candidates", AsyncMock(return_value=([file_key], set(), []))), patch.object(
            engine._freshness, "classify", AsyncMock(return_value=([file_key], [], [], []))
        ), patch.object(engine._freshness, "parse", AsyncMock(return_value=([], [], []))):
            resp = await engine.query_references(SymbolQueryRequest(symbol="foo", workspace_id=ws))

        assert resp.definition is not None
        assert resp.definition.qualified_name == "ns::foo"
        assert len(resp.references) == 1

    async def test_query_definition(self, engine: OrchestratorEngine, db_conn, tmp_path: Path):
        ws, file_key, src = await _setup_workspace(engine, tmp_path)
        await _seed_payload(f"{ws}:baseline", file_key, src)

        with patch.object(engine._candidate, "resolve_candidates", AsyncMock(return_value=([file_key], set(), []))), patch.object(
            engine._freshness, "classify", AsyncMock(return_value=([file_key], [], [], []))
        ), patch.object(engine._freshness, "parse", AsyncMock(return_value=([], [], []))):
            resp = await engine.query_definition(SymbolQueryRequest(symbol="foo", workspace_id=ws))

        assert len(resp.definitions) == 1
        assert resp.definitions[0].qualified_name == "ns::foo"

    async def test_query_call_graph(self, engine: OrchestratorEngine, db_conn, tmp_path: Path):
        ws, file_key, src = await _setup_workspace(engine, tmp_path)
        await _seed_payload(f"{ws}:baseline", file_key, src)

        with patch.object(engine._candidate, "resolve_candidates", AsyncMock(return_value=([file_key], set(), []))), patch.object(
            engine._freshness, "classify", AsyncMock(return_value=([file_key], [], [], []))
        ), patch.object(engine._freshness, "parse", AsyncMock(return_value=([], [], []))):
            resp = await engine.query_call_graph(
                CallGraphRequest(symbol="ns::foo", workspace_id=ws, direction=CallGraphDirection.INCOMING)
            )

        assert len(resp.edges) == 1
        assert resp.edges[0].caller == "ns::caller"

    async def test_query_file_symbols(self, engine: OrchestratorEngine, db_conn, tmp_path: Path):
        ws, file_key, src = await _setup_workspace(engine, tmp_path)
        await _seed_payload(f"{ws}:baseline", file_key, src)

        with patch.object(engine._freshness, "classify", AsyncMock(return_value=([file_key], [], [], []))), patch.object(
            engine._freshness, "parse", AsyncMock(return_value=([], [], []))
        ):
            resp = await engine.query_file_symbols(FileSymbolsRequest(workspace_id=ws, file_key=file_key))

        assert len(resp.symbols) == 1
        assert resp.symbols[0].qualified_name == "ns::foo"


class TestEngineCacheInvalidation:

    async def test_invalidate_context_all(self, engine: OrchestratorEngine, db_conn, tmp_path: Path):
        ws, file_key, src = await _setup_workspace(engine, tmp_path)
        await _seed_payload(f"{ws}:baseline", file_key, src)

        resp = await engine.invalidate_cache(CacheInvalidateRequest(workspace_id=ws, file_keys=None))
        assert resp.invalidated_files == 1

    async def test_invalidate_specific_file(self, engine: OrchestratorEngine, db_conn, tmp_path: Path):
        ws, file_key, src = await _setup_workspace(engine, tmp_path)
        await _seed_payload(f"{ws}:baseline", file_key, src)

        resp = await engine.invalidate_cache(CacheInvalidateRequest(workspace_id=ws, file_keys=[file_key]))
        assert resp.invalidated_files == 1

        tracked = await repo.get_tracked_file(f"{ws}:baseline", file_key)
        assert tracked is None
