# Changelog

## 2026-02-26

### Added (v4 sync + vector retrieval)
- Added manifest-driven GitLab sync fields per repo: `remote_url`, `token_env_var`, `project_path`.
- Added sync APIs:
  - `POST /workspace/{workspace_id}/sync-repo`
  - `POST /workspace/{workspace_id}/sync-batch`
  - `GET /sync-jobs/{job_id}`
  - `GET /workspace/{workspace_id}/repos/{repo_id}/sync-status`
- Added background repo sync worker with deterministic detached checkout at exact `commit_sha`.
- Added DB tables for sync state/jobs and commit summaries:
  - `repo_sync_jobs`, `repo_sync_state`, `commit_diff_summaries`
- Added sqlite-vec integration hook at DB startup with fail-fast behavior when vector features are enabled.
- Added commit-summary embedding APIs:
  - `POST /commit-diff-summaries/upsert`
  - `POST /commit-diff-summaries/search`
  - `GET /commit-diff-summaries/{workspace_id}/{repo_id}/{commit_sha}`
- Extended `/health` with sync and vector readiness metrics.

### Breaking (v3 hard cut)
- Removed all legacy single-repo request compatibility fields (`repo_root`, `file_path`, `file_paths`).
- Removed legacy parser path that wrote directly to DB.
- Removed legacy repository wrappers and implicit `legacy` context behavior.
- Engine is now v3-only and composes dedicated services for workspace/context, candidates, freshness/parse, and query reads.

### Internal refactor
- Split repository into:
  - `src/cxxtract/cache/repository_core.py`
  - `src/cxxtract/cache/repository_metrics.py`
  - `src/cxxtract/cache/repository.py` facade
- Split orchestration into:
  - `src/cxxtract/orchestrator/services/workspace_context_service.py`
  - `src/cxxtract/orchestrator/services/candidate_service.py`
  - `src/cxxtract/orchestrator/services/freshness_service.py`
  - `src/cxxtract/orchestrator/services/query_read_service.py`
