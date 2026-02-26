"""SQLite connection management and schema migration via aiosqlite."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import aiosqlite

from cxxtract.orchestrator.vec_env import default_sqlite_vec_path

logger = logging.getLogger(__name__)

# Module-level connection holder (set during app lifespan)
_connection: Optional[aiosqlite.Connection] = None
_sqlite_vec_loaded: bool = False

_SCHEMA_VERSION_V4_1 = 401
_SCHEMA_VERSION_V4_2 = 402
_SCHEMA_VERSION_V4_3 = 403
_SCHEMA_VERSION_V4_4 = 404


def _load_migration_sql() -> str:
    """Read the DDL migration script bundled with the package."""
    schema_dir = Path(__file__).resolve().parent.parent / "schema"
    migration_file = schema_dir / "migrations.sql"
    return migration_file.read_text(encoding="utf-8")


async def _get_user_version(conn: aiosqlite.Connection) -> int:
    cur = await conn.execute("PRAGMA user_version")
    row = await cur.fetchone()
    return int(row[0]) if row else 0  # type: ignore[index]


async def _set_user_version(conn: aiosqlite.Connection, version: int) -> None:
    await conn.execute(f"PRAGMA user_version = {int(version)}")


async def _column_exists(conn: aiosqlite.Connection, table: str, column: str) -> bool:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    for row in rows:
        name = str(row[1])  # type: ignore[index]
        if name == column:
            return True
    return False


async def _apply_v4_1(conn: aiosqlite.Connection) -> None:
    if not await _column_exists(conn, "repos", "remote_url"):
        await conn.execute("ALTER TABLE repos ADD COLUMN remote_url TEXT NOT NULL DEFAULT ''")
    if not await _column_exists(conn, "repos", "token_env_var"):
        await conn.execute("ALTER TABLE repos ADD COLUMN token_env_var TEXT NOT NULL DEFAULT ''")
    if not await _column_exists(conn, "repos", "project_path"):
        await conn.execute("ALTER TABLE repos ADD COLUMN project_path TEXT NOT NULL DEFAULT ''")
    await conn.commit()
    await _set_user_version(conn, _SCHEMA_VERSION_V4_1)
    await conn.commit()


async def _apply_v4_2(conn: aiosqlite.Connection) -> None:
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS repo_sync_jobs (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
            repo_id TEXT NOT NULL,
            requested_branch TEXT NOT NULL DEFAULT '',
            requested_commit_sha TEXT NOT NULL,
            requested_force_clean INTEGER NOT NULL DEFAULT 1,
            resolved_commit_sha TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_repo_sync_jobs_status_created
            ON repo_sync_jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_repo_sync_jobs_workspace_repo
            ON repo_sync_jobs(workspace_id, repo_id, created_at);

        CREATE TABLE IF NOT EXISTS repo_sync_state (
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
            repo_id TEXT NOT NULL,
            last_synced_commit_sha TEXT NOT NULL DEFAULT '',
            last_synced_branch TEXT NOT NULL DEFAULT '',
            last_success_at TEXT NOT NULL DEFAULT '',
            last_failure_at TEXT NOT NULL DEFAULT '',
            last_error_code TEXT NOT NULL DEFAULT '',
            last_error_message TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (workspace_id, repo_id)
        );
        """
    )
    if not await _column_exists(conn, "repo_sync_jobs", "requested_force_clean"):
        await conn.execute("ALTER TABLE repo_sync_jobs ADD COLUMN requested_force_clean INTEGER NOT NULL DEFAULT 1")
    await conn.commit()
    await _set_user_version(conn, _SCHEMA_VERSION_V4_2)
    await conn.commit()


async def _apply_v4_3(conn: aiosqlite.Connection) -> None:
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS commit_diff_summaries (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
            repo_id TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            branch TEXT NOT NULL DEFAULT '',
            summary_text TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding_dim INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_commit_diff_unique
            ON commit_diff_summaries(workspace_id, repo_id, commit_sha, embedding_model);
        CREATE INDEX IF NOT EXISTS idx_commit_diff_workspace_repo_branch
            ON commit_diff_summaries(workspace_id, repo_id, branch, created_at);
        """
    )
    await conn.commit()
    await _set_user_version(conn, _SCHEMA_VERSION_V4_3)
    await conn.commit()


async def _load_sqlite_vec_extension(conn: aiosqlite.Connection, extension_path: str) -> bool:
    if not extension_path:
        return False
    await conn.enable_load_extension(True)
    try:
        await conn.execute("SELECT load_extension(?)", (extension_path,))
        logger.info("sqlite-vec extension loaded from %s", extension_path)
        return True
    finally:
        await conn.enable_load_extension(False)


async def _apply_v4_4_vec(conn: aiosqlite.Connection, commit_embedding_dim: int) -> None:
    await conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS commit_diff_summary_vec
        USING vec0(
            embedding float[{int(commit_embedding_dim)}]
        )
        """
    )
    await conn.commit()
    await _set_user_version(conn, _SCHEMA_VERSION_V4_4)
    await conn.commit()


async def init_db(
    db_path: str,
    *,
    enable_vector_features: bool = False,
    commit_embedding_dim: int = 1536,
) -> aiosqlite.Connection:
    """Open (or create) the SQLite database and run migrations.

    Parameters
    ----------
    db_path:
        File-system path for the SQLite file.  Use ``:memory:`` for tests.
    enable_vector_features:
        When true, sqlite-vec must load successfully or init fails fast.
    commit_embedding_dim:
        Embedding dimension used for sqlite-vec virtual table.

    Returns
    -------
    aiosqlite.Connection
        The opened, migrated connection.
    """
    global _connection, _sqlite_vec_loaded

    logger.info("Opening SQLite database at %s", db_path)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row  # type: ignore[assignment]

    # Runtime PRAGMAs — must be set per-connection (not persisted by SQLite)
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA busy_timeout = 5000")

    # Base schema DDL (idempotent thanks to IF NOT EXISTS)
    ddl = _load_migration_sql()
    await conn.executescript(ddl)
    await conn.commit()

    version = await _get_user_version(conn)
    if version < _SCHEMA_VERSION_V4_1:
        await _apply_v4_1(conn)
        version = _SCHEMA_VERSION_V4_1
    if version < _SCHEMA_VERSION_V4_2:
        await _apply_v4_2(conn)
        version = _SCHEMA_VERSION_V4_2
    if version < _SCHEMA_VERSION_V4_3:
        await _apply_v4_3(conn)
        version = _SCHEMA_VERSION_V4_3

    _sqlite_vec_loaded = False
    if enable_vector_features:
        extension_path = str(default_sqlite_vec_path().resolve())
        try:
            _sqlite_vec_loaded = await _load_sqlite_vec_extension(conn, extension_path)
        except Exception as exc:
            await conn.close()
            raise RuntimeError(f"Failed to load sqlite-vec extension: {exc}") from exc

        if not _sqlite_vec_loaded:
            await conn.close()
            raise RuntimeError(
                "Vector features enabled but sqlite-vec extension is unavailable. "
                "Ensure bin/sqlite_vec.dll exists or disable vector features."
            )

        if version < _SCHEMA_VERSION_V4_4:
            try:
                await _apply_v4_4_vec(conn, commit_embedding_dim)
            except Exception as exc:
                await conn.close()
                raise RuntimeError(f"Failed to create sqlite-vec table: {exc}") from exc

    _connection = conn
    logger.info("Database initialized successfully")
    return conn


async def close_db() -> None:
    """Close the module-level database connection."""
    global _connection, _sqlite_vec_loaded
    if _connection is not None:
        await _connection.close()
        _connection = None
        _sqlite_vec_loaded = False
        logger.info("Database connection closed")


def get_connection() -> aiosqlite.Connection:
    """Return the current database connection.

    Raises
    ------
    RuntimeError
        If the database has not been initialised yet.
    """
    if _connection is None:
        raise RuntimeError(
            "Database not initialised. Call init_db() during app startup."
        )
    return _connection


def is_sqlite_vec_loaded() -> bool:
    """Return whether sqlite-vec extension is loaded on active connection."""
    return _sqlite_vec_loaded
