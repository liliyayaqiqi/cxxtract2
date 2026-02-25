"""Typed async CRUD operations against the SQLite facts cache."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

from cxxtract.cache.db import get_connection
from cxxtract.models import (
    ExtractedCallEdge,
    ExtractedIncludeDep,
    ExtractedReference,
    ExtractedSymbol,
    ExtractorOutput,
)

logger = logging.getLogger(__name__)


# ============================================================
# tracked_files
# ============================================================

async def upsert_tracked_file(
    file_path: str,
    content_hash: str,
    flags_hash: str,
    includes_hash: str,
    composite_hash: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
    _commit: bool = True,
) -> None:
    """Insert or update a tracked file record."""
    db = conn or get_connection()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO tracked_files
            (file_path, content_hash, flags_hash, includes_hash, composite_hash, last_parsed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_path) DO UPDATE SET
            content_hash   = excluded.content_hash,
            flags_hash     = excluded.flags_hash,
            includes_hash  = excluded.includes_hash,
            composite_hash = excluded.composite_hash,
            last_parsed_at = excluded.last_parsed_at
        """,
        (file_path, content_hash, flags_hash, includes_hash, composite_hash, now),
    )
    if _commit:
        await db.commit()


async def get_tracked_file(
    file_path: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[dict[str, Any]]:
    """Return the tracked file row, or None if not present."""
    db = conn or get_connection()
    cursor = await db.execute(
        "SELECT * FROM tracked_files WHERE file_path = ?",
        (file_path,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return dict(row)  # type: ignore[arg-type]


async def get_composite_hash(
    file_path: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[str]:
    """Return the cached composite hash for a file, or None."""
    db = conn or get_connection()
    cursor = await db.execute(
        "SELECT composite_hash FROM tracked_files WHERE file_path = ?",
        (file_path,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None  # type: ignore[index]


async def delete_tracked_file(
    file_path: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    """Delete a tracked file and all its associated facts (CASCADE)."""
    db = conn or get_connection()
    await db.execute("DELETE FROM tracked_files WHERE file_path = ?", (file_path,))
    await db.commit()


async def count_tracked_files(
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> int:
    """Return the total number of tracked files."""
    db = conn or get_connection()
    cursor = await db.execute("SELECT COUNT(*) FROM tracked_files")
    row = await cursor.fetchone()
    return row[0]  # type: ignore[index]


async def clear_all(
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> int:
    """Delete ALL tracked files (and cascade all facts). Return count deleted."""
    db = conn or get_connection()
    cursor = await db.execute("SELECT COUNT(*) FROM tracked_files")
    row = await cursor.fetchone()
    count = row[0]  # type: ignore[index]
    await db.execute("DELETE FROM tracked_files")
    await db.commit()
    return count


# ============================================================
# symbols
# ============================================================

async def upsert_symbols(
    file_path: str,
    symbols: list[ExtractedSymbol],
    *,
    conn: Optional[aiosqlite.Connection] = None,
    _commit: bool = True,
) -> None:
    """Replace all symbols for *file_path* with fresh data."""
    db = conn or get_connection()
    await db.execute("DELETE FROM symbols WHERE file_path = ?", (file_path,))
    if symbols:
        await db.executemany(
            """
            INSERT INTO symbols
                (file_path, name, qualified_name, kind, line, col, extent_end_line)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (file_path, s.name, s.qualified_name, s.kind, s.line, s.col, s.extent_end_line)
                for s in symbols
            ],
        )
    if _commit:
        await db.commit()


async def get_symbols_by_file(
    file_path: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    """Return all symbols defined in *file_path*."""
    db = conn or get_connection()
    cursor = await db.execute(
        "SELECT * FROM symbols WHERE file_path = ?",
        (file_path,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]  # type: ignore[arg-type]


async def get_definitions_for_symbol(
    qualified_name: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    """Find definition(s) of a symbol by qualified name."""
    db = conn or get_connection()
    cursor = await db.execute(
        "SELECT * FROM symbols WHERE qualified_name = ?",
        (qualified_name,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]  # type: ignore[arg-type]


async def search_symbols_by_name(
    name: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    """Search for symbols whose name or qualified_name contains *name*."""
    db = conn or get_connection()
    pattern = f"%{name}%"
    cursor = await db.execute(
        "SELECT * FROM symbols WHERE qualified_name LIKE ? OR name LIKE ?",
        (pattern, pattern),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]  # type: ignore[arg-type]


async def count_symbols(
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> int:
    """Return total number of cached symbols."""
    db = conn or get_connection()
    cursor = await db.execute("SELECT COUNT(*) FROM symbols")
    row = await cursor.fetchone()
    return row[0]  # type: ignore[index]


# ============================================================
# references
# ============================================================

async def upsert_references(
    file_path: str,
    references: list[ExtractedReference],
    *,
    conn: Optional[aiosqlite.Connection] = None,
    _commit: bool = True,
) -> None:
    """Replace all references originating from *file_path* with fresh data."""
    db = conn or get_connection()
    await db.execute("DELETE FROM references_ WHERE file_path = ?", (file_path,))
    if references:
        await db.executemany(
            """
            INSERT INTO references_
                (symbol_qualified_name, file_path, line, col, ref_kind)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (r.symbol, file_path, r.line, r.col, r.kind)
                for r in references
            ],
        )
    if _commit:
        await db.commit()


async def get_references_for_symbol(
    qualified_name: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    """Return all cached references to *qualified_name*."""
    db = conn or get_connection()
    cursor = await db.execute(
        "SELECT * FROM references_ WHERE symbol_qualified_name = ?",
        (qualified_name,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]  # type: ignore[arg-type]


async def search_references_by_symbol(
    symbol_pattern: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    """Search references whose symbol qualified name contains *symbol_pattern*."""
    db = conn or get_connection()
    pattern = f"%{symbol_pattern}%"
    cursor = await db.execute(
        "SELECT * FROM references_ WHERE symbol_qualified_name LIKE ?",
        (pattern,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]  # type: ignore[arg-type]


# ============================================================
# call_edges
# ============================================================

async def upsert_call_edges(
    file_path: str,
    edges: list[ExtractedCallEdge],
    *,
    conn: Optional[aiosqlite.Connection] = None,
    _commit: bool = True,
) -> None:
    """Replace all call edges originating from *file_path* with fresh data."""
    db = conn or get_connection()
    await db.execute("DELETE FROM call_edges WHERE file_path = ?", (file_path,))
    if edges:
        await db.executemany(
            """
            INSERT INTO call_edges
                (caller_qualified_name, callee_qualified_name, file_path, line)
            VALUES (?, ?, ?, ?)
            """,
            [
                (e.caller, e.callee, file_path, e.line)
                for e in edges
            ],
        )
    if _commit:
        await db.commit()


async def get_call_edges_for_caller(
    caller_qualified_name: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    """Return outgoing call edges from a caller."""
    db = conn or get_connection()
    cursor = await db.execute(
        "SELECT * FROM call_edges WHERE caller_qualified_name = ?",
        (caller_qualified_name,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]  # type: ignore[arg-type]


async def get_call_edges_for_callee(
    callee_qualified_name: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    """Return incoming call edges to a callee."""
    db = conn or get_connection()
    cursor = await db.execute(
        "SELECT * FROM call_edges WHERE callee_qualified_name = ?",
        (callee_qualified_name,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]  # type: ignore[arg-type]


# ============================================================
# include_deps
# ============================================================

async def upsert_include_deps(
    file_path: str,
    deps: list[ExtractedIncludeDep],
    *,
    conn: Optional[aiosqlite.Connection] = None,
    _commit: bool = True,
) -> None:
    """Replace all include deps for *file_path* with fresh data."""
    db = conn or get_connection()
    await db.execute("DELETE FROM include_deps WHERE file_path = ?", (file_path,))
    if deps:
        await db.executemany(
            """
            INSERT INTO include_deps (file_path, included_path, depth)
            VALUES (?, ?, ?)
            """,
            [(file_path, d.path, d.depth) for d in deps],
        )
    if _commit:
        await db.commit()


async def get_include_deps(
    file_path: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    """Return all include dependencies for *file_path*."""
    db = conn or get_connection()
    cursor = await db.execute(
        "SELECT * FROM include_deps WHERE file_path = ?",
        (file_path,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]  # type: ignore[arg-type]


# ============================================================
# parse_runs
# ============================================================

async def insert_parse_run(
    file_path: str,
    started_at: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> int:
    """Insert a new parse run and return its row ID."""
    db = conn or get_connection()
    cursor = await db.execute(
        "INSERT INTO parse_runs (file_path, started_at) VALUES (?, ?)",
        (file_path, started_at),
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def finish_parse_run(
    run_id: int,
    success: bool,
    error_msg: str = "",
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    """Update a parse run with its completion status."""
    db = conn or get_connection()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE parse_runs SET finished_at = ?, success = ?, error_msg = ? WHERE id = ?",
        (now, int(success), error_msg, run_id),
    )
    await db.commit()


async def get_parse_runs(
    file_path: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    """Return all parse run audit records for *file_path*, newest first."""
    db = conn or get_connection()
    cursor = await db.execute(
        "SELECT * FROM parse_runs WHERE file_path = ? ORDER BY id DESC",
        (file_path,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]  # type: ignore[arg-type]


# ============================================================
# Bulk upsert from ExtractorOutput
# ============================================================

async def upsert_extractor_output(
    output: ExtractorOutput,
    content_hash: str,
    flags_hash: str,
    includes_hash: str,
    composite_hash: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    """Persist all facts from a single cpp-extractor invocation.

    This is the primary entry point used by the orchestrator after a
    successful parse.  It updates the tracked file record and replaces
    all associated facts **atomically** within a single transaction.

    If any step fails the entire transaction is rolled back so no
    partial data is left in the cache.
    """
    db = conn or get_connection()

    # All sub-operations use _commit=False so we control the transaction.
    try:
        await upsert_tracked_file(
            output.file,
            content_hash,
            flags_hash,
            includes_hash,
            composite_hash,
            conn=db,
            _commit=False,
        )
        await upsert_symbols(output.file, output.symbols, conn=db, _commit=False)
        await upsert_references(output.file, output.references, conn=db, _commit=False)
        await upsert_call_edges(output.file, output.call_edges, conn=db, _commit=False)
        await upsert_include_deps(output.file, output.include_deps, conn=db, _commit=False)

        # Single commit for the entire batch
        await db.commit()
    except Exception:
        await db.rollback()
        raise
