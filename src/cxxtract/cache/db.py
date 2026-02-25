"""SQLite connection management and schema migration via aiosqlite."""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

# Module-level connection holder (set during app lifespan)
_connection: Optional[aiosqlite.Connection] = None


def _load_migration_sql() -> str:
    """Read the DDL migration script bundled with the package."""
    schema_dir = Path(__file__).resolve().parent.parent / "schema"
    migration_file = schema_dir / "migrations.sql"
    return migration_file.read_text(encoding="utf-8")


async def init_db(db_path: str) -> aiosqlite.Connection:
    """Open (or create) the SQLite database and run migrations.

    Parameters
    ----------
    db_path:
        File-system path for the SQLite file.  Use ``:memory:`` for tests.

    Returns
    -------
    aiosqlite.Connection
        The opened, migrated connection.
    """
    global _connection

    logger.info("Opening SQLite database at %s", db_path)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row  # type: ignore[assignment]

    # Run migration DDL (idempotent thanks to IF NOT EXISTS)
    ddl = _load_migration_sql()
    await conn.executescript(ddl)
    await conn.commit()

    _connection = conn
    logger.info("Database initialized successfully")
    return conn


async def close_db() -> None:
    """Close the module-level database connection."""
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None
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
