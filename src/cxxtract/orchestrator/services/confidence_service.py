"""Shared confidence envelope builder."""

from __future__ import annotations

from cxxtract.models import ConfidenceEnvelope, OverlayMode


def build_confidence(
    verified: list[str],
    stale: list[str],
    unparsed: list[str],
    warnings: list[str],
    overlay_mode: OverlayMode,
) -> ConfidenceEnvelope:
    """Construct confidence metadata from file classification outputs."""
    total = len(verified) + len(stale) + len(unparsed)
    verified_ratio = len(verified) / total if total else 0.0

    repo_total: dict[str, int] = {}
    repo_verified: dict[str, int] = {}
    for fk in verified + stale + unparsed:
        repo_id = fk.split(":", 1)[0] if ":" in fk else "unknown"
        repo_total[repo_id] = repo_total.get(repo_id, 0) + 1
    for fk in verified:
        repo_id = fk.split(":", 1)[0] if ":" in fk else "unknown"
        repo_verified[repo_id] = repo_verified.get(repo_id, 0) + 1

    return ConfidenceEnvelope(
        verified_files=verified,
        stale_files=stale,
        unparsed_files=unparsed,
        total_candidates=total,
        verified_ratio=round(verified_ratio, 4),
        warnings=sorted(set(warnings)),
        overlay_mode=overlay_mode,
        repo_coverage={
            repo_id: round(repo_verified.get(repo_id, 0) / count, 4)
            for repo_id, count in repo_total.items()
            if count > 0
        },
    )

