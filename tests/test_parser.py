"""Tests for parser subprocess pool (v3 parse task flow)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cxxtract.models import ExtractorOutput
from cxxtract.orchestrator.compile_db import CompileEntry
from cxxtract.orchestrator.parser import (
    ParseTask,
    _parse_extractor_json,
    parse_file,
    parse_files_concurrent,
)
from cxxtract.orchestrator.workspace import RepoManifest, WorkspaceManifest


def _make_entry(file: str, directory: str, arguments: list[str] | None = None) -> CompileEntry:
    return CompileEntry(file=file, directory=directory, arguments=arguments or ["-std=c++17"])


def _make_manifest() -> WorkspaceManifest:
    return WorkspaceManifest(
        workspace_id="ws_test",
        repos=[RepoManifest(repo_id="repoA", root="repos/repoA", compile_commands="")],
        path_remaps=[],
    )


def _make_valid_output_json(file: str) -> str:
    return json.dumps(
        {
            "file": file,
            "symbols": [
                {
                    "name": "main",
                    "qualified_name": "main",
                    "kind": "Function",
                    "line": 1,
                    "col": 1,
                    "extent_end_line": 3,
                }
            ],
            "references": [],
            "call_edges": [],
            "include_deps": [],
            "success": True,
            "diagnostics": [],
        }
    )


def _mock_process(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


class TestParseExtractorJson:

    def test_valid_json(self):
        raw = _make_valid_output_json("src/main.cpp")
        result = _parse_extractor_json(raw, "main.cpp")
        assert result is not None
        assert isinstance(result, ExtractorOutput)

    def test_invalid_json(self):
        assert _parse_extractor_json("bad", "main.cpp") is None


class TestParseFile:

    async def test_happy_path(self, tmp_path: Path):
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")

        task = ParseTask(
            context_id="ws_test:baseline",
            file_key="repoA:src/main.cpp",
            repo_id="repoA",
            rel_path="src/main.cpp",
            abs_path=str(src),
        )
        entry = _make_entry(str(src), str(tmp_path))
        proc = _mock_process(stdout=_make_valid_output_json(str(src)).encode(), returncode=0)

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            payload = await parse_file(
                task,
                entry,
                extractor_binary="fake-extractor",
                workspace_root=str(tmp_path),
                manifest=_make_manifest(),
            )

        assert payload is not None
        assert payload.file_key == "repoA:src/main.cpp"
        assert payload.output.symbols[0].name == "main"

    async def test_nonzero_exit(self, tmp_path: Path):
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        task = ParseTask("ws_test:baseline", "repoA:src/main.cpp", "repoA", "src/main.cpp", str(src))
        entry = _make_entry(str(src), str(tmp_path))

        proc = _mock_process(stderr=b"error", returncode=1)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            payload = await parse_file(
                task,
                entry,
                extractor_binary="fake-extractor",
                workspace_root=str(tmp_path),
                manifest=_make_manifest(),
            )

        assert payload is None

    async def test_timeout(self, tmp_path: Path):
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        task = ParseTask("ws_test:baseline", "repoA:src/main.cpp", "repoA", "src/main.cpp", str(src))
        entry = _make_entry(str(src), str(tmp_path))

        proc = _mock_process()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                payload = await parse_file(
                    task,
                    entry,
                    extractor_binary="fake-extractor",
                    workspace_root=str(tmp_path),
                    manifest=_make_manifest(),
                    timeout_s=1,
                )

        assert payload is None


class TestParseFilesConcurrent:

    async def test_multiple_files(self, tmp_path: Path):
        a = tmp_path / "a.cpp"
        b = tmp_path / "b.cpp"
        a.write_text("// a")
        b.write_text("// b")

        tasks = [
            (
                ParseTask("ws_test:baseline", "repoA:src/a.cpp", "repoA", "src/a.cpp", str(a)),
                _make_entry(str(a), str(tmp_path)),
            ),
            (
                ParseTask("ws_test:baseline", "repoA:src/b.cpp", "repoA", "src/b.cpp", str(b)),
                _make_entry(str(b), str(tmp_path)),
            ),
        ]

        def make_proc(*args, **kwargs):
            file_path = args[4]
            return _mock_process(stdout=_make_valid_output_json(file_path).encode(), returncode=0)

        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=make_proc)):
            results = await parse_files_concurrent(
                tasks,
                extractor_binary="fake-extractor",
                workspace_root=str(tmp_path),
                manifest=_make_manifest(),
                max_workers=2,
            )

        assert set(results.keys()) == {"repoA:src/a.cpp", "repoA:src/b.cpp"}
        assert all(v is not None for v in results.values())
