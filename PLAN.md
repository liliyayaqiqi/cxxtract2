# PLAN.md v2 Refinement: Multi-Repo Production Hardening (4 Critical Fixes)

## Brief Summary
1. Keep `Workspace Manifest`, `repo_id:rel_path` canonical identity, and Baseline+Overlay PR semantics.
2. Patch four critical architecture issues: include-path hash correctness, overlay recall correctness, SQLite write contention, and overlay metadata bloat.
3. This file is the implementation-oriented specification used by the coding agent.

## Critical Fix 1: Include Hash Path Mismatch (Path Remapping / VFS)

### Design Update
1. Add `path_remaps` to `workspace.yaml`, mapping external absolute include prefixes to workspace repo source prefixes.
2. Parse engine builds a Clang VFS overlay (`-ivfsoverlay`) before extractor invocation so include resolution points back to workspace sources.
3. After extractor outputs `include_deps`, Python remaps again to canonical workspace paths before hashing/storage.
4. For remappable includes belonging to a registered repo, includes hash must use workspace source file content.
5. Unresolvable external includes are marked with `external_unresolved_include` warning and treated as limited-confidence.

### Interface Additions
```yaml
path_remaps:
  - from_prefix: "C:/vcpkg/installed/x64-windows/include"
    to_repo_id: "repoB"
    to_prefix: "repos/repoB/include"
```

```json
{"raw_path":"...","resolved_file_key":"repoB:include/x.h","resolved_abs_path":"...","resolved":true}
```

## Critical Fix 2: FTS5 + Overlay Merge Correctness

### Design Update
1. `recall_fts` is candidate recall only; it is not the final truth source.
2. Overlay merge logic is done in Python orchestrator memory, not inside SQLite MATCH logic.
3. Merge algorithm:
   1. Load baseline candidate set.
   2. Load overlay candidate set.
   3. Load overlay file states (`added|modified|deleted|renamed|unchanged`).
   4. Apply dict merge by `file_key`: delete baseline hits for tombstones, replace baseline with overlay for modified/added.
4. Only merged candidates enter hash-diff/parse pipeline.

### Schema Additions
1. Add `context_file_states(context_id, file_key, state, replaced_from_file_key)`.
2. `state` enum values: `added|modified|deleted|renamed|unchanged`.

## Critical Fix 3: SQLite Write-vs-Write Contention (Single Writer)

### Design Update
1. Enforce single-writer architecture for all DB writes.
2. Parse workers only run extractor and emit payloads; they never commit DB changes.
3. Workers push payloads to an `asyncio.Queue`; one dedicated writer task persists sequentially.
4. Writer supports micro-batch commit and finite retry.
5. `database is locked` is an observable error, not a normal behavior.

### Component Changes
1. `parser.py`: return payload and stop direct DB upsert.
2. `engine.py`: enqueue payload for persistence.
3. `writer.py`: add `SingleWriterService` lifecycle-managed in FastAPI lifespan.

## Critical Fix 4: Overlay Metadata Bloat Control

### Design Update
1. Overlay is sparse: persist only changed-file facts.
2. Deleted files are stored as tombstones only in `context_file_states`.
3. Query resolution is file-granular: overlay-first, baseline-fallback.
4. Add overlay limits: `max_overlay_files=5000`, `max_overlay_rows=2000000`.
5. On limit breach, mark context `partial_overlay`, stop bulk persist, fall back to lazy parse, return warning.
6. Enforce TTL/LRU/space-budget cleanup for stale contexts.

### Schema Additions
1. `analysis_contexts`: add `overlay_mode`, `overlay_file_count`, `overlay_row_count`, `last_accessed_at`.

## Public API / Model Refinements
1. `ConfidenceEnvelope` adds `overlay_mode` and `repo_coverage`.
2. Add `POST /context/create-pr-overlay` response with overlay stats and partial flag.
3. Extend `GET /health` with writer queue depth/lag, active context count, and overlay disk usage.

## Test Cases and Acceptance Scenarios
1. Include remap catches stale cross-repo headers instead of silently staying fresh.
2. Python overlay merge removes deleted baseline hits and overrides modified file hits.
3. 50 concurrent parse workers do not produce write-lock failures due to single-writer queue.
4. Sparse overlay avoids full duplication and enforces partial overlay mode under hard caps.

## Implementation Phases
1. Phase A: schema migration + path remap pipeline.
2. Phase B: candidate-only recall + Python overlay merge.
3. Phase C: single-writer queue adoption.
4. Phase D: sparse overlay caps + GC + observability.
5. Phase E: load/perf regression and P95 latency verification.

## Assumptions
1. Repos are synced under one workspace with stable prefixes.
2. Single SQLite DB remains the persistence layer.
3. Windows-only runtime.
4. GitLab-first eventing.
5. In-place breaking API evolution is allowed.
