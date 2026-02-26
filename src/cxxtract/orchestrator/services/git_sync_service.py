"""GitLab repository sync service for deterministic SHA checkout."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from cxxtract.config import Settings
from cxxtract.orchestrator.workspace import RepoManifest

logger = logging.getLogger(__name__)


class GitSyncError(RuntimeError):
    """Structured error for repo sync failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GitSyncService:
    """Synchronize a workspace repo to an exact commit SHA."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _repo_lock(self, workspace_id: str, repo_id: str) -> asyncio.Lock:
        key = (workspace_id, repo_id)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def _run_git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str],
        timeout_s: int = 120,
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            self._settings.git_binary,
            *args,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise GitSyncError("git_timeout", f"git command timed out: {' '.join(args)}") from exc

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        return int(proc.returncode or 0), stdout, stderr

    async def _ensure_cloned(self, repo_root: Path, remote_url: str, env: dict[str, str]) -> None:
        if (repo_root / ".git").exists():
            return
        repo_root.parent.mkdir(parents=True, exist_ok=True)
        rc, _out, err = await self._run_git(["clone", remote_url, str(repo_root)], env=env)
        if rc != 0:
            raise GitSyncError("clone_failed", err or "git clone failed")

    async def _ensure_clean_or_fail(self, repo_root: Path, env: dict[str, str], force_clean: bool) -> None:
        if force_clean:
            rc1, _out1, err1 = await self._run_git(["reset", "--hard"], cwd=repo_root, env=env)
            if rc1 != 0:
                raise GitSyncError("reset_failed", err1 or "git reset --hard failed")
            rc2, _out2, err2 = await self._run_git(["clean", "-fd"], cwd=repo_root, env=env)
            if rc2 != 0:
                raise GitSyncError("clean_failed", err2 or "git clean -fd failed")
            return

        rc, out, err = await self._run_git(["status", "--porcelain"], cwd=repo_root, env=env)
        if rc != 0:
            raise GitSyncError("status_failed", err or "git status failed")
        if out.strip():
            raise GitSyncError("dirty_worktree", "repository has local modifications")

    async def sync_repo(
        self,
        *,
        workspace_id: str,
        workspace_root: str,
        repo: RepoManifest,
        commit_sha: str,
        branch: str = "",
        force_clean: bool = True,
    ) -> dict[str, object]:
        """Sync one repo to exact commit SHA and return resolved result metadata."""
        if not repo.remote_url:
            raise GitSyncError("sync_not_configured", f"repo {repo.repo_id} has no remote_url")
        if not repo.token_env_var:
            raise GitSyncError("missing_token_env", f"repo {repo.repo_id} token_env_var is empty")

        token = os.environ.get(repo.token_env_var, "")
        if not token:
            raise GitSyncError("missing_token_env", f"env var {repo.token_env_var} is not set")

        # Never log token; pass via environment header only.
        env = dict(os.environ)
        env["GIT_HTTP_EXTRA_HEADER"] = f"PRIVATE-TOKEN: {token}"

        repo_root = (Path(workspace_root).resolve() / repo.root).resolve()

        warnings: list[str] = []
        lock = self._repo_lock(workspace_id, repo.repo_id)
        async with lock:
            await self._ensure_cloned(repo_root, repo.remote_url, env)
            await self._ensure_clean_or_fail(repo_root, env, force_clean)

            if branch:
                rc, _out, err = await self._run_git(["fetch", "origin", branch], cwd=repo_root, env=env)
                if rc != 0:
                    raise GitSyncError("fetch_branch_failed", err or f"git fetch origin {branch} failed")

            rc, _out, err = await self._run_git(["fetch", "origin", commit_sha], cwd=repo_root, env=env)
            if rc != 0:
                raise GitSyncError("commit_not_found", err or f"commit {commit_sha} not found")

            rc, _out, err = await self._run_git(["cat-file", "-e", f"{commit_sha}^{{commit}}"], cwd=repo_root, env=env)
            if rc != 0:
                raise GitSyncError("commit_not_found", err or f"commit {commit_sha} not found locally")

            if branch:
                rc, _out, _err = await self._run_git(
                    ["merge-base", "--is-ancestor", commit_sha, f"origin/{branch}"],
                    cwd=repo_root,
                    env=env,
                )
                if rc != 0:
                    warnings.append("sha_branch_mismatch")

            rc, _out, err = await self._run_git(["checkout", "--detach", commit_sha], cwd=repo_root, env=env)
            if rc != 0:
                raise GitSyncError("checkout_failed", err or f"git checkout --detach {commit_sha} failed")

            rc, out, err = await self._run_git(["rev-parse", "HEAD"], cwd=repo_root, env=env)
            if rc != 0:
                raise GitSyncError("resolve_head_failed", err or "git rev-parse HEAD failed")
            resolved_sha = out.strip().lower()

            return {
                "repo_root": str(repo_root).replace("\\", "/"),
                "resolved_commit_sha": resolved_sha,
                "warnings": warnings,
            }
