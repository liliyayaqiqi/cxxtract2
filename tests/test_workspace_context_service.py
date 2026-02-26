"""Tests for workspace compile-db resolution service."""

from __future__ import annotations

import json
from pathlib import Path

from cxxtract.config import Settings
from cxxtract.orchestrator.services.workspace_context_service import WorkspaceContextService


def test_compile_db_uses_rewritten_cache(tmp_path: Path):
    workspace_root = tmp_path / "ws"
    repo_root = workspace_root / "repos" / "repoA"
    build_dir = repo_root / "build"
    src = repo_root / "src" / "main.cpp"
    build_dir.mkdir(parents=True, exist_ok=True)
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("int main() { return 0; }")

    legacy_root = tmp_path / "legacy" / "repoA"
    compile_db = build_dir / "compile_commands.json"
    compile_db.write_text(
        json.dumps(
            [
                {
                    "directory": str(legacy_root / "out" / "debug"),
                    "file": str(legacy_root / "src" / "main.cpp"),
                    "command": f'clang++ "{legacy_root / "src" / "main.cpp"}"',
                }
            ]
        ),
        encoding="utf-8",
    )

    svc = WorkspaceContextService(Settings(db_path=":memory:"))
    db = svc.compile_db(
        workspace_id="ws_main",
        workspace_root=str(workspace_root),
        repo_id="repoA",
        repo_root="repos/repoA",
        compile_commands="repos/repoA/build/compile_commands.json",
    )
    assert db is not None
    assert db.has(src)

    cache_root = workspace_root / ".cxxtract" / "compdb_cache" / "ws_main" / "repoA"
    rewritten = list(cache_root.glob("compile_commands.*.rewritten.json"))
    assert rewritten
