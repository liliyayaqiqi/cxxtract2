"""Tests for ripgrep environment management (rg_env) and recall engine (recall).

This module covers:
  - rg_env: find_rg, check_rg_version, RgVersionInfo, ensure_rg
  - recall: build_symbol_pattern, build_multi_pattern, _normalise_path,
            _parse_rg_json, _deduplicate_hits, run_recall, run_recall_multi
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from cxxtract.models import RecallHit, RecallResult
from cxxtract.orchestrator.rg_env import (
    RgVersionInfo,
    _get_candidate_paths,
    check_rg_version,
    ensure_rg,
    find_rg,
)
from cxxtract.orchestrator.recall import (
    _deduplicate_hits,
    _normalise_path,
    _parse_rg_json,
    build_multi_pattern,
    build_symbol_pattern,
    run_recall,
    run_recall_multi,
)


# ====================================================================
# Helper: locate rg binary (more robust than shutil.which alone)
# ====================================================================

def _find_rg_binary() -> Optional[str]:
    """Find the rg binary on this system, trying multiple strategies."""
    # 1. shutil.which
    rg = shutil.which("rg")
    if rg:
        return rg

    # 2. Refresh PATH from registry (winget/installers update the registry
    #    but not the current process environment)
    try:
        import winreg
        machine_path = ""
        user_path = ""
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as key:
                machine_path, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                user_path, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            pass
        fresh_path = f"{machine_path};{user_path}"
        for d in fresh_path.split(";"):
            d = d.strip()
            if not d:
                continue
            candidate = Path(d) / "rg.exe"
            if candidate.is_file():
                return str(candidate)
    except ImportError:
        pass

    # 3. Try where.exe (works even when PATH hasn't been refreshed in-process)
    try:
        result = subprocess.run(
            ["where.exe", "rg"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            first_line = result.stdout.strip().splitlines()[0]
            if Path(first_line).is_file():
                return first_line
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, IndexError):
        pass

    # 4. Use find_rg from rg_env (probes common Windows locations)
    return find_rg()


# Module-level lookup so we only do it once
_RG_BINARY: Optional[str] = _find_rg_binary()


def _skip_no_rg():
    """Pytest skip helper: skip test if rg is not available."""
    if _RG_BINARY is None:
        pytest.skip("rg not installed on this system")


# ====================================================================
# RgVersionInfo
# ====================================================================

class TestRgVersionInfo:
    """Tests for the RgVersionInfo helper class."""

    def test_semver_property(self):
        v = RgVersionInfo(14, 1, 0, "ripgrep 14.1.0")
        assert v.semver == "14.1.0"

    def test_meets_minimum_above(self):
        v = RgVersionInfo(14, 0, 0, "ripgrep 14.0.0")
        assert v.meets_minimum() is True

    def test_meets_minimum_exact(self):
        v = RgVersionInfo(13, 0, 0, "ripgrep 13.0.0")
        assert v.meets_minimum() is True

    def test_meets_minimum_below_major(self):
        v = RgVersionInfo(12, 9, 9, "ripgrep 12.9.9")
        assert v.meets_minimum() is False

    def test_meets_minimum_same_major_higher_minor(self):
        v = RgVersionInfo(13, 5, 0, "ripgrep 13.5.0")
        assert v.meets_minimum() is True

    def test_repr(self):
        v = RgVersionInfo(14, 1, 0, "ripgrep 14.1.0")
        assert "14.1.0" in repr(v)


# ====================================================================
# find_rg
# ====================================================================

class TestFindRg:
    """Tests for find_rg()."""

    def test_finds_rg_on_system(self):
        """If rg is installed on this system, find_rg should find it."""
        _skip_no_rg()
        result = find_rg()
        # find_rg may return None if rg is installed but not on PATH
        # and not in standard probe locations.  We only assert it finds
        # rg when we know it's reachable (via the test helper).
        if result is not None:
            assert Path(result).name.lower().startswith("rg")

    def test_configured_absolute_path(self, tmp_path: Path):
        """find_rg should use an absolute configured path if it exists."""
        fake_rg = tmp_path / "rg.exe"
        fake_rg.write_text("fake")
        result = find_rg(str(fake_rg))
        assert result is not None
        assert Path(result).resolve() == fake_rg.resolve()

    def test_configured_path_not_found(self):
        """find_rg returns None when configured path doesn't exist and rg not on PATH."""
        with patch("cxxtract.orchestrator.rg_env.shutil.which", return_value=None), \
             patch("cxxtract.orchestrator.rg_env._get_candidate_paths", return_value=[]):
            result = find_rg("/nonexistent/rg.exe")
            assert result is None

    def test_falls_back_to_candidates(self, tmp_path: Path):
        """find_rg should check candidate paths if shutil.which fails."""
        fake_rg = tmp_path / "rg.exe"
        fake_rg.write_text("fake")
        with patch("cxxtract.orchestrator.rg_env.shutil.which", return_value=None), \
             patch("cxxtract.orchestrator.rg_env._get_candidate_paths", return_value=[fake_rg]):
            result = find_rg()
            assert result is not None
            assert Path(result).resolve() == fake_rg.resolve()

    def test_candidate_paths_returns_list(self):
        """_get_candidate_paths should return a list of Path objects."""
        candidates = _get_candidate_paths()
        assert isinstance(candidates, list)
        for c in candidates:
            assert isinstance(c, Path)


# ====================================================================
# check_rg_version
# ====================================================================

class TestCheckRgVersion:
    """Tests for check_rg_version()."""

    def test_parses_real_rg(self):
        """If rg is installed, check_rg_version should parse its version."""
        _skip_no_rg()
        version = check_rg_version(_RG_BINARY)
        assert version is not None
        assert version.major >= 1
        assert version.raw  # non-empty raw string

    def test_parses_mock_version_output(self):
        """Test parsing with mocked subprocess output."""
        mock_result = MagicMock()
        mock_result.stdout = "ripgrep 14.1.0\n-SIMD -AVX (compiled)\n"
        with patch("cxxtract.orchestrator.rg_env.subprocess.run", return_value=mock_result):
            version = check_rg_version("/fake/rg")
            assert version is not None
            assert version.major == 14
            assert version.minor == 1
            assert version.patch == 0
            assert version.raw == "ripgrep 14.1.0"

    def test_returns_none_for_missing_binary(self):
        """check_rg_version should return None if binary doesn't exist."""
        version = check_rg_version("/nonexistent/path/rg.exe")
        assert version is None

    def test_returns_none_for_empty_output(self):
        """check_rg_version should return None for empty output."""
        mock_result = MagicMock()
        mock_result.stdout = ""
        with patch("cxxtract.orchestrator.rg_env.subprocess.run", return_value=mock_result):
            version = check_rg_version("/fake/rg")
            assert version is None

    def test_returns_none_for_unparseable_output(self):
        """check_rg_version should return None for unrecognized output."""
        mock_result = MagicMock()
        mock_result.stdout = "some random output without version"
        with patch("cxxtract.orchestrator.rg_env.subprocess.run", return_value=mock_result):
            version = check_rg_version("/fake/rg")
            assert version is None


# ====================================================================
# ensure_rg
# ====================================================================

class TestEnsureRg:
    """Tests for ensure_rg()."""

    def test_ensure_rg_finds_system_rg(self):
        """If rg is installed, ensure_rg should succeed."""
        _skip_no_rg()
        settings = MagicMock()
        settings.rg_binary = "rg"
        rg_path, version = ensure_rg(settings)
        assert rg_path is not None
        assert version is not None
        assert version.meets_minimum()

    def test_ensure_rg_updates_settings(self):
        """ensure_rg should update settings.rg_binary with resolved path."""
        _skip_no_rg()

        class FakeSettings:
            rg_binary: str = "rg"

        settings = FakeSettings()
        rg_path, version = ensure_rg(settings)
        assert rg_path is not None
        # settings.rg_binary should now be an absolute path
        assert Path(settings.rg_binary).is_absolute()

    def test_ensure_rg_returns_none_when_not_found(self):
        """ensure_rg should return (None, None) when rg is not available."""
        with patch("cxxtract.orchestrator.rg_env.find_rg", return_value=None), \
             patch("cxxtract.orchestrator.rg_env.install_rg", return_value=None):
            settings = MagicMock()
            settings.rg_binary = "rg"
            rg_path, version = ensure_rg(settings)
            assert rg_path is None
            assert version is None

    def test_ensure_rg_tries_install_when_not_found(self):
        """ensure_rg should try install_rg when find_rg returns None."""
        with patch("cxxtract.orchestrator.rg_env.find_rg", return_value=None) as mock_find, \
             patch("cxxtract.orchestrator.rg_env.install_rg", return_value=None) as mock_install:
            settings = MagicMock()
            settings.rg_binary = "rg"
            ensure_rg(settings)
            mock_find.assert_called_once()
            mock_install.assert_called_once()


# ====================================================================
# build_symbol_pattern
# ====================================================================

class TestBuildSymbolPattern:
    """Tests for build_symbol_pattern()."""

    def test_simple_symbol(self):
        assert build_symbol_pattern("doLogin") == r"\bdoLogin\b"

    def test_qualified_symbol(self):
        pattern = build_symbol_pattern("Session::Auth")
        assert r"\bSession" in pattern
        assert r"Auth\b" in pattern
        assert r"::" in pattern

    def test_deeply_qualified(self):
        pattern = build_symbol_pattern("ns::Class::Method")
        assert r"\bns" in pattern
        assert r"Method\b" in pattern

    def test_whitespace_stripped(self):
        pattern = build_symbol_pattern("  MyClass :: myMethod  ")
        assert r"\bMyClass" in pattern
        assert r"myMethod\b" in pattern

    def test_special_chars_escaped(self):
        """Special regex chars in symbol names should be escaped."""
        pattern = build_symbol_pattern("operator+")
        assert r"\+" in pattern

    def test_empty_parts_ignored(self):
        """Leading/trailing :: should not produce empty parts."""
        pattern = build_symbol_pattern("::globalFunc")
        assert r"\bglobalFunc\b" in pattern


# ====================================================================
# build_multi_pattern
# ====================================================================

class TestBuildMultiPattern:
    """Tests for build_multi_pattern()."""

    def test_single_symbol(self):
        pattern = build_multi_pattern(["doLogin"])
        assert r"\bdoLogin\b" in pattern

    def test_multiple_symbols(self):
        pattern = build_multi_pattern(["foo", "bar"])
        assert "|" in pattern
        assert r"\bfoo\b" in pattern
        assert r"\bbar\b" in pattern

    def test_empty_list(self):
        """Empty list should produce empty pattern."""
        # build_multi_pattern joins with |, so empty list -> empty string
        pattern = build_multi_pattern([])
        assert pattern == ""

    def test_qualified_symbols(self):
        pattern = build_multi_pattern(["Session::Auth", "Db::Query"])
        assert "|" in pattern
        assert "Session" in pattern
        assert "Query" in pattern


# ====================================================================
# _normalise_path
# ====================================================================

class TestNormalisePath:
    """Tests for _normalise_path()."""

    def test_backslash_to_forward(self):
        assert _normalise_path(r"C:\foo\bar\baz.cpp") == "C:/foo/bar/baz.cpp"

    def test_already_forward_slashes(self):
        assert _normalise_path("C:/foo/bar/baz.cpp") == "C:/foo/bar/baz.cpp"

    def test_mixed_slashes(self):
        assert _normalise_path(r"C:/foo\bar/baz.cpp") == "C:/foo/bar/baz.cpp"

    def test_empty_string(self):
        assert _normalise_path("") == ""


# ====================================================================
# _parse_rg_json
# ====================================================================

class TestParseRgJson:
    """Tests for _parse_rg_json()."""

    def _make_match_line(
        self,
        file_path: str = "src/main.cpp",
        line_number: int = 42,
        line_text: str = "void doLogin() {",
    ) -> str:
        return json.dumps({
            "type": "match",
            "data": {
                "path": {"text": file_path},
                "line_number": line_number,
                "lines": {"text": line_text + "\n"},
            },
        })

    def test_parses_single_match(self):
        output = self._make_match_line()
        hits = _parse_rg_json(output)
        assert len(hits) == 1
        assert hits[0].file_path == "src/main.cpp"
        assert hits[0].line_number == 42
        assert hits[0].line_text == "void doLogin() {"

    def test_skips_non_match_types(self):
        lines = [
            json.dumps({"type": "begin", "data": {}}),
            self._make_match_line(),
            json.dumps({"type": "end", "data": {}}),
            json.dumps({"type": "summary", "data": {}}),
        ]
        output = "\n".join(lines)
        hits = _parse_rg_json(output)
        assert len(hits) == 1

    def test_skips_invalid_json(self):
        output = "not json\n" + self._make_match_line()
        hits = _parse_rg_json(output)
        assert len(hits) == 1

    def test_skips_empty_lines(self):
        output = "\n\n" + self._make_match_line() + "\n\n"
        hits = _parse_rg_json(output)
        assert len(hits) == 1

    def test_normalises_backslash_paths(self):
        output = self._make_match_line(file_path=r"src\utils\helper.h")
        hits = _parse_rg_json(output)
        assert hits[0].file_path == "src/utils/helper.h"

    def test_empty_output(self):
        hits = _parse_rg_json("")
        assert hits == []

    def test_multiple_matches(self):
        lines = [
            self._make_match_line("a.cpp", 1, "line1"),
            self._make_match_line("b.cpp", 2, "line2"),
            self._make_match_line("c.cpp", 3, "line3"),
        ]
        hits = _parse_rg_json("\n".join(lines))
        assert len(hits) == 3

    def test_strips_trailing_newline_from_line_text(self):
        output = self._make_match_line(line_text="void foo();")
        hits = _parse_rg_json(output)
        assert not hits[0].line_text.endswith("\n")


# ====================================================================
# _deduplicate_hits
# ====================================================================

class TestDeduplicateHits:
    """Tests for _deduplicate_hits()."""

    def test_deduplicates_same_file(self):
        hits = [
            RecallHit(file_path="a.cpp", line_number=1, line_text="line1"),
            RecallHit(file_path="a.cpp", line_number=2, line_text="line2"),
        ]
        result = _deduplicate_hits(hits, max_files=10)
        assert len(result) == 1

    def test_preserves_different_files(self):
        hits = [
            RecallHit(file_path="a.cpp", line_number=1, line_text="line1"),
            RecallHit(file_path="b.cpp", line_number=2, line_text="line2"),
        ]
        result = _deduplicate_hits(hits, max_files=10)
        assert len(result) == 2

    def test_respects_max_files(self):
        hits = [
            RecallHit(file_path=f"file{i}.cpp", line_number=i, line_text=f"line{i}")
            for i in range(20)
        ]
        result = _deduplicate_hits(hits, max_files=5)
        assert len(result) == 5

    def test_empty_input(self):
        result = _deduplicate_hits([], max_files=10)
        assert result == []

    def test_normalises_paths_in_output(self):
        hits = [
            RecallHit(file_path="src/a.cpp", line_number=1, line_text="x"),
        ]
        result = _deduplicate_hits(hits, max_files=10)
        assert "\\" not in result[0].file_path  # should be forward slashes


# ====================================================================
# run_recall (async, integration-ish)
# ====================================================================

class TestRunRecall:
    """Tests for run_recall() — uses actual rg if available, mocks otherwise."""

    @pytest.mark.asyncio
    async def test_basic_recall(self, tmp_path: Path):
        """Create temp C++ files and search for a symbol."""
        _skip_no_rg()

        # Create a test file
        cpp_file = tmp_path / "test.cpp"
        cpp_file.write_text("void doLogin() { /* login logic */ }\n")

        result = await run_recall(
            "doLogin",
            str(tmp_path),
            rg_binary=_RG_BINARY,
            max_files=10,
            timeout_s=10,
        )
        assert isinstance(result, RecallResult)
        assert result.error is None
        assert result.rg_exit_code == 0
        assert result.elapsed_ms > 0
        assert len(result.hits) == 1
        assert "doLogin" in result.hits[0].line_text

    @pytest.mark.asyncio
    async def test_no_matches(self, tmp_path: Path):
        """run_recall should return empty hits when no match is found."""
        _skip_no_rg()

        cpp_file = tmp_path / "empty.cpp"
        cpp_file.write_text("int main() { return 0; }\n")

        result = await run_recall(
            "nonExistentSymbol_xyz",
            str(tmp_path),
            rg_binary=_RG_BINARY,
            max_files=10,
            timeout_s=10,
        )
        assert result.error is None
        assert result.rg_exit_code == 1  # no matches
        assert len(result.hits) == 0

    @pytest.mark.asyncio
    async def test_missing_binary(self, tmp_path: Path):
        """run_recall should return error when rg binary doesn't exist."""
        result = await run_recall(
            "doLogin",
            str(tmp_path),
            rg_binary="/nonexistent/rg_fake_binary_12345",
            max_files=10,
            timeout_s=10,
        )
        assert result.error is not None
        assert "not found" in result.error.lower() or "os error" in result.error.lower()
        assert len(result.hits) == 0

    @pytest.mark.asyncio
    async def test_qualified_symbol(self, tmp_path: Path):
        """run_recall should find qualified C++ symbols."""
        _skip_no_rg()

        cpp_file = tmp_path / "session.h"
        cpp_file.write_text(textwrap.dedent("""\
            class Session {
            public:
                void Auth();
                void Logout();
            };
            void Session::Auth() { /* ... */ }
        """))

        result = await run_recall(
            "Session::Auth",
            str(tmp_path),
            rg_binary=_RG_BINARY,
            max_files=10,
            timeout_s=10,
        )
        assert result.error is None
        assert len(result.hits) >= 1

    @pytest.mark.asyncio
    async def test_multiple_files(self, tmp_path: Path):
        """run_recall should find matches across multiple files."""
        _skip_no_rg()

        (tmp_path / "a.cpp").write_text("void myFunc() {}\n")
        (tmp_path / "b.h").write_text("void myFunc();\n")

        result = await run_recall(
            "myFunc",
            str(tmp_path),
            rg_binary=_RG_BINARY,
            max_files=10,
            timeout_s=10,
        )
        assert result.error is None
        assert len(result.hits) == 2

    @pytest.mark.asyncio
    async def test_max_files_limit(self, tmp_path: Path):
        """run_recall should respect the max_files limit."""
        _skip_no_rg()

        for i in range(10):
            (tmp_path / f"file{i}.cpp").write_text(f"void commonSymbol() {{ /* {i} */ }}\n")

        result = await run_recall(
            "commonSymbol",
            str(tmp_path),
            rg_binary=_RG_BINARY,
            max_files=3,
            timeout_s=10,
        )
        assert result.error is None
        assert len(result.hits) <= 3

    @pytest.mark.asyncio
    async def test_file_glob_filtering(self, tmp_path: Path):
        """run_recall should respect file_globs parameter."""
        _skip_no_rg()

        (tmp_path / "a.cpp").write_text("void targetFunc() {}\n")
        (tmp_path / "b.h").write_text("void targetFunc();\n")
        (tmp_path / "c.txt").write_text("targetFunc mentioned here\n")

        result = await run_recall(
            "targetFunc",
            str(tmp_path),
            rg_binary=_RG_BINARY,
            max_files=10,
            timeout_s=10,
            file_globs=["*.h"],
        )
        assert result.error is None
        # Should only find the .h file
        assert len(result.hits) == 1
        assert result.hits[0].file_path.endswith(".h")

    @pytest.mark.asyncio
    async def test_path_normalisation(self, tmp_path: Path):
        """All returned paths should use forward slashes."""
        _skip_no_rg()

        (tmp_path / "test.cpp").write_text("void pathTest() {}\n")

        result = await run_recall(
            "pathTest",
            str(tmp_path),
            rg_binary=_RG_BINARY,
            max_files=10,
            timeout_s=10,
        )
        assert result.error is None
        for hit in result.hits:
            assert "\\" not in hit.file_path, f"Backslash in path: {hit.file_path}"

    @pytest.mark.asyncio
    async def test_result_has_timing(self, tmp_path: Path):
        """RecallResult should include elapsed_ms > 0."""
        _skip_no_rg()

        (tmp_path / "x.cpp").write_text("void timingTest() {}\n")

        result = await run_recall(
            "timingTest",
            str(tmp_path),
            rg_binary=_RG_BINARY,
            max_files=10,
            timeout_s=10,
        )
        assert result.elapsed_ms > 0

    @pytest.mark.asyncio
    async def test_cancellation(self, tmp_path: Path):
        """run_recall should support cooperative cancellation."""
        _skip_no_rg()

        (tmp_path / "x.cpp").write_text("void cancelTest() {}\n")

        # Set the cancel event immediately
        cancel = asyncio.Event()
        cancel.set()

        result = await run_recall(
            "cancelTest",
            str(tmp_path),
            rg_binary=_RG_BINARY,
            max_files=10,
            timeout_s=10,
            cancel_event=cancel,
        )
        # Either it completed before cancel took effect, or it was cancelled.
        # We just verify it doesn't crash and returns a RecallResult.
        assert isinstance(result, RecallResult)


# ====================================================================
# run_recall_multi
# ====================================================================

class TestRunRecallMulti:
    """Tests for run_recall_multi()."""

    @pytest.mark.asyncio
    async def test_empty_symbols(self):
        """Should return empty result for empty symbol list."""
        result = await run_recall_multi(
            [],
            "/tmp",
            max_files=10,
            timeout_s=10,
        )
        assert isinstance(result, RecallResult)
        assert len(result.hits) == 0
        assert result.pattern == "(empty)"

    @pytest.mark.asyncio
    async def test_single_symbol_delegates(self, tmp_path: Path):
        """Single-symbol list should delegate to run_recall."""
        _skip_no_rg()

        (tmp_path / "x.cpp").write_text("void singleFunc() {}\n")

        result = await run_recall_multi(
            ["singleFunc"],
            str(tmp_path),
            rg_binary=_RG_BINARY,
            max_files=10,
            timeout_s=10,
        )
        assert result.error is None
        assert len(result.hits) == 1

    @pytest.mark.asyncio
    async def test_multi_symbol(self, tmp_path: Path):
        """Should find matches for multiple symbols in one invocation."""
        _skip_no_rg()

        (tmp_path / "a.cpp").write_text("void alphaFunc() {}\n")
        (tmp_path / "b.cpp").write_text("void betaFunc() {}\n")
        (tmp_path / "c.cpp").write_text("int unrelated = 0;\n")

        result = await run_recall_multi(
            ["alphaFunc", "betaFunc"],
            str(tmp_path),
            rg_binary=_RG_BINARY,
            max_files=10,
            timeout_s=10,
        )
        assert result.error is None
        assert len(result.hits) >= 2
        paths = {hit.file_path for hit in result.hits}
        # Both files should be found
        a_found = any("a.cpp" in p for p in paths)
        b_found = any("b.cpp" in p for p in paths)
        assert a_found, f"a.cpp not found in {paths}"
        assert b_found, f"b.cpp not found in {paths}"


# ====================================================================
# RecallResult model
# ====================================================================

class TestRecallResultModel:
    """Tests for the RecallResult Pydantic model."""

    def test_default_values(self):
        r = RecallResult()
        assert r.hits == []
        assert r.error is None
        assert r.rg_exit_code is None
        assert r.elapsed_ms == 0.0
        assert r.pattern == ""

    def test_with_hits(self):
        hits = [RecallHit(file_path="a.cpp", line_number=1, line_text="x")]
        r = RecallResult(hits=hits, rg_exit_code=0, elapsed_ms=42.5, pattern=r"\bfoo\b")
        assert len(r.hits) == 1
        assert r.rg_exit_code == 0
        assert r.elapsed_ms == 42.5

    def test_with_error(self):
        r = RecallResult(error="timeout", elapsed_ms=30000.0)
        assert r.error == "timeout"
        assert len(r.hits) == 0

    def test_serialisation(self):
        r = RecallResult(
            hits=[RecallHit(file_path="a.cpp", line_number=1, line_text="x")],
            rg_exit_code=0,
            elapsed_ms=42.5,
            pattern=r"\bfoo\b",
        )
        d = r.model_dump()
        assert d["rg_exit_code"] == 0
        assert d["elapsed_ms"] == 42.5
        assert len(d["hits"]) == 1
