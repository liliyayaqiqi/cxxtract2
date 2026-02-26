# CXXtract2 Multi-Repo Productization Plan (Cross-Repo Relation Resolution)

## Summary
Transform CXXtract2 from single-repo query semantics to a workspace-centric, multi-repo semantic service using a unified path identity model, hybrid recall (SQLite FTS5 + ripgrep fallback), and PR baseline+overlay analysis contexts. Keep deployment Windows-only, GitLab-first, single-org, and persist all metadata/results in one SQLite DB under the synced workspace repo.

## Product Decisions Locked
1. Scope model: Workspace graph.
2. Freshness: Event-driven pre-indexing + lazy fallback.
3. Build metadata source: Workspace manifest.
4. Scale target: Mid scale (up to ~50 repos, ~10M LOC).
5. Tenancy: Single organization.
6. PR semantics: Baseline + overlay.
7. Platform: Windows only.
8. API evolution: In-place breaking change.
9. SLO target: Query P95 under 3s.
10. SCM integration: GitLab first.
11. Recall backend: Hybrid lexical index + rg fallback.
12. Storage: Single SQLite DB in workspace.
13. Canonical path key: `repo_id + rel_path`.
14. Workspace layout: Stable repo prefix roots.
15. Dependency model: Manifest edges + inferred edges.

## Public API and Type Changes (Breaking, In-Place)

### Request model changes
1. Replace `repo_root` with `workspace_id` in all query requests.
2. Replace single `compile_commands` override with optional `repo_overrides` map keyed by `repo_id`.
3. Add `analysis_context` object on all query endpoints:
```json
{
  "mode": "baseline|pr",
  "context_id": "string",
  "base_ref": "string",
  "head_ref": "string",
  "pr_id": "string"
}
```
4. Add `scope` object:
```json
{
  "entry_repos": ["repoA", "repoB"],
  "max_repo_hops": 2
}
```

### Response model changes
1. Extend confidence envelope with repo-aware fields:
```json
{
  "verified_files": ["repoA:src/x.cpp"],
  "stale_files": ["repoB:lib/y.cpp"],
  "unparsed_files": ["repoC:inc/z.h"],
  "repo_coverage": {"repoA": 0.96, "repoB": 0.74}
}
```
2. Return canonical file keys everywhere (`repo_id:rel_path`) plus derived absolute path when requested.

### New endpoints
1. `POST /workspace/register`
2. `GET /workspace/{workspace_id}`
3. `POST /workspace/{workspace_id}/refresh-manifest`
4. `POST /webhooks/gitlab`
5. `POST /context/create-pr-overlay`
6. `POST /context/{context_id}/expire`

## Workspace Manifest (Source of Truth)
Define `workspace.yaml` at workspace root:
```yaml
workspace_id: ws_main
repos:
  - repo_id: repoA
    root: repos/repoA
    compile_commands: repos/repoA/build/compile_commands.json
    default_branch: main
    depends_on: [repoB]
  - repo_id: repoB
    root: repos/repoB
    compile_commands: repos/repoB/build/compile_commands.json
    default_branch: main
    depends_on: []
```
Use manifest edges first; add inferred edges from include and call-graph observations.

## Data Model and Path Unification (SQLite)

### Canonical identity
1. Primary file identity: `file_key = "{repo_id}:{rel_path_posix}"`.
2. Store `abs_path` as derived/non-identity field.
3. Normalize all separators to `/`, preserve case for display, use normalized lowercased lookup key for matching.

### Schema changes
1. Add tables: `workspaces`, `repos`, `analysis_contexts`, `index_jobs`.
2. Refactor `tracked_files` PK from `file_path` to `(context_id, file_key)`.
3. Refactor symbols/references/call_edges/include_deps to use `(context_id, file_key)` foreign keys.
4. Add `recall_fts` virtual table (FTS5) for lexical recall with columns `(context_id, file_key, repo_id, content)`.
5. Add indexes:
- `(context_id, qualified_name)` on symbols
- `(context_id, symbol_qualified_name)` on references
- `(context_id, caller_qualified_name)` and `(context_id, callee_qualified_name)` on call edges
- `(context_id, repo_id)` on tracked and FTS mapping tables

## Orchestrator Pipeline Enhancements

### New stage order
1. Resolve workspace and analysis context.
2. Resolve candidate repos from `scope.entry_repos` + manifest/inferred dependency closure.
3. Run recall against `recall_fts` for candidate repos/context.
4. Fallback to `rg` only for changed/unindexed files.
5. Classify freshness using composite hashes per `(context_id, file_key)`.
6. Parse stale files with repo-specific compile flags.
7. Merge overlay over baseline deterministically.
8. Return repo-aware confidence and warnings.

### Compile DB handling
1. Cache compile DB per `(workspace_id, repo_id, compile_db_path_hash)`.
2. Normalize compile DB file entries to canonical `file_key`.
3. If compile flags missing for a repo file, mark `unparsed` only for that file; never fail whole query.

## PR Baseline + Overlay Execution
1. Baseline context is maintained per workspace default branch head.
2. PR context stores only changed/affected files as overlay.
3. Query resolution order: overlay first, then baseline for non-overridden files.
4. Overlay TTL and cleanup policy:
- default TTL: 72 hours after last access
- explicit eviction endpoint and periodic GC job

## GitLab-First Eventing and Index Jobs
1. Ingest `push` and `merge_request` webhooks.
2. Create durable `index_jobs` rows in SQLite.
3. Worker loop polls jobs with lease/heartbeat fields.
4. Idempotency key: `(workspace_id, repo_id, ref, context_id, event_sha)`.
5. Retry policy: exponential backoff, max 5 attempts, dead-letter status after max retries.

## Performance and SLO Controls (P95 < 3s)
1. Query-time parse budget: cap stale parse fanout (for example max 15 files/query), report overflow in warnings.
2. Maintain high baseline coverage via event indexing to reduce cold parse latency.
3. Warm compile DB and workspace manifest caches on startup.
4. Use WAL mode, busy timeout, and prepared statements for high-concurrency reads.
5. Return partial-but-bounded answers with explicit confidence when budget exceeded.

## Product-Quality Reliability and Observability
1. Structured logs with `workspace_id`, `context_id`, `repo_id`, `request_id`.
2. Metrics:
- recall latency (FTS and rg separately)
- parse success/failure/timeout rates
- cache hit ratio by repo
- confidence verified ratio distribution
- webhook-to-index lag
3. Health endpoint extensions:
- manifest load status
- index queue depth
- oldest pending job age
- per-context freshness summary

## Test Plan and Acceptance Scenarios

### Unit tests
1. Path normalization and canonical key mapping across repos.
2. Compile DB resolution for repo-prefixed workspace roots.
3. Overlay merge precedence logic.
4. Manifest dependency closure + inferred edge merge logic.

### Integration tests
1. Cross-repo definition/reference resolution where caller and callee are in different repos.
2. Same relative path in two repos does not collide in DB.
3. Missing compile commands in one repo degrades only that repo’s coverage.
4. FTS recall plus rg fallback returns deterministic deduped candidates.

### End-to-end tests
1. GitLab MR webhook creates overlay context and query sees PR changes immediately.
2. Baseline-only query vs PR-overlay query produce expected delta.
3. Job retry and dead-letter behavior on extractor failure.
4. P95 latency under synthetic mid-scale workload meets target with confidence degradation when budgets hit.

### Migration validation
1. One-time migration from old `file_path` schema to new `file_key/context_id` schema.
2. Backfill baseline context from existing cache rows.
3. Verify old data is queryable under `baseline` after migration.

## Rollout Plan
1. Phase 1: Introduce workspace manifest, canonical `file_key`, and schema migration.
2. Phase 2: Multi-repo compile DB and query model breaking changes.
3. Phase 3: FTS recall backend + rg fallback integration.
4. Phase 4: Baseline+overlay context engine and merge semantics.
5. Phase 5: GitLab webhook ingestion and durable indexing jobs.
6. Phase 6: Performance tuning, SLO gates, and production observability.
7. Phase 7: Load test sign-off and production cutover.

## Assumptions and Defaults
1. All repos are already synced under stable prefix roots in one workspace repo.
2. SQLite remains the only metadata store for this stage.
3. Deployment remains Windows-only.
4. API clients can adopt breaking request/response changes immediately.
5. GitLab is the only required SCM integration initially.
