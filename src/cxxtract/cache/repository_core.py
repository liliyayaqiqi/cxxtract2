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
            INSERT INTO repos (
                workspace_id, repo_id, root, compile_commands, default_branch, depends_on_json,
                remote_url, token_env_var, project_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    workspace_id,
                    r["repo_id"],
                    r.get("root", ""),
                    r.get("compile_commands", ""),
                    r.get("default_branch", "main"),
                    json.dumps(r.get("depends_on", [])),
                    r.get("remote_url", ""),
                    r.get("token_env_var", ""),
                    r.get("project_path", ""),
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


async def get_repo(
    workspace_id: str,
    repo_id: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[dict[str, Any]]:
    db = conn or get_connection()
    cur = await db.execute(
        "SELECT * FROM repos WHERE workspace_id = ? AND repo_id = ?",
        (workspace_id, repo_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    out = dict(row)  # type: ignore[arg-type]
    try:
        out["depends_on"] = json.loads(out.get("depends_on_json", "[]"))
    except json.JSONDecodeError:
        out["depends_on"] = []
    return out


async def insert_repo_sync_job(
    *,
    job_id: str,
    workspace_id: str,
    repo_id: str,
    requested_commit_sha: str,
    requested_branch: str = "",
    requested_force_clean: bool = True,
    max_attempts: int = 3,
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    now = _utc_now()
    await db.execute(
        """
        INSERT INTO repo_sync_jobs (
            id, workspace_id, repo_id, requested_branch, requested_commit_sha,
            requested_force_clean, status, attempts, max_attempts, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
        """,
        (
            job_id,
            workspace_id,
            repo_id,
            requested_branch,
            requested_commit_sha,
            1 if requested_force_clean else 0,
            max(1, max_attempts),
            now,
            now,
        ),
    )
    await db.commit()


async def get_repo_sync_job(
    job_id: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[dict[str, Any]]:
    db = conn or get_connection()
    cur = await db.execute("SELECT * FROM repo_sync_jobs WHERE id = ?", (job_id,))
    row = await cur.fetchone()
    return dict(row) if row else None  # type: ignore[arg-type]


async def lease_next_repo_sync_job(
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[dict[str, Any]]:
    db = conn or get_connection()
    now = _utc_now()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cur = await db.execute(
            """
            SELECT *
            FROM repo_sync_jobs
            WHERE status IN ('pending', 'failed')
              AND attempts < max_attempts
            ORDER BY created_at ASC
            LIMIT 1
            """
        )
        row = await cur.fetchone()
        if row is None:
            await db.execute("COMMIT")
            return None

        job = dict(row)  # type: ignore[arg-type]
        await db.execute(
            """
            UPDATE repo_sync_jobs
            SET status = 'running',
                attempts = attempts + 1,
                started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
                updated_at = ?,
                error_code = '',
                error_message = ''
            WHERE id = ?
            """,
            (now, now, job["id"]),
        )
        await db.execute("COMMIT")
        return await get_repo_sync_job(str(job["id"]), conn=db)
    except Exception:
        await db.execute("ROLLBACK")
        raise


async def mark_repo_sync_job_done(
    *,
    job_id: str,
    resolved_commit_sha: str,
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    now = _utc_now()
    await db.execute(
        """
        UPDATE repo_sync_jobs
        SET status = 'done',
            resolved_commit_sha = ?,
            updated_at = ?,
            finished_at = ?,
            error_code = '',
            error_message = ''
        WHERE id = ?
        """,
        (resolved_commit_sha, now, now, job_id),
    )
    await db.commit()


async def mark_repo_sync_job_failed(
    *,
    job_id: str,
    error_code: str,
    error_message: str,
    dead_letter: bool = False,
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    now = _utc_now()
    status = "dead_letter" if dead_letter else "failed"
    await db.execute(
        """
        UPDATE repo_sync_jobs
        SET status = ?,
            updated_at = ?,
            finished_at = CASE WHEN ? = 'dead_letter' THEN ? ELSE finished_at END,
            error_code = ?,
            error_message = ?
        WHERE id = ?
        """,
        (status, now, status, now, error_code, error_message[:4000], job_id),
    )
    await db.commit()


async def upsert_repo_sync_state(
    *,
    workspace_id: str,
    repo_id: str,
    last_synced_commit_sha: str = "",
    last_synced_branch: str = "",
    success: bool = True,
    error_code: str = "",
    error_message: str = "",
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    now = _utc_now()
    last_success_at = now if success else ""
    last_failure_at = now if not success else ""
    await db.execute(
        """
        INSERT INTO repo_sync_state (
            workspace_id, repo_id, last_synced_commit_sha, last_synced_branch,
            last_success_at, last_failure_at, last_error_code, last_error_message, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(workspace_id, repo_id) DO UPDATE SET
            last_synced_commit_sha = CASE WHEN excluded.last_synced_commit_sha != '' THEN excluded.last_synced_commit_sha ELSE repo_sync_state.last_synced_commit_sha END,
            last_synced_branch = CASE WHEN excluded.last_synced_branch != '' THEN excluded.last_synced_branch ELSE repo_sync_state.last_synced_branch END,
            last_success_at = CASE WHEN excluded.last_success_at != '' THEN excluded.last_success_at ELSE repo_sync_state.last_success_at END,
            last_failure_at = CASE WHEN excluded.last_failure_at != '' THEN excluded.last_failure_at ELSE repo_sync_state.last_failure_at END,
            last_error_code = excluded.last_error_code,
            last_error_message = excluded.last_error_message,
            updated_at = excluded.updated_at
        """,
        (
            workspace_id,
            repo_id,
            last_synced_commit_sha,
            last_synced_branch,
            last_success_at,
            last_failure_at,
            error_code,
            error_message[:4000],
            now,
        ),
    )
    await db.commit()


async def get_repo_sync_state(
    workspace_id: str,
    repo_id: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[dict[str, Any]]:
    db = conn or get_connection()
    cur = await db.execute(
        "SELECT * FROM repo_sync_state WHERE workspace_id = ? AND repo_id = ?",
        (workspace_id, repo_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None  # type: ignore[arg-type]


def _embedding_to_blob(embedding: list[float]) -> bytes:
    import array

    arr = array.array("f", [float(v) for v in embedding])
    return arr.tobytes()


def _embedding_from_blob(blob: bytes) -> list[float]:
    import array

    arr = array.array("f")
    arr.frombytes(blob)
    return list(arr)


async def upsert_commit_diff_summary(
    *,
    summary_id: str,
    workspace_id: str,
    repo_id: str,
    commit_sha: str,
    branch: str,
    summary_text: str,
    embedding_model: str,
    embedding: list[float],
    metadata: dict[str, Any],
    conn: Optional[aiosqlite.Connection] = None,
) -> None:
    db = conn or get_connection()
    now = _utc_now()
    metadata_json = json.dumps(metadata, ensure_ascii=False)
    await db.execute(
        """
        INSERT INTO commit_diff_summaries (
            id, workspace_id, repo_id, commit_sha, branch, summary_text,
            embedding_model, embedding_dim, metadata_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(workspace_id, repo_id, commit_sha, embedding_model) DO UPDATE SET
            id = excluded.id,
            branch = excluded.branch,
            summary_text = excluded.summary_text,
            embedding_dim = excluded.embedding_dim,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            summary_id,
            workspace_id,
            repo_id,
            commit_sha,
            branch,
            summary_text,
            embedding_model,
            len(embedding),
            metadata_json,
            now,
            now,
        ),
    )
    cur_rowid = await db.execute(
        "SELECT rowid FROM commit_diff_summaries WHERE id = ?",
        (summary_id,),
    )
    rowid_row = await cur_rowid.fetchone()
    if rowid_row is None:
        raise RuntimeError(f"failed to resolve rowid for summary id {summary_id}")
    summary_rowid = int(rowid_row[0])  # type: ignore[index]

    await db.execute(
        """
        INSERT OR REPLACE INTO commit_diff_summary_vec(rowid, embedding)
        VALUES (?, ?)
        """,
        (summary_rowid, _embedding_to_blob(embedding)),
    )
    await db.commit()


async def get_commit_diff_summary(
    workspace_id: str,
    repo_id: str,
    commit_sha: str,
    *,
    embedding_model: str = "",
    include_embedding: bool = False,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[dict[str, Any]]:
    db = conn or get_connection()
    if embedding_model:
        cur = await db.execute(
            """
            SELECT * FROM commit_diff_summaries
            WHERE workspace_id = ? AND repo_id = ? AND commit_sha = ? AND embedding_model = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (workspace_id, repo_id, commit_sha, embedding_model),
        )
    else:
        cur = await db.execute(
            """
            SELECT * FROM commit_diff_summaries
            WHERE workspace_id = ? AND repo_id = ? AND commit_sha = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (workspace_id, repo_id, commit_sha),
        )
    row = await cur.fetchone()
    if row is None:
        return None
    record = dict(row)  # type: ignore[arg-type]
    try:
        record["metadata"] = json.loads(record.get("metadata_json", "{}"))
    except json.JSONDecodeError:
        record["metadata"] = {}
    if include_embedding:
        cur_rowid = await db.execute("SELECT rowid FROM commit_diff_summaries WHERE id = ?", (record["id"],))
        rowid_row = await cur_rowid.fetchone()
        summary_rowid = int(rowid_row[0]) if rowid_row else -1  # type: ignore[index]
        cur_vec = await db.execute(
            "SELECT embedding FROM commit_diff_summary_vec WHERE rowid = ?",
            (summary_rowid,),
        )
        vec_row = await cur_vec.fetchone()
        if vec_row is not None and vec_row[0] is not None:  # type: ignore[index]
            record["embedding"] = _embedding_from_blob(vec_row[0])  # type: ignore[index]
        else:
            record["embedding"] = []
    return record


async def search_commit_diff_summaries(
    *,
    query_embedding: list[float],
    top_k: int,
    workspace_id: str = "",
    repo_ids: Optional[list[str]] = None,
    branches: Optional[list[str]] = None,
    commit_sha_prefix: str = "",
    created_after: str = "",
    score_threshold: float = 0.0,
    conn: Optional[aiosqlite.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_connection()
    candidate_limit = max(top_k * 5, top_k)

    cur = await db.execute(
        """
        SELECT rowid, distance
        FROM commit_diff_summary_vec
        WHERE embedding MATCH ?
          AND k = ?
        """,
        (_embedding_to_blob(query_embedding), candidate_limit),
    )
    vec_rows = await cur.fetchall()
    if not vec_rows:
        return []

    distance_by_rowid: dict[int, float] = {}
    for row in vec_rows:
        sid = int(row[0])  # type: ignore[index]
        dist = float(row[1])  # type: ignore[index]
        distance_by_rowid[sid] = dist

    ids = list(distance_by_rowid.keys())
    placeholders = ",".join(["?"] * len(ids))
    sql = f"SELECT rowid AS vec_rowid, * FROM commit_diff_summaries WHERE rowid IN ({placeholders})"
    params: list[Any] = ids
    if workspace_id:
        sql += " AND workspace_id = ?"
        params.append(workspace_id)
    if repo_ids:
        ph = ",".join(["?"] * len(repo_ids))
        sql += f" AND repo_id IN ({ph})"
        params.extend(repo_ids)
    if branches:
        ph = ",".join(["?"] * len(branches))
        sql += f" AND branch IN ({ph})"
        params.extend(branches)
    if commit_sha_prefix:
        sql += " AND commit_sha LIKE ?"
        params.append(f"{commit_sha_prefix}%")
    if created_after:
        sql += " AND created_at >= ?"
        params.append(created_after)

    cur_meta = await db.execute(sql, params)
    rows = await _fetch_all_dict(cur_meta)
    ranked: list[dict[str, Any]] = []
    for row in rows:
        sid = int(row["vec_rowid"])
        dist = distance_by_rowid.get(sid)
        if dist is None:
            continue
        score = 1.0 / (1.0 + dist)
        if score < score_threshold:
            continue
        row["score"] = score
        try:
            row["metadata"] = json.loads(row.get("metadata_json", "{}"))
        except json.JSONDecodeError:
            row["metadata"] = {}
        ranked.append(row)

    ranked.sort(key=lambda r: float(r["score"]), reverse=True)
    return ranked[:top_k]
