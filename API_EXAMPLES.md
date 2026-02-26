# API Examples (v4)

## Query References

```json
POST /query/references
{
  "workspace_id": "ws_main",
  "symbol": "ns::foo",
  "analysis_context": {"mode": "baseline"},
  "scope": {"entry_repos": ["repoA"], "max_repo_hops": 2}
}
```

## Query Definition

```json
POST /query/definition
{
  "workspace_id": "ws_main",
  "symbol": "ns::foo"
}
```

## Query Call Graph

```json
POST /query/call-graph
{
  "workspace_id": "ws_main",
  "symbol": "ns::foo",
  "direction": "both"
}
```

## Query File Symbols

```json
POST /query/file-symbols
{
  "workspace_id": "ws_main",
  "file_key": "repoA:src/main.cpp"
}
```

## Explore List Candidates

```json
POST /explore/list-candidates
{
  "workspace_id": "ws_main",
  "symbol": "ns::foo",
  "analysis_context": {"mode": "baseline"},
  "scope": {"entry_repos": ["repoA"], "max_repo_hops": 2},
  "max_files": 120,
  "include_rg": true
}
```

## Explore Classify Freshness

```json
POST /explore/classify-freshness
{
  "workspace_id": "ws_main",
  "candidate_file_keys": ["repoA:src/main.cpp", "repoB:lib/foo.h"]
}
```

## Explore Parse File

```json
POST /explore/parse-file
{
  "workspace_id": "ws_main",
  "file_keys": ["repoA:src/main.cpp"],
  "max_parse_workers": 2,
  "timeout_s": 120,
  "skip_if_fresh": true
}
```

## Explore Fetch References

```json
POST /explore/fetch-references
{
  "workspace_id": "ws_main",
  "symbol": "ns::foo",
  "candidate_file_keys": ["repoA:src/main.cpp"]
}
```

## Explore Read File

```json
POST /explore/read-file
{
  "workspace_id": "ws_main",
  "file_key": "repoA:src/main.cpp",
  "start_line": 1,
  "end_line": 200,
  "max_bytes": 65536
}
```

## Invalidate Cache

```json
POST /cache/invalidate
{
  "workspace_id": "ws_main",
  "context_id": "ws_main:baseline",
  "file_keys": ["repoA:src/main.cpp"]
}
```

## Sync Single Repo to Exact SHA

```json
POST /workspace/ws_main/sync-repo
{
  "repo_id": "repoA",
  "commit_sha": "40hexsha40hexsha40hexsha40hexsha40hex",
  "branch": "feature/x",
  "force_clean": true
}
```

## Sync Batch

```json
POST /workspace/ws_main/sync-batch
{
  "targets": [
    {"repo_id": "repoA", "commit_sha": "40hex..."},
    {"repo_id": "repoB", "commit_sha": "40hex...", "branch": "main"}
  ]
}
```

## Sync All Repos Declared In Manifest

```json
POST /workspace/ws_main/sync-all-repos
{
  "force_clean": true
}
```

## Upsert Commit Diff Summary Embedding

```json
POST /commit-diff-summaries/upsert
{
  "workspace_id": "ws_main",
  "repo_id": "repoA",
  "commit_sha": "40hex...",
  "branch": "main",
  "summary_text": "Merged diff summary ...",
  "embedding_model": "text-embedding-3-large",
  "embedding": [0.01, -0.02],
  "metadata": {"mr_iid": 123}
}
```

## Search Commit Diff Summaries

```json
POST /commit-diff-summaries/search
{
  "query_embedding": [0.02, -0.01],
  "top_k": 10,
  "workspace_id": "ws_main",
  "repo_ids": ["repoA"],
  "branches": ["main"]
}
```

## Legacy Fields Removed

The following request fields are no longer accepted and now return `422`:
- `repo_root`
- `file_path`
- `file_paths`
