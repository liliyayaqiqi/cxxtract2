"""Background worker for processing repository sync jobs."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from cxxtract.cache import repository as repo
from cxxtract.config import Settings
from cxxtract.orchestrator.services.git_sync_service import GitSyncError, GitSyncService
from cxxtract.orchestrator.services.workspace_context_service import WorkspaceContextService

logger = logging.getLogger(__name__)


class SyncWorkerService:
    """Poll and execute repo sync jobs with bounded retries."""

    def __init__(
        self,
        settings: Settings,
        workspace_context_service: WorkspaceContextService,
        git_sync_service: Optional[GitSyncService] = None,
    ) -> None:
        self._settings = settings
        self._workspace_context_service = workspace_context_service
        self._git_sync_service = git_sync_service or GitSyncService(settings)
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        count = max(1, int(self._settings.git_sync_worker_count))
        self._tasks = [
            asyncio.create_task(self._worker_loop(i), name=f"cxxtract-sync-worker-{i}")
            for i in range(count)
        ]

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []

    async def _worker_loop(self, worker_id: int) -> None:
        while self._running:
            job = await repo.lease_next_repo_sync_job()
            if not job:
                await asyncio.sleep(0.2)
                continue
            await self._process_job(worker_id, job)

    async def _process_job(self, worker_id: int, job: dict) -> None:
        job_id = str(job["id"])
        workspace_id = str(job["workspace_id"])
        repo_id = str(job["repo_id"])
        requested_sha = str(job["requested_commit_sha"])
        requested_branch = str(job.get("requested_branch", ""))
        force_clean = bool(int(job.get("requested_force_clean", 1)))
        attempts = int(job.get("attempts", 1))
        max_attempts = int(job.get("max_attempts", self._settings.git_sync_retry_attempts))

        try:
            ws, manifest = await self._workspace_context_service.resolve_workspace(workspace_id)
            repo_cfg = manifest.repo_map().get(repo_id)
            if repo_cfg is None:
                raise GitSyncError("repo_not_in_manifest", f"repo {repo_id} not found in workspace manifest")

            sync_result = await self._git_sync_service.sync_repo(
                workspace_id=workspace_id,
                workspace_root=str(ws["root_path"]),
                repo=repo_cfg,
                commit_sha=requested_sha,
                branch=requested_branch,
                force_clean=force_clean,
            )

            resolved_sha = str(sync_result.get("resolved_commit_sha", requested_sha)).lower()
            await repo.mark_repo_sync_job_done(job_id=job_id, resolved_commit_sha=resolved_sha)
            await repo.upsert_repo_sync_state(
                workspace_id=workspace_id,
                repo_id=repo_id,
                last_synced_commit_sha=resolved_sha,
                last_synced_branch=requested_branch,
                success=True,
                error_code="",
                error_message="",
            )
            logger.info(
                "repo sync done worker=%s job=%s workspace=%s repo=%s sha=%s",
                worker_id,
                job_id,
                workspace_id,
                repo_id,
                resolved_sha,
            )

        except GitSyncError as exc:
            dead_letter = attempts >= max_attempts
            await repo.mark_repo_sync_job_failed(
                job_id=job_id,
                error_code=exc.code,
                error_message=exc.message,
                dead_letter=dead_letter,
            )
            await repo.upsert_repo_sync_state(
                workspace_id=workspace_id,
                repo_id=repo_id,
                success=False,
                error_code=exc.code,
                error_message=exc.message,
            )
            logger.warning(
                "repo sync failed worker=%s job=%s workspace=%s repo=%s attempts=%s/%s code=%s",
                worker_id,
                job_id,
                workspace_id,
                repo_id,
                attempts,
                max_attempts,
                exc.code,
            )

        except Exception as exc:
            dead_letter = attempts >= max_attempts
            await repo.mark_repo_sync_job_failed(
                job_id=job_id,
                error_code="sync_unhandled",
                error_message=str(exc),
                dead_letter=dead_letter,
            )
            await repo.upsert_repo_sync_state(
                workspace_id=workspace_id,
                repo_id=repo_id,
                success=False,
                error_code="sync_unhandled",
                error_message=str(exc),
            )
            logger.exception(
                "repo sync unhandled worker=%s job=%s workspace=%s repo=%s",
                worker_id,
                job_id,
                workspace_id,
                repo_id,
            )
