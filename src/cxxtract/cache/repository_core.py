"""Core async CRUD operations for the SQLite workspace facts cache (v3-only)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from cxxtract.cache.db import get_connection
from cxxtract.models import ContextFileStateKind, OverlayMode, ParsePayload

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fetch_all_dict(cursor: aiosqlite.Cursor) -> list[dict[str, Any]]:
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]  # type: ignore[arg-type]


async def upsert_workspace(
    workspace_id: str,
    root_path: str,
    manifest_path: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    now = _utc_now()
    await db.execute(
        """
        INSERT INTO workspaces (workspace_id, root_path, manifest_path, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(workspace_id) DO UPDATE SET
            root_path = excluded.root_path,
            manifest_path = excluded.manifest_path,
            updated_at = excluded.updated_at
        """,
        (workspace_id, root_path, manifest_path, now, now),
    )
    await db.commit()


async def get_workspace(
    workspace_id: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[dict[str, Any]]:
    db = conn or get_connection()
    cur = await db.execute("SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,))
    row = await cur.fetchone()
    return dict(row) if row else None  # type: ignore[arg-type]


async def list_workspaces(*, conn: Optional[aiosqlite.Connection] = None) -> list[dict[str, Any]]:
    db = conn or get_connection()
    cur = await db.execute("SELECT * FROM workspaces ORDER BY workspace_id")
    return await _fetch_all_dict(cur)


async def replace_workspace_repos(
    workspace_id: str,
    repos: list[dict[str, Any]],
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> int:
    db = conn or get_connection()
    await db.execute("DELETE FROM repos WHERE workspace_id = ?", (workspace_id,))
    if repos:
        await db.executemany(
            """
            INSERT INTO repos (workspace_id, repo_id, root, compile_commands, default_branch, depends_on_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    workspace_id,
                    r["repo_id"],
                    r.get("root", ""),
                    r.get("compile_commands", ""),
                    r.get("default_branch", "main"),
                    json.dumps(r.get("depends_on", [])),
                )
                for r in repos
            ],
        )
    await db.commit()
    return len(repos)


async def list_repos(
    workspace_id: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_connection()
    cur = await db.execute("SELECT * FROM repos WHERE workspace_id = ? ORDER BY repo_id", (workspace_id,))
    rows = await _fetch_all_dict(cur)
    for r in rows:
        try:
            r["depends_on"] = json.loads(r.get("depends_on_json", "[]"))
        except json.JSONDecodeError:
            r["depends_on"] = []
    return rows


async def upsert_analysis_context(
    context_id: str,
    workspace_id: str,
    mode: str,
    *,
    base_context_id: str = "",
    overlay_mode: str = OverlayMode.SPARSE.value,
    status: str = "active",
    expires_at: str = "",
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    now = _utc_now()
    await db.execute(
        """
        INSERT INTO analysis_contexts (
            context_id, workspace_id, mode, base_context_id, overlay_mode, status,
            created_at, last_accessed_at, expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(context_id) DO UPDATE SET
            workspace_id = excluded.workspace_id,
            mode = excluded.mode,
            base_context_id = excluded.base_context_id,
            overlay_mode = excluded.overlay_mode,
            status = excluded.status,
            last_accessed_at = excluded.last_accessed_at,
            expires_at = excluded.expires_at
        """,
        (
            context_id,
            workspace_id,
            mode,
            base_context_id,
            overlay_mode,
            status,
            now,
            now,
            expires_at,
        ),
    )
    await db.commit()


async def ensure_baseline_context(
    workspace_id: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> str:
    db = conn or get_connection()
    now = _utc_now()
    await db.execute(
        """
        INSERT INTO workspaces (workspace_id, root_path, manifest_path, created_at, updated_at)
        VALUES (?, ?, '', ?, ?)
        ON CONFLICT(workspace_id) DO NOTHING
        """,
        (workspace_id, ".", now, now),
    )
    context_id = f"{workspace_id}:baseline"
    await upsert_analysis_context(
        context_id,
        workspace_id,
        "baseline",
        base_context_id="",
        overlay_mode=OverlayMode.SPARSE.value,
        conn=db,
    )
    return context_id


async def get_analysis_context(
    context_id: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[dict[str, Any]]:
    db = conn or get_connection()
    cur = await db.execute("SELECT * FROM analysis_contexts WHERE context_id = ?", (context_id,))
    row = await cur.fetchone()
    return dict(row) if row else None  # type: ignore[arg-type]


async def touch_context(context_id: str, *, conn: Optional[aiosqlite.Connection] = None) -> None:
    db = conn or get_connection()
    await db.execute(
        "UPDATE analysis_contexts SET last_accessed_at = ? WHERE context_id = ?",
        (_utc_now(), context_id),
    )
    await db.commit()


async def expire_context(context_id: str, *, conn: Optional[aiosqlite.Connection] = None) -> bool:
    db = conn or get_connection()
    cur = await db.execute(
        "UPDATE analysis_contexts SET status = 'expired', last_accessed_at = ? WHERE context_id = ?",
        (_utc_now(), context_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def list_active_contexts(
    workspace_id: str = "",
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_connection()
    if workspace_id:
        cur = await db.execute(
            "SELECT * FROM analysis_contexts WHERE workspace_id = ? AND status = 'active'",
            (workspace_id,),
        )
    else:
        cur = await db.execute("SELECT * FROM analysis_contexts WHERE status = 'active'")
    return await _fetch_all_dict(cur)


async def update_context_overlay_stats(
    context_id: str,
    *,
    file_delta: int = 0,
    row_delta: int = 0,
    max_overlay_files: int = 5000,
    max_overlay_rows: int = 2_000_000,
    force_partial_overlay: bool = False,
    conn: Optional[aiosqlite.Connection] = None,
) -> str:
    db = conn or get_connection()
    ctx = await get_analysis_context(context_id, conn=db)
    if ctx is None:
        return OverlayMode.SPARSE.value

    new_files = max(0, int(ctx.get("overlay_file_count", 0)) + file_delta)
    new_rows = max(0, int(ctx.get("overlay_row_count", 0)) + row_delta)
    mode = str(ctx.get("overlay_mode", OverlayMode.SPARSE.value))
    if force_partial_overlay or new_files > max_overlay_files or new_rows > max_overlay_rows:
        mode = OverlayMode.PARTIAL_OVERLAY.value

    await db.execute(
        """
        UPDATE analysis_contexts
        SET overlay_file_count = ?, overlay_row_count = ?, overlay_mode = ?, last_accessed_at = ?
        WHERE context_id = ?
        """,
        (new_files, new_rows, mode, _utc_now(), context_id),
    )
    await db.commit()
    return mode


async def upsert_context_file_state(
    context_id: str,
    file_key: str,
    state: ContextFileStateKind | str,
    *,
    replaced_from_file_key: str = "",
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    await db.execute(
        """
        INSERT INTO context_file_states (context_id, file_key, state, replaced_from_file_key, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(context_id, file_key) DO UPDATE SET
            state = excluded.state,
            replaced_from_file_key = excluded.replaced_from_file_key,
            updated_at = excluded.updated_at
        """,
        (context_id, file_key, str(state), replaced_from_file_key, _utc_now()),
    )
    await db.commit()


async def get_context_file_states(
    context_id: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_connection()
    cur = await db.execute("SELECT * FROM context_file_states WHERE context_id = ?", (context_id,))
    return await _fetch_all_dict(cur)


async def upsert_recall_content(
    context_id: str,
    file_key: str,
    repo_id: str,
    content: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    await db.execute("DELETE FROM recall_fts WHERE context_id = ? AND file_key = ?", (context_id, file_key))
    await db.execute(
        "INSERT INTO recall_fts (context_id, file_key, repo_id, content) VALUES (?, ?, ?, ?)",
        (context_id, file_key, repo_id, content),
    )


async def delete_recall_content(
    context_id: str,
    file_key: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    await db.execute("DELETE FROM recall_fts WHERE context_id = ? AND file_key = ?", (context_id, file_key))
    await db.commit()


async def search_recall_candidates(
    context_id: str,
    query: str,
    *,
    repo_ids: Optional[list[str]] = None,
    max_files: int = 200,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[str]:
    db = conn or get_connection()
    sql = "SELECT DISTINCT file_key FROM recall_fts WHERE context_id = ? AND recall_fts MATCH ?"
    params: list[Any] = [context_id, query]
    if repo_ids:
        placeholders = ",".join(["?"] * len(repo_ids))
        sql += f" AND repo_id IN ({placeholders})"
        params.extend(repo_ids)
    sql += " LIMIT ?"
    params.append(max_files)
    try:
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
        return [r[0] for r in rows]  # type: ignore[index]
    except Exception:
        logger.exception("FTS search failed for context=%s query=%s", context_id, query)
        return []


async def insert_parse_run(
    context_id: str,
    file_key: str,
    abs_path: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> int:
    db = conn or get_connection()
    cur = await db.execute(
        """
        INSERT INTO parse_runs (context_id, file_key, abs_path, started_at)
        VALUES (?, ?, ?, ?)
        """,
        (context_id, file_key, abs_path, _utc_now()),
    )
    await db.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def finish_parse_run(
    run_id: int,
    *,
    success: bool,
    error_msg: str = "",
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    await db.execute(
        """
        UPDATE parse_runs
        SET finished_at = ?, success = ?, error_msg = ?
        WHERE id = ?
        """,
        (_utc_now(), 1 if success else 0, error_msg, run_id),
    )
    await db.commit()


async def get_parse_runs(
    context_id: str,
    file_key: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_connection()
    cur = await db.execute(
        """
        SELECT * FROM parse_runs
        WHERE context_id = ? AND file_key = ?
        ORDER BY id DESC
        """,
        (context_id, file_key),
    )
    return await _fetch_all_dict(cur)


async def upsert_parse_payload(
    payload: ParsePayload,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    now = _utc_now()
    try:
        await db.execute(
            """
            INSERT INTO tracked_files (
                context_id, file_key, repo_id, rel_path, abs_path, content_hash,
                flags_hash, includes_hash, composite_hash, last_parsed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(context_id, file_key) DO UPDATE SET
                repo_id = excluded.repo_id,
                rel_path = excluded.rel_path,
                abs_path = excluded.abs_path,
                content_hash = excluded.content_hash,
                flags_hash = excluded.flags_hash,
                includes_hash = excluded.includes_hash,
                composite_hash = excluded.composite_hash,
                last_parsed_at = excluded.last_parsed_at
            """,
            (
                payload.context_id,
                payload.file_key,
                payload.repo_id,
                payload.rel_path,
                payload.abs_path,
                payload.content_hash,
                payload.flags_hash,
                payload.includes_hash,
                payload.composite_hash,
                now,
            ),
        )

        await db.execute("DELETE FROM symbols WHERE context_id = ? AND file_key = ?", (payload.context_id, payload.file_key))
        await db.execute("DELETE FROM references_ WHERE context_id = ? AND file_key = ?", (payload.context_id, payload.file_key))
        await db.execute("DELETE FROM call_edges WHERE context_id = ? AND file_key = ?", (payload.context_id, payload.file_key))
        await db.execute("DELETE FROM include_deps WHERE context_id = ? AND file_key = ?", (payload.context_id, payload.file_key))

        if payload.output.symbols:
            await db.executemany(
                """
                INSERT INTO symbols (
                    context_id, file_key, name, qualified_name, kind, line, col, extent_end_line
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        payload.context_id,
                        payload.file_key,
                        s.name,
                        s.qualified_name,
                        s.kind,
                        s.line,
                        s.col,
                        s.extent_end_line,
                    )
                    for s in payload.output.symbols
                ],
            )

        if payload.output.references:
            await db.executemany(
                """
                INSERT INTO references_ (
                    context_id, file_key, symbol_qualified_name, line, col, ref_kind
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        payload.context_id,
                        payload.file_key,
                        r.symbol,
                        r.line,
                        r.col,
                        r.kind,
                    )
                    for r in payload.output.references
                ],
            )

        if payload.output.call_edges:
            await db.executemany(
                """
                INSERT INTO call_edges (
                    context_id, file_key, caller_qualified_name, callee_qualified_name, line
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        payload.context_id,
                        payload.file_key,
                        e.caller,
                        e.callee,
                        e.line,
                    )
                    for e in payload.output.call_edges
                ],
            )

        if payload.resolved_include_deps:
            await db.executemany(
                """
                INSERT INTO include_deps (
                    context_id, file_key, included_file_key, included_abs_path, raw_path, depth
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        payload.context_id,
                        payload.file_key,
                        d.resolved_file_key,
                        d.resolved_abs_path,
                        d.raw_path,
                        d.depth,
                    )
                    for d in payload.resolved_include_deps
                ],
            )

        try:
            content = Path(payload.abs_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        await upsert_recall_content(payload.context_id, payload.file_key, payload.repo_id, content, conn=db)

        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def get_tracked_file(
    context_id: str,
    file_key: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[dict[str, Any]]:
    db = conn or get_connection()
    cur = await db.execute(
        "SELECT * FROM tracked_files WHERE context_id = ? AND file_key = ?",
        (context_id, file_key),
    )
    row = await cur.fetchone()
    return dict(row) if row else None  # type: ignore[arg-type]


async def get_composite_hash(
    context_id: str,
    file_key: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[str]:
    db = conn or get_connection()
    cur = await db.execute(
        "SELECT composite_hash FROM tracked_files WHERE context_id = ? AND file_key = ?",
        (context_id, file_key),
    )
    row = await cur.fetchone()
    return row[0] if row else None  # type: ignore[index]


async def delete_tracked_file(
    context_id: str,
    file_key: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    await db.execute(
        "DELETE FROM tracked_files WHERE context_id = ? AND file_key = ?",
        (context_id, file_key),
    )
    await delete_recall_content(context_id, file_key, conn=db)
    await db.commit()


async def count_tracked_files(
    context_id: str = "",
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> int:
    db = conn or get_connection()
    if context_id:
        cur = await db.execute("SELECT COUNT(*) FROM tracked_files WHERE context_id = ?", (context_id,))
    else:
        cur = await db.execute("SELECT COUNT(*) FROM tracked_files")
    row = await cur.fetchone()
    return row[0] if row else 0  # type: ignore[index]


async def count_symbols(
    context_id: str = "",
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> int:
    db = conn or get_connection()
    if context_id:
        cur = await db.execute("SELECT COUNT(*) FROM symbols WHERE context_id = ?", (context_id,))
    else:
        cur = await db.execute("SELECT COUNT(*) FROM symbols")
    row = await cur.fetchone()
    return row[0] if row else 0  # type: ignore[index]


async def clear_context(
    context_id: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> int:
    db = conn or get_connection()
    cur = await db.execute("SELECT COUNT(*) FROM tracked_files WHERE context_id = ?", (context_id,))
    row = await cur.fetchone()
    count = row[0] if row else 0  # type: ignore[index]
    await db.execute("DELETE FROM tracked_files WHERE context_id = ?", (context_id,))
    await db.execute("DELETE FROM recall_fts WHERE context_id = ?", (context_id,))
    await db.commit()
    return count


def _iter_contexts(context_chain: Optional[list[str]]) -> list[str]:
    return context_chain or []


async def _search_symbols_in_context(
    context_id: str,
    name: str,
    *,
    candidate_file_keys: Optional[set[str]] = None,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_connection()
    pattern = f"%{name}%"
    sql = (
        "SELECT s.*, t.abs_path, t.repo_id, t.rel_path "
        "FROM symbols s JOIN tracked_files t "
        "ON s.context_id = t.context_id AND s.file_key = t.file_key "
        "WHERE s.context_id = ? AND (s.qualified_name LIKE ? OR s.name LIKE ?)"
    )
    params: list[Any] = [context_id, pattern, pattern]
    if candidate_file_keys:
        placeholders = ",".join(["?"] * len(candidate_file_keys))
        sql += f" AND s.file_key IN ({placeholders})"
        params.extend(sorted(candidate_file_keys))
    cur = await db.execute(sql, params)
    return await _fetch_all_dict(cur)


async def search_symbols_by_name(
    name: str,
    *,
    context_chain: Optional[list[str]] = None,
    candidate_file_keys: Optional[set[str]] = None,
    excluded_file_keys: Optional[set[str]] = None,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    excluded = excluded_file_keys or set()
    seen: set[tuple[str, str, int, int]] = set()
    merged: list[dict[str, Any]] = []
    for context_id in _iter_contexts(context_chain):
        rows = await _search_symbols_in_context(context_id, name, candidate_file_keys=candidate_file_keys, conn=conn)
        for r in rows:
            if r["file_key"] in excluded:
                continue
            key = (r["file_key"], r["qualified_name"], r["line"], r["col"])
            if key in seen:
                continue
            seen.add(key)
            r["context_id"] = context_id
            merged.append(r)
    return merged


async def _search_refs_in_context(
    context_id: str,
    symbol_pattern: str,
    *,
    candidate_file_keys: Optional[set[str]] = None,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_connection()
    pattern = f"%{symbol_pattern}%"
    sql = (
        "SELECT r.*, t.abs_path, t.repo_id, t.rel_path "
        "FROM references_ r JOIN tracked_files t "
        "ON r.context_id = t.context_id AND r.file_key = t.file_key "
        "WHERE r.context_id = ? AND r.symbol_qualified_name LIKE ?"
    )
    params: list[Any] = [context_id, pattern]
    if candidate_file_keys:
        placeholders = ",".join(["?"] * len(candidate_file_keys))
        sql += f" AND r.file_key IN ({placeholders})"
        params.extend(sorted(candidate_file_keys))
    cur = await db.execute(sql, params)
    return await _fetch_all_dict(cur)


async def search_references_by_symbol(
    symbol_pattern: str,
    *,
    context_chain: Optional[list[str]] = None,
    candidate_file_keys: Optional[set[str]] = None,
    excluded_file_keys: Optional[set[str]] = None,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    excluded = excluded_file_keys or set()
    seen: set[tuple[str, str, int, int, str]] = set()
    merged: list[dict[str, Any]] = []
    for context_id in _iter_contexts(context_chain):
        rows = await _search_refs_in_context(
            context_id,
            symbol_pattern,
            candidate_file_keys=candidate_file_keys,
            conn=conn,
        )
        for r in rows:
            if r["file_key"] in excluded:
                continue
            key = (r["file_key"], r["symbol_qualified_name"], r["line"], r["col"], r["ref_kind"])
            if key in seen:
                continue
            seen.add(key)
            r["context_id"] = context_id
            merged.append(r)
    return merged


async def _call_edges_context(
    context_id: str,
    *,
    caller: str = "",
    callee: str = "",
    candidate_file_keys: Optional[set[str]] = None,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_connection()
    sql = (
        "SELECT c.*, t.abs_path, t.repo_id, t.rel_path "
        "FROM call_edges c JOIN tracked_files t "
        "ON c.context_id = t.context_id AND c.file_key = t.file_key "
        "WHERE c.context_id = ?"
    )
    params: list[Any] = [context_id]
    if caller:
        sql += " AND c.caller_qualified_name = ?"
        params.append(caller)
    if callee:
        sql += " AND c.callee_qualified_name = ?"
        params.append(callee)
    if candidate_file_keys:
        placeholders = ",".join(["?"] * len(candidate_file_keys))
        sql += f" AND c.file_key IN ({placeholders})"
        params.extend(sorted(candidate_file_keys))
    cur = await db.execute(sql, params)
    return await _fetch_all_dict(cur)


async def get_call_edges_for_caller(
    caller_qualified_name: str,
    *,
    context_chain: Optional[list[str]] = None,
    candidate_file_keys: Optional[set[str]] = None,
    excluded_file_keys: Optional[set[str]] = None,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    excluded = excluded_file_keys or set()
    seen: set[tuple[str, str, str, int]] = set()
    merged: list[dict[str, Any]] = []
    for context_id in _iter_contexts(context_chain):
        rows = await _call_edges_context(
            context_id,
            caller=caller_qualified_name,
            candidate_file_keys=candidate_file_keys,
            conn=conn,
        )
        for r in rows:
            if r["file_key"] in excluded:
                continue
            key = (r["file_key"], r["caller_qualified_name"], r["callee_qualified_name"], r["line"])
            if key in seen:
                continue
            seen.add(key)
            r["context_id"] = context_id
            merged.append(r)
    return merged


async def get_call_edges_for_callee(
    callee_qualified_name: str,
    *,
    context_chain: Optional[list[str]] = None,
    candidate_file_keys: Optional[set[str]] = None,
    excluded_file_keys: Optional[set[str]] = None,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    excluded = excluded_file_keys or set()
    seen: set[tuple[str, str, str, int]] = set()
    merged: list[dict[str, Any]] = []
    for context_id in _iter_contexts(context_chain):
        rows = await _call_edges_context(
            context_id,
            callee=callee_qualified_name,
            candidate_file_keys=candidate_file_keys,
            conn=conn,
        )
        for r in rows:
            if r["file_key"] in excluded:
                continue
            key = (r["file_key"], r["caller_qualified_name"], r["callee_qualified_name"], r["line"])
            if key in seen:
                continue
            seen.add(key)
            r["context_id"] = context_id
            merged.append(r)
    return merged


async def get_symbols_by_file(
    file_key: str,
    *,
    context_chain: Optional[list[str]] = None,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_connection()
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, str]] = set()
    for context_id in _iter_contexts(context_chain):
        cur = await db.execute(
            """
            SELECT s.*, t.abs_path, t.repo_id, t.rel_path
            FROM symbols s JOIN tracked_files t
              ON s.context_id = t.context_id AND s.file_key = t.file_key
            WHERE s.context_id = ? AND s.file_key = ?
            """,
            (context_id, file_key),
        )
        rows = await _fetch_all_dict(cur)
        for r in rows:
            key = (r["qualified_name"], r["line"], r["col"], r["kind"])
            if key in seen:
                continue
            seen.add(key)
            r["context_id"] = context_id
            merged.append(r)
    return merged


async def insert_index_job(
    *,
    job_id: str,
    workspace_id: str,
    repo_id: str,
    context_id: str,
    event_type: str,
    event_sha: str = "",
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    now = _utc_now()
    await db.execute(
        """
        INSERT INTO index_jobs (
            id, workspace_id, repo_id, context_id, event_type, event_sha,
            status, attempts, max_attempts, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 5, ?, ?)
        """,
        (job_id, workspace_id, repo_id, context_id, event_type, event_sha, now, now),
    )
    await db.commit()
