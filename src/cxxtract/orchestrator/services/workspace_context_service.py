"""Workspace/context and compile-db resolution service."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from cxxtract.cache import repository as repo
from cxxtract.config import Settings
from cxxtract.models import OverlayMode
from cxxtract.orchestrator.compile_db import CompilationDatabase
from cxxtract.orchestrator.workspace import WorkspaceManifest, load_workspace_manifest

logger = logging.getLogger(__name__)


class WorkspaceContextService:
    """Resolves workspace metadata, active contexts, and compile databases."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._compile_dbs: dict[str, CompilationDatabase] = {}
        self._manifests: dict[str, WorkspaceManifest] = {}

    async def resolve_workspace(self, workspace_id: str, reload_manifest: bool = False) -> tuple[dict, WorkspaceManifest]:
        ws = await repo.get_workspace(workspace_id)
        if ws is None:
            raise ValueError(f"Workspace not found: {workspace_id}")

        manifest_path = ws.get("manifest_path", "")
        if not manifest_path:
            raise ValueError(f"Workspace manifest_path is empty for {workspace_id}")

        if reload_manifest or manifest_path not in self._manifests:
            self._manifests[manifest_path] = load_workspace_manifest(manifest_path)

        mf = self._manifests[manifest_path]
        await repo.replace_workspace_repos(
            workspace_id,
            [
                {
                    "repo_id": r.repo_id,
                    "root": r.root,
                    "compile_commands": r.compile_commands,
                    "default_branch": r.default_branch,
                    "depends_on": r.depends_on,
                }
                for r in mf.repos
            ],
        )
        return ws, mf

    async def resolve_contexts(self, req) -> tuple[str, str, OverlayMode]:
        baseline = await repo.ensure_baseline_context(req.workspace_id)
        mode = req.analysis_context.mode.value
        if mode == "baseline":
            context_id = req.analysis_context.context_id or baseline
            await repo.upsert_analysis_context(context_id, req.workspace_id, "baseline")
            return context_id, baseline, OverlayMode.SPARSE

        context_id = req.analysis_context.context_id or f"{req.workspace_id}:pr:{req.analysis_context.pr_id or uuid4().hex[:8]}"
        await repo.upsert_analysis_context(context_id, req.workspace_id, "pr", base_context_id=baseline)
        ctx = await repo.get_analysis_context(context_id)
        mode_value = ctx["overlay_mode"] if ctx else OverlayMode.SPARSE.value
        return context_id, baseline, OverlayMode(mode_value)

    @staticmethod
    def candidate_repos(mf: WorkspaceManifest, entry_repos: list[str], hops: int) -> list[str]:
        repo_map = mf.repo_map()
        if not entry_repos:
            return sorted(repo_map.keys())

        queue = [(r, 0) for r in entry_repos if r in repo_map]
        result: set[str] = set()
        while queue:
            repo_id, depth = queue.pop(0)
            if repo_id in result:
                continue
            result.add(repo_id)
            if depth >= hops:
                continue
            for dep in repo_map[repo_id].depends_on:
                if dep in repo_map and dep not in result:
                    queue.append((dep, depth + 1))
        return sorted(result)

    def compile_db(
        self,
        workspace_id: str,
        workspace_root: str,
        repo_id: str,
        repo_root: str,
        compile_commands: str,
    ) -> CompilationDatabase | None:
        if not compile_commands:
            return None

        cc_path = str((Path(workspace_root) / compile_commands).resolve())
        key = f"{workspace_id}|{repo_id}|{cc_path}"
        if key not in self._compile_dbs:
            try:
                self._compile_dbs[key] = CompilationDatabase.load(
                    cc_path,
                    repo_id=repo_id,
                    repo_root=str((Path(workspace_root) / repo_root).resolve()),
                )
            except (FileNotFoundError, ValueError) as exc:
                logger.error("compile_commands load failed for %s: %s", repo_id, exc)
                return None
        return self._compile_dbs[key]

    def resolve_compile_dbs(
        self,
        workspace_id: str,
        workspace_root: str,
        manifest: WorkspaceManifest,
        repo_ids: list[str],
        repo_overrides,
    ) -> dict[str, CompilationDatabase | None]:
        repo_map = manifest.repo_map()
        resolved: dict[str, CompilationDatabase | None] = {}
        for repo_id in repo_ids:
            cfg = repo_map.get(repo_id)
            if cfg is None:
                resolved[repo_id] = None
                continue
            override = repo_overrides.get(repo_id)
            cc_path = override.compile_commands if override else cfg.compile_commands
            resolved[repo_id] = self.compile_db(workspace_id, workspace_root, repo_id, cfg.root, cc_path)
        return resolved
