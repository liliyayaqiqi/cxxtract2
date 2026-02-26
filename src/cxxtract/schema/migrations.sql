-- CXXtract2 SQLite Schema (v3 multi-repo workspace model)
-- Context-aware semantic facts with sparse overlay support.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- Workspace metadata
-- ============================================================
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id  TEXT PRIMARY KEY,
    root_path     TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
    workspace_id      TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    repo_id           TEXT NOT NULL,
    root              TEXT NOT NULL,
    compile_commands  TEXT NOT NULL DEFAULT '',
    default_branch    TEXT NOT NULL DEFAULT 'main',
    depends_on_json   TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (workspace_id, repo_id)
);

-- ============================================================
-- Analysis contexts and sparse overlay metadata
-- ============================================================
CREATE TABLE IF NOT EXISTS analysis_contexts (
    context_id          TEXT PRIMARY KEY,
    workspace_id        TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    mode                TEXT NOT NULL,  -- baseline | pr
    base_context_id     TEXT NOT NULL DEFAULT '',
    overlay_mode        TEXT NOT NULL DEFAULT 'sparse', -- full|sparse|partial_overlay
    overlay_file_count  INTEGER NOT NULL DEFAULT 0,
    overlay_row_count   INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'active', -- active|expired
    created_at          TEXT NOT NULL,
    last_accessed_at    TEXT NOT NULL,
    expires_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_context_workspace_status
    ON analysis_contexts(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_context_last_accessed
    ON analysis_contexts(last_accessed_at);

CREATE TABLE IF NOT EXISTS context_file_states (
    context_id              TEXT NOT NULL REFERENCES analysis_contexts(context_id) ON DELETE CASCADE,
    file_key                TEXT NOT NULL,
    state                   TEXT NOT NULL, -- added|modified|deleted|renamed|unchanged
    replaced_from_file_key  TEXT NOT NULL DEFAULT '',
    updated_at              TEXT NOT NULL,
    PRIMARY KEY (context_id, file_key)
);

CREATE INDEX IF NOT EXISTS idx_context_file_states_state
    ON context_file_states(context_id, state);

-- ============================================================
-- Indexed files and semantic facts
-- ============================================================
CREATE TABLE IF NOT EXISTS tracked_files (
    context_id      TEXT NOT NULL,
    file_key        TEXT NOT NULL,
    repo_id         TEXT NOT NULL,
    rel_path        TEXT NOT NULL,
    abs_path        TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    flags_hash      TEXT NOT NULL,
    includes_hash   TEXT NOT NULL DEFAULT '',
    composite_hash  TEXT NOT NULL,
    last_parsed_at  TEXT NOT NULL,
    PRIMARY KEY (context_id, file_key),
    FOREIGN KEY (context_id) REFERENCES analysis_contexts(context_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tracked_context_repo
    ON tracked_files(context_id, repo_id);
CREATE INDEX IF NOT EXISTS idx_tracked_context_abs
    ON tracked_files(context_id, abs_path);

CREATE TABLE IF NOT EXISTS symbols (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    context_id        TEXT NOT NULL,
    file_key          TEXT NOT NULL,
    name              TEXT NOT NULL,
    qualified_name    TEXT NOT NULL,
    kind              TEXT NOT NULL,
    line              INTEGER NOT NULL,
    col               INTEGER NOT NULL,
    extent_end_line   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (context_id, file_key)
        REFERENCES tracked_files(context_id, file_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_symbols_context_file
    ON symbols(context_id, file_key);
CREATE INDEX IF NOT EXISTS idx_symbols_context_qname
    ON symbols(context_id, qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbols_context_name
    ON symbols(context_id, name);

CREATE TABLE IF NOT EXISTS references_ (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    context_id             TEXT NOT NULL,
    file_key               TEXT NOT NULL,
    symbol_qualified_name  TEXT NOT NULL,
    line                   INTEGER NOT NULL,
    col                    INTEGER NOT NULL,
    ref_kind               TEXT NOT NULL DEFAULT 'unknown',
    FOREIGN KEY (context_id, file_key)
        REFERENCES tracked_files(context_id, file_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_refs_context_symbol
    ON references_(context_id, symbol_qualified_name);
CREATE INDEX IF NOT EXISTS idx_refs_context_file
    ON references_(context_id, file_key);

CREATE TABLE IF NOT EXISTS call_edges (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    context_id             TEXT NOT NULL,
    file_key               TEXT NOT NULL,
    caller_qualified_name  TEXT NOT NULL,
    callee_qualified_name  TEXT NOT NULL,
    line                   INTEGER NOT NULL,
    FOREIGN KEY (context_id, file_key)
        REFERENCES tracked_files(context_id, file_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_calls_context_caller
    ON call_edges(context_id, caller_qualified_name);
CREATE INDEX IF NOT EXISTS idx_calls_context_callee
    ON call_edges(context_id, callee_qualified_name);
CREATE INDEX IF NOT EXISTS idx_calls_context_file
    ON call_edges(context_id, file_key);

CREATE TABLE IF NOT EXISTS include_deps (
    context_id         TEXT NOT NULL,
    file_key           TEXT NOT NULL,
    included_file_key  TEXT NOT NULL DEFAULT '',
    included_abs_path  TEXT NOT NULL DEFAULT '',
    raw_path           TEXT NOT NULL DEFAULT '',
    depth              INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (context_id, file_key, raw_path),
    FOREIGN KEY (context_id, file_key)
        REFERENCES tracked_files(context_id, file_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_include_context_file
    ON include_deps(context_id, file_key);

CREATE TABLE IF NOT EXISTS parse_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    context_id    TEXT NOT NULL,
    file_key      TEXT NOT NULL,
    abs_path      TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    success       INTEGER NOT NULL DEFAULT 0,
    error_msg     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_parse_runs_context_file
    ON parse_runs(context_id, file_key);

-- ============================================================
-- Recall and indexing job metadata
-- ============================================================
CREATE VIRTUAL TABLE IF NOT EXISTS recall_fts USING fts5(
    context_id,
    file_key,
    repo_id,
    content
);

CREATE TABLE IF NOT EXISTS index_jobs (
    id                 TEXT PRIMARY KEY,
    workspace_id       TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    repo_id            TEXT NOT NULL,
    context_id         TEXT NOT NULL DEFAULT '',
    event_type         TEXT NOT NULL,
    event_sha          TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed|dead_letter
    attempts           INTEGER NOT NULL DEFAULT 0,
    max_attempts       INTEGER NOT NULL DEFAULT 5,
    lease_until        TEXT,
    last_error         TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_index_jobs_status_created
    ON index_jobs(status, created_at);
