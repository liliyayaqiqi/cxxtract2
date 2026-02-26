"""Tests for workspace manifest validation rules (v4 sync fields)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cxxtract.orchestrator.workspace import load_workspace_manifest


def _write_manifest(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "workspace.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_manifest_accepts_sync_repo_fields(tmp_path: Path):
    path = _write_manifest(
        tmp_path,
        "\n".join(
            [
                "workspace_id: ws_main",
                "repos:",
                "  - repo_id: repoA",
                "    root: repos/repoA",
                "    compile_commands: repos/repoA/build/compile_commands.json",
                "    default_branch: main",
                "    depends_on: []",
                "    remote_url: https://gitlab.example.com/group/repoA.git",
                "    token_env_var: CXXTRACT_GITLAB_TOKEN_REPOA",
                "    project_path: group/repoA",
                f"    commit_sha: {'a' * 40}",
                "path_remaps: []",
            ]
        ),
    )

    mf = load_workspace_manifest(path)
    assert mf.repos[0].remote_url.startswith("https://")


def test_manifest_rejects_non_https_remote(tmp_path: Path):
    path = _write_manifest(
        tmp_path,
        "\n".join(
            [
                "workspace_id: ws_main",
                "repos:",
                "  - repo_id: repoA",
                "    root: repos/repoA",
                "    remote_url: http://gitlab.example.com/group/repoA.git",
                "    token_env_var: TOKEN_VAR",
                f"    commit_sha: {'a' * 40}",
                "path_remaps: []",
            ]
        ),
    )

    with pytest.raises(ValueError):
        load_workspace_manifest(path)


def test_manifest_rejects_missing_token_env_for_remote(tmp_path: Path):
    path = _write_manifest(
        tmp_path,
        "\n".join(
            [
                "workspace_id: ws_main",
                "repos:",
                "  - repo_id: repoA",
                "    root: repos/repoA",
                "    remote_url: https://gitlab.example.com/group/repoA.git",
                "path_remaps: []",
            ]
        ),
    )

    with pytest.raises(ValueError):
        load_workspace_manifest(path)


def test_manifest_rejects_missing_commit_sha_for_remote(tmp_path: Path):
    path = _write_manifest(
        tmp_path,
        "\n".join(
            [
                "workspace_id: ws_main",
                "repos:",
                "  - repo_id: repoA",
                "    root: repos/repoA",
                "    remote_url: https://gitlab.example.com/group/repoA.git",
                "    token_env_var: TOKEN_VAR",
                "path_remaps: []",
            ]
        ),
    )

    with pytest.raises(ValueError):
        load_workspace_manifest(path)


def test_manifest_rejects_duplicate_repo_id(tmp_path: Path):
    path = _write_manifest(
        tmp_path,
        "\n".join(
            [
                "workspace_id: ws_main",
                "repos:",
                "  - repo_id: repoA",
                "    root: repos/repoA",
                "  - repo_id: repoA",
                "    root: repos/repoA_dup",
                "path_remaps: []",
            ]
        ),
    )

    with pytest.raises(ValueError):
        load_workspace_manifest(path)
