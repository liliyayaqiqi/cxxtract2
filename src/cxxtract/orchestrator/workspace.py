"""Workspace manifest loading and path canonicalization helpers."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Optional

import yaml
from pydantic import BaseModel, Field, model_validator

from cxxtract.models import ResolvedIncludeDep


class RepoManifest(BaseModel):
    """A repository node in the workspace manifest."""

    repo_id: str
    root: str
    compile_commands: str = ""
    default_branch: str = "main"
    depends_on: list[str] = Field(default_factory=list)
    remote_url: str = ""
    token_env_var: str = ""
    project_path: str = ""
    commit_sha: str = ""

    @model_validator(mode="after")
    def _validate_sync_fields(self) -> "RepoManifest":
        if self.remote_url:
            if not self.remote_url.lower().startswith("https://"):
                raise ValueError(f"repo {self.repo_id}: remote_url must be HTTPS")
            if not self.token_env_var:
                raise ValueError(f"repo {self.repo_id}: token_env_var is required when remote_url is set")
            if not self.commit_sha:
                raise ValueError(f"repo {self.repo_id}: commit_sha is required when remote_url is set")
            sha = self.commit_sha.strip()
            if len(sha) != 40 or any(c not in "0123456789abcdefABCDEF" for c in sha):
                raise ValueError(f"repo {self.repo_id}: commit_sha must be a 40-character hex SHA")
            self.commit_sha = sha.lower()
        return self


class PathRemap(BaseModel):
    """Maps external include prefixes to workspace repo prefixes."""

    from_prefix: str
    to_repo_id: str
    to_prefix: str


class WorkspaceManifest(BaseModel):
    """Top-level workspace manifest schema."""

    workspace_id: str
    repos: list[RepoManifest] = Field(default_factory=list)
    path_remaps: list[PathRemap] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_repo_ids(self) -> "WorkspaceManifest":
        seen: set[str] = set()
        for repo in self.repos:
            if repo.repo_id in seen:
                raise ValueError(f"duplicate repo_id in manifest: {repo.repo_id}")
            seen.add(repo.repo_id)
        return self

    def repo_map(self) -> dict[str, RepoManifest]:
        return {r.repo_id: r for r in self.repos}


def load_workspace_manifest(path: str | Path) -> WorkspaceManifest:
    """Load and validate a workspace manifest from YAML."""
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid workspace manifest structure: {p}")
    return WorkspaceManifest.model_validate(raw)


def normalize_path(path: str | Path) -> str:
    """Normalize a path string to forward slashes."""
    return str(path).replace("\\", "/")


def resolve_file_key(
    workspace_root: str | Path,
    manifest: WorkspaceManifest,
    abs_path: str | Path,
) -> Optional[tuple[str, str, str, str]]:
    """Resolve an absolute path to (file_key, repo_id, rel_path, abs_path_norm)."""
    abs_norm = normalize_path(Path(abs_path).resolve())
    root = Path(workspace_root).resolve()

    for repo in manifest.repos:
        repo_root = (root / repo.root).resolve()
        repo_root_norm = normalize_path(repo_root)
        if abs_norm.lower().startswith(repo_root_norm.lower().rstrip("/") + "/") or (
            abs_norm.lower() == repo_root_norm.lower()
        ):
            rel = normalize_path(Path(abs_norm).resolve().relative_to(repo_root))
            file_key = f"{repo.repo_id}:{rel}"
            return file_key, repo.repo_id, rel, abs_norm

    return None


def file_key_to_abs_path(
    workspace_root: str | Path,
    manifest: WorkspaceManifest,
    file_key: str,
) -> Optional[tuple[str, str, str]]:
    """Resolve canonical file key into (repo_id, rel_path, abs_path)."""
    if ":" not in file_key:
        return None
    repo_id, rel = file_key.split(":", 1)
    repo = manifest.repo_map().get(repo_id)
    if repo is None:
        return None
    abs_path = (Path(workspace_root).resolve() / repo.root / PurePosixPath(rel)).resolve()
    return repo_id, rel, normalize_path(abs_path)


def resolve_include_dep(
    workspace_root: str | Path,
    manifest: WorkspaceManifest,
    raw_path: str,
    depth: int = 1,
) -> ResolvedIncludeDep:
    """Resolve an include path to canonical workspace identity, if possible."""
    raw_norm = normalize_path(raw_path)

    # 1) Directly inside workspace repos
    direct = resolve_file_key(workspace_root, manifest, raw_norm)
    if direct:
        file_key, _, _, abs_norm = direct
        return ResolvedIncludeDep(
            raw_path=raw_norm,
            resolved_file_key=file_key,
            resolved_abs_path=abs_norm,
            resolved=True,
            depth=depth,
        )

    # 2) Path remaps from external include roots
    root = Path(workspace_root).resolve()
    repo_map = manifest.repo_map()
    for remap in manifest.path_remaps:
        from_norm = normalize_path(remap.from_prefix).rstrip("/")
        if raw_norm.lower().startswith(from_norm.lower() + "/") or raw_norm.lower() == from_norm.lower():
            suffix = raw_norm[len(from_norm) :].lstrip("/\\")
            target_repo = repo_map.get(remap.to_repo_id)
            if target_repo is None:
                continue
            remapped = (root / remap.to_prefix / PurePosixPath(suffix)).resolve()
            remapped_norm = normalize_path(remapped)
            resolved = resolve_file_key(workspace_root, manifest, remapped_norm)
            if resolved:
                file_key, _, _, abs_norm = resolved
                return ResolvedIncludeDep(
                    raw_path=raw_norm,
                    resolved_file_key=file_key,
                    resolved_abs_path=abs_norm,
                    resolved=True,
                    depth=depth,
                )
            return ResolvedIncludeDep(
                raw_path=raw_norm,
                resolved_file_key="",
                resolved_abs_path=remapped_norm,
                resolved=False,
                depth=depth,
            )

    return ResolvedIncludeDep(
        raw_path=raw_norm,
        resolved_file_key="",
        resolved_abs_path="",
        resolved=False,
        depth=depth,
    )
