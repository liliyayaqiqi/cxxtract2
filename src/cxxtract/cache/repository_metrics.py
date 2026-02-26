"""Metrics and health-oriented repository helpers (v3-only)."""

from __future__ import annotations

from typing import Optional

import aiosqlite

from cxxtract.cache.db import get_connection


async def count_active_contexts(*, conn: Optional[aiosqlite.Connection] = None) -> int:
    db = conn or get_connection()
    cur = await db.execute("SELECT COUNT(*) FROM analysis_contexts WHERE status = 'active'")
    row = await cur.fetchone()
    return row[0] if row else 0  # type: ignore[index]


async def get_overlay_disk_usage_bytes(*, conn: Optional[aiosqlite.Connection] = None) -> int:
    db = conn or get_connection()
    cur1 = await db.execute("PRAGMA page_count")
    cur2 = await db.execute("PRAGMA page_size")
    row1 = await cur1.fetchone()
    row2 = await cur2.fetchone()
    pages = int(row1[0]) if row1 else 0  # type: ignore[index]
    page_size = int(row2[0]) if row2 else 0  # type: ignore[index]
    return pages * page_size


async def get_index_queue_depth(*, conn: Optional[aiosqlite.Connection] = None) -> int:
    db = conn or get_connection()
    cur = await db.execute("SELECT COUNT(*) FROM index_jobs WHERE status IN ('pending', 'running')")
    row = await cur.fetchone()
    return row[0] if row else 0  # type: ignore[index]


async def get_oldest_pending_job_age_s(*, conn: Optional[aiosqlite.Connection] = None) -> float:
    db = conn or get_connection()
    cur = await db.execute(
        """
        SELECT (julianday('now') - julianday(created_at)) * 86400.0
        FROM index_jobs
        WHERE status IN ('pending', 'running')
        ORDER BY created_at ASC
        LIMIT 1
        """
    )
    row = await cur.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0  # type: ignore[index]
