# Changelog

## 2026-02-26

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
