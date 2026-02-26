"""Candidate recall and overlay merge service."""

from __future__ import annotations

from pathlib import Path

from cxxtract.cache import repository as repo
from cxxtract.config import Settings
from cxxtract.orchestrator.recall import run_recall
from cxxtract.orchestrator.workspace import WorkspaceManifest, resolve_file_key


class CandidateService:
    """Builds file candidate sets with FTS recall + rg fallback + overlay merge."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _rg_file_keys(
        self,
        symbol: str,
        workspace_root: str,
        manifest: WorkspaceManifest,
        repo_ids: list[str],
        max_files: int,
    ) -> tuple[set[str], list[str]]:
        keys: set[str] = set()
        warnings: list[str] = []
        per_repo = max(20, max_files // max(1, len(repo_ids)))
        repo_map = manifest.repo_map()

        for repo_id in repo_ids:
            repo_cfg = repo_map.get(repo_id)
            if repo_cfg is None:
                continue
            repo_root = str((Path(workspace_root) / repo_cfg.root).resolve())
            result = await run_recall(
                symbol,
                repo_root,
                rg_binary=self._settings.rg_binary,
                max_files=per_repo,
                timeout_s=self._settings.recall_timeout_s,
            )
            if result.error:
                warnings.append(f"recall[{repo_id}]: {result.error}")
            for hit in result.hits:
                resolved = resolve_file_key(workspace_root, manifest, hit.file_path)
                if resolved:
                    keys.add(resolved[0])
            if len(keys) >= max_files:
                break

        return set(list(keys)[:max_files]), warnings

    async def resolve_candidates(
        self,
        symbol: str,
        context_id: str,
        baseline_id: str,
        repo_ids: list[str],
        workspace_root: str,
        manifest: WorkspaceManifest,
        max_files: int,
    ) -> tuple[list[str], set[str], list[str]]:
        baseline = set(await repo.search_recall_candidates(baseline_id, symbol, repo_ids=repo_ids, max_files=max_files))
        overlay = (
            set(await repo.search_recall_candidates(context_id, symbol, repo_ids=repo_ids, max_files=max_files))
            if context_id != baseline_id
            else set()
        )
        rg_keys, warnings = await self._rg_file_keys(symbol, workspace_root, manifest, repo_ids, max_files)

        merged = {k: "baseline" for k in baseline | rg_keys}
        deleted: set[str] = set()
        for k in overlay:
            merged[k] = "overlay"

        if context_id != baseline_id:
            for state in await repo.get_context_file_states(context_id):
                file_key = state["file_key"]
                st = state["state"]
                if st == "deleted":
                    merged.pop(file_key, None)
                    deleted.add(file_key)
                elif st in {"modified", "added"}:
                    merged[file_key] = "overlay"
                elif st == "renamed":
                    replaced = state.get("replaced_from_file_key", "")
                    if replaced:
                        merged.pop(replaced, None)
                        deleted.add(replaced)
                    merged[file_key] = "overlay"

        return list(merged.keys())[:max_files], deleted, warnings
