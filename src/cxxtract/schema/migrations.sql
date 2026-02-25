-- CXXtract2 SQLite Schema
-- All semantic facts are derived data cached for performance.
-- The composite hash on tracked_files drives cache invalidation.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- tracked_files: one row per source file we have ever parsed
-- ============================================================
CREATE TABLE IF NOT EXISTS tracked_files (
    file_path     TEXT    PRIMARY KEY,
    content_hash  TEXT    NOT NULL,
    flags_hash    TEXT    NOT NULL,
    includes_hash TEXT    NOT NULL DEFAULT '',
    composite_hash TEXT   NOT NULL,
    last_parsed_at TEXT   NOT NULL  -- ISO-8601 timestamp
);

-- ============================================================
-- symbols: definitions extracted from AST
-- ============================================================
CREATE TABLE IF NOT EXISTS symbols (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT    NOT NULL REFERENCES tracked_files(file_path) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    qualified_name  TEXT    NOT NULL,
    kind            TEXT    NOT NULL,  -- e.g. CXXMethod, Function, ClassDecl, VarDecl
    line            INTEGER NOT NULL,
    col             INTEGER NOT NULL,
    extent_end_line INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_symbols_file      ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_qname     ON symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbols_name      ON symbols(name);

-- ============================================================
-- references: usages of symbols across files
-- ============================================================
CREATE TABLE IF NOT EXISTS references_ (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_qualified_name TEXT NOT NULL,
    file_path   TEXT    NOT NULL REFERENCES tracked_files(file_path) ON DELETE CASCADE,
    line        INTEGER NOT NULL,
    col         INTEGER NOT NULL,
    ref_kind    TEXT    NOT NULL DEFAULT 'unknown'  -- call, read, write, addr
);

CREATE INDEX IF NOT EXISTS idx_refs_symbol   ON references_(symbol_qualified_name);
CREATE INDEX IF NOT EXISTS idx_refs_file     ON references_(file_path);

-- ============================================================
-- call_edges: directed caller -> callee edges
-- ============================================================
CREATE TABLE IF NOT EXISTS call_edges (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_qualified_name TEXT    NOT NULL,
    callee_qualified_name TEXT    NOT NULL,
    file_path             TEXT    NOT NULL REFERENCES tracked_files(file_path) ON DELETE CASCADE,
    line                  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calls_caller ON call_edges(caller_qualified_name);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON call_edges(callee_qualified_name);
CREATE INDEX IF NOT EXISTS idx_calls_file   ON call_edges(file_path);

-- ============================================================
-- include_deps: header dependency graph per translation unit
-- ============================================================
CREATE TABLE IF NOT EXISTS include_deps (
    file_path     TEXT    NOT NULL REFERENCES tracked_files(file_path) ON DELETE CASCADE,
    included_path TEXT    NOT NULL,
    depth         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (file_path, included_path)
);

-- ============================================================
-- parse_runs: audit log of extractor invocations
-- ============================================================
CREATE TABLE IF NOT EXISTS parse_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT    NOT NULL,
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    success     INTEGER NOT NULL DEFAULT 0,
    error_msg   TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_parse_runs_file ON parse_runs(file_path);
