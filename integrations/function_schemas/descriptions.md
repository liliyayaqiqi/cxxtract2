# CXXtract2 Agent Tool Guidance

## Decision Matrix

- Use `cxxtract.query.*` fast-track tools when symbol/function identity is known and you want direct output.
- Use `cxxtract.explore.*` atomic tools when symbol identity is ambiguous or when you need evidence-first, bounded-cost step-by-step verification.
- Use operational tools only for explicit workspace/context/cache/sync/vector state management.

## Tool Descriptions

### `cxxtract.query.references`

- Class: `aggregated`
- Side effectful: `false`
- HTTP: `POST /query/references`

What this tool does: Resolve a symbol, refresh semantic facts as needed, and return references with confidence.
When to use: Prefer this fast-track aggregated tool when the target symbol/function is already known and you want a direct answer in one round trip.
When not to use: Avoid this when symbol identity is uncertain or you need explicit stepwise evidence and bounded intermediate costs; use `cxxtract.explore.*` tools instead.
Input prerequisites: Known or plausible symbol string and workspace_id.
Expected output: Returns references plus ConfidenceEnvelope. Response model: `ReferencesResponse`.

### `cxxtract.query.definition`

- Class: `aggregated`
- Side effectful: `false`
- HTTP: `POST /query/definition`

What this tool does: Resolve one or more symbol definitions in a single fast-track call.
When to use: Prefer this fast-track aggregated tool when the target symbol/function is already known and you want a direct answer in one round trip.
When not to use: Avoid this when symbol identity is uncertain or you need explicit stepwise evidence and bounded intermediate costs; use `cxxtract.explore.*` tools instead.
Input prerequisites: Known or plausible symbol string and workspace_id.
Expected output: Returns definitions plus ConfidenceEnvelope. Response model: `DefinitionResponse`.

### `cxxtract.query.call_graph`

- Class: `aggregated`
- Side effectful: `false`
- HTTP: `POST /query/call-graph`

What this tool does: Return call edges around a function symbol with one-shot orchestration.
When to use: Prefer this fast-track aggregated tool when the target symbol/function is already known and you want a direct answer in one round trip.
When not to use: Avoid this when symbol identity is uncertain or you need explicit stepwise evidence and bounded intermediate costs; use `cxxtract.explore.*` tools instead.
Input prerequisites: Qualified function symbol and workspace_id.
Expected output: Returns edges plus ConfidenceEnvelope. Response model: `CallGraphResponse`.

### `cxxtract.query.file_symbols`

- Class: `aggregated`
- Side effectful: `false`
- HTTP: `POST /query/file-symbols`

What this tool does: Return symbols for one canonical file key.
When to use: Prefer this fast-track aggregated tool when the target symbol/function is already known and you want a direct answer in one round trip.
When not to use: Avoid this when symbol identity is uncertain or you need explicit stepwise evidence and bounded intermediate costs; use `cxxtract.explore.*` tools instead.
Input prerequisites: workspace_id and canonical file_key (repo_id:rel/path).
Expected output: Returns file symbols plus ConfidenceEnvelope. Response model: `FileSymbolsResponse`.

### `cxxtract.explore.rg_search`

- Class: `atomic`
- Side effectful: `false`
- HTTP: `POST /explore/rg-search`

What this tool does: Run bounded lexical recall with evidence for candidate discovery.
When to use: Use this atomic tool in iterative exploration chains to control recall, freshness, parsing, and semantic verification step-by-step with explicit evidence and bounded Cost/Coverage envelopes.
When not to use: Avoid this for simple known-symbol lookups where a `cxxtract.query.*` fast-track tool can answer directly with lower orchestration overhead.
Input prerequisites: workspace_id and query string.
Expected output: Returns hits, EvidenceItem[], CostEnvelope, CoverageEnvelope. Response model: `RgSearchResponse`.

### `cxxtract.explore.read_file`

- Class: `atomic`
- Side effectful: `false`
- HTTP: `POST /explore/read-file`

What this tool does: Read a bounded line/byte slice from one canonical file key.
When to use: Use this atomic tool in iterative exploration chains to control recall, freshness, parsing, and semantic verification step-by-step with explicit evidence and bounded Cost/Coverage envelopes.
When not to use: Avoid this for simple known-symbol lookups where a `cxxtract.query.*` fast-track tool can answer directly with lower orchestration overhead.
Input prerequisites: workspace_id and canonical file_key.
Expected output: Returns content slice, truncation signal, CostEnvelope. Response model: `ReadFileResponse`.

### `cxxtract.explore.get_compile_command`

- Class: `atomic`
- Side effectful: `false`
- HTTP: `POST /explore/get-compile-command`

What this tool does: Inspect the effective compile command and flags hash for semantic trust checks.
When to use: Use this atomic tool in iterative exploration chains to control recall, freshness, parsing, and semantic verification step-by-step with explicit evidence and bounded Cost/Coverage envelopes.
When not to use: Avoid this for simple known-symbol lookups where a `cxxtract.query.*` fast-track tool can answer directly with lower orchestration overhead.
Input prerequisites: workspace_id and canonical file_key.
Expected output: Returns compile match_type, cwd, args, flags_hash. Response model: `GetCompileCommandResponse`.

### `cxxtract.explore.list_candidates`

- Class: `atomic`
- Side effectful: `false`
- HTTP: `POST /explore/list-candidates`

What this tool does: Generate merged candidate file keys with provenance and truncation metadata.
When to use: Use this atomic tool in iterative exploration chains to control recall, freshness, parsing, and semantic verification step-by-step with explicit evidence and bounded Cost/Coverage envelopes.
When not to use: Avoid this for simple known-symbol lookups where a `cxxtract.query.*` fast-track tool can answer directly with lower orchestration overhead.
Input prerequisites: workspace_id and target symbol.
Expected output: Returns candidates, provenance, CostEnvelope, CoverageEnvelope. Response model: `ListCandidatesResponse`.

### `cxxtract.explore.classify_freshness`

- Class: `atomic`
- Side effectful: `false`
- HTTP: `POST /explore/classify-freshness`

What this tool does: Classify candidate files into fresh/stale/unparsed and emit parse queue descriptors.
When to use: Use this atomic tool in iterative exploration chains to control recall, freshness, parsing, and semantic verification step-by-step with explicit evidence and bounded Cost/Coverage envelopes.
When not to use: Avoid this for simple known-symbol lookups where a `cxxtract.query.*` fast-track tool can answer directly with lower orchestration overhead.
Input prerequisites: workspace_id and candidate_file_keys.
Expected output: Returns freshness buckets, parse_queue, CostEnvelope, CoverageEnvelope. Response model: `ClassifyFreshnessResponse`.

### `cxxtract.explore.parse_file`

- Class: `atomic`
- Side effectful: `true`
- HTTP: `POST /explore/parse-file`

What this tool does: Run on-demand semantic parsing and persist facts through single-writer pipeline.
When to use: Use this atomic tool in iterative exploration chains to control recall, freshness, parsing, and semantic verification step-by-step with explicit evidence and bounded Cost/Coverage envelopes.
When not to use: Avoid this for simple known-symbol lookups where a `cxxtract.query.*` fast-track tool can answer directly with lower orchestration overhead.
Input prerequisites: workspace_id and file_keys selected for parsing.
Expected output: Returns parsed/failed/skipped lists, persisted_fact_rows, CostEnvelope, CoverageEnvelope. Response model: `ParseFileResponse`.
Side effects: This tool mutates service/workspace state. Call only with explicit intent and include clear rationale in your plan.

### `cxxtract.explore.fetch_symbols`

- Class: `atomic`
- Side effectful: `false`
- HTTP: `POST /explore/fetch-symbols`

What this tool does: Fetch semantic symbol rows from cache for explicit candidate sets.
When to use: Use this atomic tool in iterative exploration chains to control recall, freshness, parsing, and semantic verification step-by-step with explicit evidence and bounded Cost/Coverage envelopes.
When not to use: Avoid this for simple known-symbol lookups where a `cxxtract.query.*` fast-track tool can answer directly with lower orchestration overhead.
Input prerequisites: workspace_id and target symbol; candidate_file_keys optional but recommended.
Expected output: Returns symbol rows with evidence and Cost/Coverage envelopes. Response model: `FetchSymbolsResponse`.

### `cxxtract.explore.fetch_references`

- Class: `atomic`
- Side effectful: `false`
- HTTP: `POST /explore/fetch-references`

What this tool does: Fetch semantic reference rows from cache for explicit candidate sets.
When to use: Use this atomic tool in iterative exploration chains to control recall, freshness, parsing, and semantic verification step-by-step with explicit evidence and bounded Cost/Coverage envelopes.
When not to use: Avoid this for simple known-symbol lookups where a `cxxtract.query.*` fast-track tool can answer directly with lower orchestration overhead.
Input prerequisites: workspace_id and target symbol; candidate_file_keys optional but recommended.
Expected output: Returns references with evidence and Cost/Coverage envelopes. Response model: `FetchReferencesResponse`.

### `cxxtract.explore.fetch_call_edges`

- Class: `atomic`
- Side effectful: `false`
- HTTP: `POST /explore/fetch-call-edges`

What this tool does: Fetch call edges for explicit symbols and direction from semantic cache.
When to use: Use this atomic tool in iterative exploration chains to control recall, freshness, parsing, and semantic verification step-by-step with explicit evidence and bounded Cost/Coverage envelopes.
When not to use: Avoid this for simple known-symbol lookups where a `cxxtract.query.*` fast-track tool can answer directly with lower orchestration overhead.
Input prerequisites: workspace_id, symbol, direction, and candidate_file_keys for high precision.
Expected output: Returns call edges with evidence and Cost/Coverage envelopes. Response model: `FetchCallEdgesResponse`.

### `cxxtract.explore.get_confidence`

- Class: `atomic`
- Side effectful: `false`
- HTTP: `POST /explore/get-confidence`

What this tool does: Compute ConfidenceEnvelope from explicit verified/stale/unparsed file sets.
When to use: Use this atomic tool in iterative exploration chains to control recall, freshness, parsing, and semantic verification step-by-step with explicit evidence and bounded Cost/Coverage envelopes.
When not to use: Avoid this for simple known-symbol lookups where a `cxxtract.query.*` fast-track tool can answer directly with lower orchestration overhead.
Input prerequisites: Provide verification sets from prior exploration steps.
Expected output: Returns ConfidenceEnvelope and CoverageEnvelope. Response model: `GetConfidenceResponse`.

### `cxxtract.cache.invalidate`

- Class: `operational`
- Side effectful: `true`
- HTTP: `POST /cache/invalidate`

What this tool does: Invalidate cached semantic facts for one context or a selected file set.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: workspace_id and optional context_id/file_keys.
Expected output: Returns invalidated_files count and status message. Response model: `CacheInvalidateResponse`.
Side effects: This tool mutates service/workspace state. Call only with explicit intent and include clear rationale in your plan.

### `cxxtract.workspace.register`

- Class: `operational`
- Side effectful: `true`
- HTTP: `POST /workspace/register`

What this tool does: Register or refresh workspace manifest binding for API operations.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: workspace_id, root_path, and manifest_path.
Expected output: Returns workspace metadata and known contexts. Response model: `WorkspaceInfoResponse`.
Side effects: This tool mutates service/workspace state. Call only with explicit intent and include clear rationale in your plan.

### `cxxtract.workspace.get`

- Class: `operational`
- Side effectful: `false`
- HTTP: `GET /workspace/{workspace_id}`

What this tool does: Read workspace metadata, repos, and active contexts.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: workspace_id path parameter.
Expected output: Returns WorkspaceInfoResponse. Response model: `WorkspaceInfoResponse`.

### `cxxtract.workspace.refresh_manifest`

- Class: `operational`
- Side effectful: `true`
- HTTP: `POST /workspace/{workspace_id}/refresh-manifest`

What this tool does: Reload workspace manifest from disk and refresh repo mapping.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: workspace_id path parameter.
Expected output: Returns repos_synced count and message. Response model: `WorkspaceRefreshResponse`.
Side effects: This tool mutates service/workspace state. Call only with explicit intent and include clear rationale in your plan.

### `cxxtract.context.create_pr_overlay`

- Class: `operational`
- Side effectful: `true`
- HTTP: `POST /context/create-pr-overlay`

What this tool does: Create a PR overlay context on top of a baseline for sparse review workflows.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: workspace_id and PR/base/head identifiers.
Expected output: Returns new context_id and overlay mode stats. Response model: `ContextCreateOverlayResponse`.
Side effects: This tool mutates service/workspace state. Call only with explicit intent and include clear rationale in your plan.

### `cxxtract.context.expire`

- Class: `operational`
- Side effectful: `true`
- HTTP: `POST /context/{context_id}/expire`

What this tool does: Expire an overlay context and release associated resources.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: context_id path parameter.
Expected output: Returns expired boolean and message. Response model: `ContextExpireResponse`.
Side effects: This tool mutates service/workspace state. Call only with explicit intent and include clear rationale in your plan.

### `cxxtract.webhook.gitlab`

- Class: `operational`
- Side effectful: `true`
- HTTP: `POST /webhooks/gitlab`

What this tool does: Ingest GitLab webhook payloads and enqueue downstream sync/index work.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: event_type and payload with workspace/repo identifiers.
Expected output: Returns accepted flag and optional job ids. Response model: `WebhookGitLabResponse`.
Side effects: This tool mutates service/workspace state. Call only with explicit intent and include clear rationale in your plan.

### `cxxtract.sync.repo`

- Class: `operational`
- Side effectful: `true`
- HTTP: `POST /workspace/{workspace_id}/sync-repo`

What this tool does: Enqueue deterministic sync for one repo at exact commit SHA.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: workspace_id path parameter and repo sync request body.
Expected output: Returns RepoSyncJobResponse. Response model: `RepoSyncJobResponse`.
Side effects: This tool mutates service/workspace state. Call only with explicit intent and include clear rationale in your plan.

### `cxxtract.sync.batch`

- Class: `operational`
- Side effectful: `true`
- HTTP: `POST /workspace/{workspace_id}/sync-batch`

What this tool does: Enqueue sync jobs for multiple repos in one request.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: workspace_id path parameter and targets list.
Expected output: Returns list of created sync jobs. Response model: `RepoSyncBatchResponse`.
Side effects: This tool mutates service/workspace state. Call only with explicit intent and include clear rationale in your plan.

### `cxxtract.sync.all_repos`

- Class: `operational`
- Side effectful: `true`
- HTTP: `POST /workspace/{workspace_id}/sync-all-repos`

What this tool does: Enqueue sync jobs for all sync-enabled repos in manifest.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: workspace_id path parameter.
Expected output: Returns jobs list and skipped repos. Response model: `RepoSyncAllResponse`.
Side effects: This tool mutates service/workspace state. Call only with explicit intent and include clear rationale in your plan.

### `cxxtract.sync.job_get`

- Class: `operational`
- Side effectful: `false`
- HTTP: `GET /sync-jobs/{job_id}`

What this tool does: Read sync job status and diagnostic fields.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: job_id path parameter.
Expected output: Returns RepoSyncJobResponse. Response model: `RepoSyncJobResponse`.

### `cxxtract.sync.status`

- Class: `operational`
- Side effectful: `false`
- HTTP: `GET /workspace/{workspace_id}/repos/{repo_id}/sync-status`

What this tool does: Read latest sync status snapshot for one repository.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: workspace_id and repo_id path parameters.
Expected output: Returns RepoSyncStatusResponse. Response model: `RepoSyncStatusResponse`.

### `cxxtract.vector.upsert`

- Class: `operational`
- Side effectful: `true`
- HTTP: `POST /commit-diff-summaries/upsert`

What this tool does: Store or update commit diff summary and embedding vector.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: workspace/repo/commit identifiers, summary text, embedding payload.
Expected output: Returns stored CommitDiffSummaryRecord. Response model: `CommitDiffSummaryRecord`.
Side effects: This tool mutates service/workspace state. Call only with explicit intent and include clear rationale in your plan.

### `cxxtract.vector.search`

- Class: `operational`
- Side effectful: `false`
- HTTP: `POST /commit-diff-summaries/search`

What this tool does: Run top-k vector search over commit summary embeddings.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: query_embedding and workspace scope.
Expected output: Returns ranked commit summary hits. Response model: `CommitDiffSummarySearchResponse`.

### `cxxtract.vector.get`

- Class: `operational`
- Side effectful: `false`
- HTTP: `GET /commit-diff-summaries/{workspace_id}/{repo_id}/{commit_sha}`

What this tool does: Fetch one commit diff summary record by composite key.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: workspace_id, repo_id, commit_sha path parameters.
Expected output: Returns found flag and optional record. Response model: `CommitDiffSummaryGetResponse`.

### `cxxtract.health.get`

- Class: `operational`
- Side effectful: `false`
- HTTP: `GET /health`

What this tool does: Read service health, queue depths, tool availability, and vector status.
When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are explicitly intended by the task.
When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control operation is required.
Input prerequisites: No inputs.
Expected output: Returns HealthResponse counters and availability flags. Response model: `HealthResponse`.
