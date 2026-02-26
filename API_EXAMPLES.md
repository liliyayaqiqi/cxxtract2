# API Examples (v3-only)

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

## Invalidate Cache

```json
POST /cache/invalidate
{
  "workspace_id": "ws_main",
  "context_id": "ws_main:baseline",
  "file_keys": ["repoA:src/main.cpp"]
}
```

## Legacy Fields Removed

The following request fields are no longer accepted and now return `422`:
- `repo_root`
- `file_path`
- `file_paths`
