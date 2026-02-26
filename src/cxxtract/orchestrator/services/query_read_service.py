"""Query read service for overlay-first result materialization."""

from __future__ import annotations

from cxxtract.cache import repository as repo
from cxxtract.models import CallEdgeResponse, CallGraphDirection, ReferenceLocation, SymbolLocation


class QueryReadService:
    """Reads symbols/references/edges with context-chain semantics."""

    @staticmethod
    async def load_definition(
        symbol: str,
        *,
        context_chain: list[str],
        candidate_file_keys: set[str],
        excluded_file_keys: set[str],
    ) -> SymbolLocation | None:
        rows = await repo.search_symbols_by_name(
            symbol,
            context_chain=context_chain,
            candidate_file_keys=candidate_file_keys,
            excluded_file_keys=excluded_file_keys,
        )
        if not rows:
            return None
        d = rows[0]
        return SymbolLocation(
            file_key=d["file_key"],
            line=d["line"],
            col=d["col"],
            kind=d["kind"],
            qualified_name=d["qualified_name"],
            extent_end_line=d.get("extent_end_line", 0),
            abs_path=d.get("abs_path", ""),
            context_id=d.get("context_id", ""),
        )

    @staticmethod
    async def load_definitions(
        symbol: str,
        *,
        context_chain: list[str],
        candidate_file_keys: set[str],
        excluded_file_keys: set[str],
    ) -> list[SymbolLocation]:
        rows = await repo.search_symbols_by_name(
            symbol,
            context_chain=context_chain,
            candidate_file_keys=candidate_file_keys,
            excluded_file_keys=excluded_file_keys,
        )
        return [
            SymbolLocation(
                file_key=r["file_key"],
                line=r["line"],
                col=r["col"],
                kind=r["kind"],
                qualified_name=r["qualified_name"],
                extent_end_line=r.get("extent_end_line", 0),
                abs_path=r.get("abs_path", ""),
                context_id=r.get("context_id", ""),
            )
            for r in rows
        ]

    @staticmethod
    async def load_references(
        symbol: str,
        *,
        context_chain: list[str],
        candidate_file_keys: set[str],
        excluded_file_keys: set[str],
    ) -> list[ReferenceLocation]:
        rows = await repo.search_references_by_symbol(
            symbol,
            context_chain=context_chain,
            candidate_file_keys=candidate_file_keys,
            excluded_file_keys=excluded_file_keys,
        )
        return [
            ReferenceLocation(
                file_key=r["file_key"],
                line=r["line"],
                col=r["col"],
                kind=r["ref_kind"],
                abs_path=r.get("abs_path", ""),
                context_id=r.get("context_id", ""),
            )
            for r in rows
        ]

    @staticmethod
    async def load_call_edges(
        symbol: str,
        direction: CallGraphDirection,
        *,
        context_chain: list[str],
        candidate_file_keys: set[str],
        excluded_file_keys: set[str],
    ) -> list[CallEdgeResponse]:
        edges: list[CallEdgeResponse] = []

        if direction in (CallGraphDirection.OUTGOING, CallGraphDirection.BOTH):
            rows = await repo.get_call_edges_for_caller(
                symbol,
                context_chain=context_chain,
                candidate_file_keys=candidate_file_keys,
                excluded_file_keys=excluded_file_keys,
            )
            edges.extend(
                [
                    CallEdgeResponse(
                        caller=r["caller_qualified_name"],
                        callee=r["callee_qualified_name"],
                        file_key=r["file_key"],
                        line=r["line"],
                        abs_path=r.get("abs_path", ""),
                        context_id=r.get("context_id", ""),
                    )
                    for r in rows
                ]
            )

        if direction in (CallGraphDirection.INCOMING, CallGraphDirection.BOTH):
            rows = await repo.get_call_edges_for_callee(
                symbol,
                context_chain=context_chain,
                candidate_file_keys=candidate_file_keys,
                excluded_file_keys=excluded_file_keys,
            )
            edges.extend(
                [
                    CallEdgeResponse(
                        caller=r["caller_qualified_name"],
                        callee=r["callee_qualified_name"],
                        file_key=r["file_key"],
                        line=r["line"],
                        abs_path=r.get("abs_path", ""),
                        context_id=r.get("context_id", ""),
                    )
                    for r in rows
                ]
            )

        return edges

    @staticmethod
    async def load_file_symbols(file_key: str, *, context_chain: list[str]) -> list[SymbolLocation]:
        rows = await repo.get_symbols_by_file(file_key, context_chain=context_chain)
        return [
            SymbolLocation(
                file_key=s["file_key"],
                line=s["line"],
                col=s["col"],
                kind=s["kind"],
                qualified_name=s["qualified_name"],
                extent_end_line=s.get("extent_end_line", 0),
                abs_path=s.get("abs_path", ""),
                context_id=s.get("context_id", ""),
            )
            for s in rows
        ]
