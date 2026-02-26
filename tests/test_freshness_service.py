"""Tests for freshness classification and parse scheduling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cxxtract.config import Settings
from cxxtract.orchestrator.compile_db import CompilationDatabase
from cxxtract.orchestrator.services.freshness_service import FreshnessService
from cxxtract.orchestrator.workspace import WorkspaceManifest


class _NoopWriter:
    queue_depth = 0
    lag_ms = 0.0

    async def enqueue(self, payload) -> None:
        return

    async def flush(self) -> None:
        return


@pytest.mark.asyncio
async def test_classify_uses_fallback_compile_entry_for_header(tmp_path: Path, db_conn):
    workspace_root = tmp_path
    repo_root = workspace_root / "repos" / "repoA"
    src_dir = repo_root / "src"
    build_dir = repo_root / "build"
    src_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)

    header = src_dir / "webrtc_connection.h"
    source = src_dir / "webrtc_connection.cc"
    header.write_text("struct webrtc_connection_t {};")
    source.write_text('#include "webrtc_connection.h"\nwebrtc_connection_t x;')

    compile_db_path = build_dir / "compile_commands.json"
    compile_db_path.write_text(
        json.dumps(
            [
                {
                    "directory": str(repo_root),
                    "arguments": ["clang++", "-std=c++20", str(source)],
                    "file": str(source),
                }
            ]
        ),
        encoding="utf-8",
    )

    manifest = WorkspaceManifest.model_validate(
        {
            "workspace_id": "ws_main",
            "repos": [
                {
                    "repo_id": "repoA",
                    "root": "repos/repoA",
                    "compile_commands": "repos/repoA/build/compile_commands.json",
                    "default_branch": "main",
                    "depends_on": [],
                }
            ],
            "path_remaps": [],
        }
    )
    cdb = CompilationDatabase.load(compile_db_path, repo_id="repoA", repo_root=str(repo_root))

    svc = FreshnessService(Settings(db_path=":memory:", extractor_binary="fake"), _NoopWriter())
    fresh, stale, unparsed, tasks = await svc.classify(
        context_id="ws_main:baseline",
        file_keys=["repoA:src/webrtc_connection.h"],
        compile_dbs={"repoA": cdb},
        workspace_root=str(workspace_root),
        manifest=manifest,
    )

    assert fresh == []
    assert stale == ["repoA:src/webrtc_connection.h"]
    assert unparsed == []
    assert len(tasks) == 1
    parse_task, compile_entry = tasks[0]
    assert parse_task.file_key == "repoA:src/webrtc_connection.h"
    assert Path(compile_entry.file).name == "webrtc_connection.cc"
