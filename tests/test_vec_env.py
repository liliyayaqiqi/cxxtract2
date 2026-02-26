"""Tests for sqlite-vec auto-detection/auto-install environment manager."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from cxxtract.config import Settings
from cxxtract.orchestrator.vec_env import ensure_sqlite_vec, find_sqlite_vec


def test_find_sqlite_vec_in_default_bin(tmp_path: Path):
    dll = tmp_path / "bin" / "sqlite_vec.dll"
    dll.parent.mkdir(parents=True, exist_ok=True)
    dll.write_bytes(b"x")
    with patch("cxxtract.orchestrator.vec_env.default_sqlite_vec_path", return_value=dll):
        found = find_sqlite_vec()
    assert found is not None
    assert Path(found).resolve() == dll.resolve()


def test_ensure_sqlite_vec_disabled(tmp_path: Path):
    settings = Settings(enable_vector_features=False)
    assert ensure_sqlite_vec(settings) is None


def test_ensure_sqlite_vec_uses_found_path(tmp_path: Path):
    settings = Settings(enable_vector_features=True)
    expected = str((tmp_path / "sqlite_vec.dll").resolve())

    with patch("cxxtract.orchestrator.vec_env.find_sqlite_vec", return_value=expected), patch(
        "cxxtract.orchestrator.vec_env.install_sqlite_vec", return_value=None
    ):
        resolved = ensure_sqlite_vec(settings)

    assert resolved == expected


def test_ensure_sqlite_vec_installs_when_missing(tmp_path: Path):
    settings = Settings(enable_vector_features=True)
    installed = str((tmp_path / "sqlite_vec.dll").resolve())

    with patch("cxxtract.orchestrator.vec_env.find_sqlite_vec", return_value=None), patch(
        "cxxtract.orchestrator.vec_env.install_sqlite_vec", return_value=installed
    ):
        resolved = ensure_sqlite_vec(settings)

    assert resolved == installed


def test_ensure_sqlite_vec_returns_none_when_unavailable():
    settings = Settings(enable_vector_features=True)
    with patch("cxxtract.orchestrator.vec_env.find_sqlite_vec", return_value=None), patch(
        "cxxtract.orchestrator.vec_env.install_sqlite_vec", return_value=None
    ):
        resolved = ensure_sqlite_vec(settings)
    assert resolved is None
