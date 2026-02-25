"""Tests for the parser subprocess pool (parser.py).

Covers:
  - _parse_extractor_json: valid JSON, invalid JSON, non-dict, validation error
  - parse_file: happy path, non-zero exit, invalid JSON, timeout, missing binary
  - parse_files_concurrent: multiple files, mixed results, semaphore limiting
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cxxtract.models import ExtractorOutput
from cxxtract.orchestrator.compile_db import CompileEntry
from cxxtract.orchestrator.parser import (
    _parse_extractor_json,
    parse_file,
    parse_files_concurrent,
)


# ====================================================================
# Helpers
# ====================================================================

def _make_entry(
    file: str = "src/main.cpp",
    directory: str = "/project",
    arguments: list[str] | None = None,
) -> CompileEntry:
    return CompileEntry(
        file=file,
        directory=directory,
        arguments=arguments or ["-std=c++17"],
    )


def _make_valid_output_json(file: str = "src/main.cpp") -> str:
    return json.dumps({
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
    })


def _mock_process(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    """Create a mock asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


# ====================================================================
# _parse_extractor_json
# ====================================================================

class TestParseExtractorJson:

    def test_valid_json(self):
        raw = _make_valid_output_json()
        result = _parse_extractor_json(raw, "main.cpp")
        assert result is not None
        assert isinstance(result, ExtractorOutput)
        assert result.file == "src/main.cpp"
        assert len(result.symbols) == 1
        assert result.symbols[0].name == "main"

    def test_invalid_json(self):
        result = _parse_extractor_json("not json at all", "main.cpp")
        assert result is None

    def test_empty_string(self):
        result = _parse_extractor_json("", "main.cpp")
        assert result is None

    def test_non_dict_json(self):
        result = _parse_extractor_json("[1, 2, 3]", "main.cpp")
        assert result is None

    def test_missing_required_fields(self):
        """JSON object without 'file' should fail validation."""
        result = _parse_extractor_json('{"symbols": []}', "main.cpp")
        assert result is None

    def test_full_output_with_all_fields(self):
        raw = json.dumps({
            "file": "a.cpp",
            "symbols": [
                {"name": "foo", "qualified_name": "ns::foo", "kind": "Function",
                 "line": 5, "col": 1, "extent_end_line": 10},
            ],
            "references": [
                {"symbol": "ns::bar", "line": 7, "col": 3, "kind": "call"},
            ],
            "call_edges": [
                {"caller": "ns::foo", "callee": "ns::bar", "line": 7},
            ],
            "include_deps": [
                {"path": "bar.h", "depth": 1},
            ],
            "success": True,
            "diagnostics": ["warning: something"],
        })
        result = _parse_extractor_json(raw, "a.cpp")
        assert result is not None
        assert len(result.symbols) == 1
        assert len(result.references) == 1
        assert len(result.call_edges) == 1
        assert len(result.include_deps) == 1
        assert result.diagnostics == ["warning: something"]


# ====================================================================
# parse_file (mocked subprocess and repository)
# ====================================================================

class TestParseFile:

    @pytest.fixture(autouse=True)
    def _mock_repo(self):
        """Mock all repository calls so parse_file doesn't need a real DB."""
        with patch("cxxtract.orchestrator.parser.repo") as mock_repo:
            mock_repo.insert_parse_run = AsyncMock(return_value=1)
            mock_repo.finish_parse_run = AsyncMock()
            mock_repo.upsert_extractor_output = AsyncMock()
            self.mock_repo = mock_repo
            yield

    async def test_happy_path(self, tmp_path: Path):
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        entry = _make_entry(str(src), str(tmp_path))
        valid_json = _make_valid_output_json(str(src))

        proc = _mock_process(stdout=valid_json.encode(), returncode=0)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await parse_file(
                str(src), entry, extractor_binary="fake-extractor"
            )

        assert result is not None
        assert isinstance(result, ExtractorOutput)
        self.mock_repo.insert_parse_run.assert_awaited_once()
        self.mock_repo.upsert_extractor_output.assert_awaited_once()
        self.mock_repo.finish_parse_run.assert_awaited_once()

    async def test_nonzero_exit_code(self, tmp_path: Path):
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        entry = _make_entry(str(src), str(tmp_path))

        proc = _mock_process(stderr=b"error: syntax error", returncode=1)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await parse_file(
                str(src), entry, extractor_binary="fake-extractor"
            )

        assert result is None
        self.mock_repo.finish_parse_run.assert_awaited_once()
        call_args = self.mock_repo.finish_parse_run.call_args
        assert call_args[1]["success"] is False

    async def test_invalid_json_output(self, tmp_path: Path):
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        entry = _make_entry(str(src), str(tmp_path))

        proc = _mock_process(stdout=b"not json", returncode=0)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await parse_file(
                str(src), entry, extractor_binary="fake-extractor"
            )

        assert result is None
        self.mock_repo.finish_parse_run.assert_awaited()
        call_args = self.mock_repo.finish_parse_run.call_args
        assert call_args[1]["success"] is False
        assert "Invalid JSON" in call_args[1]["error_msg"]

    async def test_binary_not_found(self, tmp_path: Path):
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        entry = _make_entry(str(src), str(tmp_path))

        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError("binary not found")),
        ):
            result = await parse_file(
                str(src), entry, extractor_binary="/nonexistent/extractor"
            )

        assert result is None
        self.mock_repo.finish_parse_run.assert_awaited()
        call_args = self.mock_repo.finish_parse_run.call_args
        assert call_args[1]["success"] is False

    async def test_timeout(self, tmp_path: Path):
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        entry = _make_entry(str(src), str(tmp_path))

        proc = _mock_process()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                result = await parse_file(
                    str(src), entry, extractor_binary="fake-extractor", timeout_s=1
                )

        assert result is None
        self.mock_repo.finish_parse_run.assert_awaited()

    async def test_semaphore_released_on_success(self, tmp_path: Path):
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        entry = _make_entry(str(src), str(tmp_path))
        valid_json = _make_valid_output_json(str(src))
        sem = asyncio.Semaphore(1)

        proc = _mock_process(stdout=valid_json.encode(), returncode=0)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            await parse_file(
                str(src), entry, extractor_binary="fake", semaphore=sem
            )

        # Semaphore should be released: can acquire immediately
        assert sem._value == 1

    async def test_semaphore_released_on_failure(self, tmp_path: Path):
        src = tmp_path / "main.cpp"
        src.write_text("int main() {}")
        entry = _make_entry(str(src), str(tmp_path))
        sem = asyncio.Semaphore(1)

        proc = _mock_process(stderr=b"fail", returncode=1)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            await parse_file(
                str(src), entry, extractor_binary="fake", semaphore=sem
            )

        assert sem._value == 1


# ====================================================================
# parse_files_concurrent
# ====================================================================

class TestParseFilesConcurrent:

    @pytest.fixture(autouse=True)
    def _mock_repo(self):
        with patch("cxxtract.orchestrator.parser.repo") as mock_repo:
            mock_repo.insert_parse_run = AsyncMock(return_value=1)
            mock_repo.finish_parse_run = AsyncMock()
            mock_repo.upsert_extractor_output = AsyncMock()
            self.mock_repo = mock_repo
            yield

    async def test_multiple_files(self, tmp_path: Path):
        files = []
        for name in ["a.cpp", "b.cpp"]:
            f = tmp_path / name
            f.write_text(f"// {name}")
            files.append(f)

        files_and_entries = [
            (str(f), _make_entry(str(f), str(tmp_path)))
            for f in files
        ]

        def make_proc(*args, **kwargs):
            fp = args[4]  # --file <path>
            valid_json = _make_valid_output_json(fp)
            return _mock_process(stdout=valid_json.encode(), returncode=0)

        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=make_proc)):
            results = await parse_files_concurrent(
                files_and_entries, extractor_binary="fake", max_workers=2
            )

        assert len(results) == 2
        for fp, output in results.items():
            assert output is not None

    async def test_mixed_success_failure(self, tmp_path: Path):
        ok_file = tmp_path / "ok.cpp"
        fail_file = tmp_path / "fail.cpp"
        ok_file.write_text("// ok")
        fail_file.write_text("// fail")

        files_and_entries = [
            (str(ok_file), _make_entry(str(ok_file), str(tmp_path))),
            (str(fail_file), _make_entry(str(fail_file), str(tmp_path))),
        ]

        call_count = 0

        async def mock_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call succeeds
                valid_json = _make_valid_output_json(str(ok_file))
                return _mock_process(stdout=valid_json.encode(), returncode=0)
            else:
                # Second call fails
                return _mock_process(stderr=b"error", returncode=1)

        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=mock_exec)):
            results = await parse_files_concurrent(
                files_and_entries, extractor_binary="fake", max_workers=2
            )

        assert len(results) == 2
        assert results[str(ok_file)] is not None
        assert results[str(fail_file)] is None

    async def test_empty_list(self):
        results = await parse_files_concurrent(
            [], extractor_binary="fake", max_workers=2
        )
        assert results == {}
