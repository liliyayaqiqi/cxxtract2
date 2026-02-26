"""Commit diff summary storage and vector retrieval service."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from cxxtract.cache import repository as repo
from cxxtract.cache.db import is_sqlite_vec_loaded
from cxxtract.config import Settings
from cxxtract.models import (
    CommitDiffSummaryGetResponse,
    CommitDiffSummaryHit,
    CommitDiffSummaryRecord,
    CommitDiffSummarySearchRequest,
    CommitDiffSummarySearchResponse,
    CommitDiffSummaryUpsertRequest,
)

logger = logging.getLogger(__name__)


class CommitSummaryService:
    """Manage commit diff summaries and sqlite-vec similarity search."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _ensure_vector_ready(self) -> None:
        if not self._settings.enable_vector_features:
            raise RuntimeError("vector_disabled")
        if not is_sqlite_vec_loaded():
            raise RuntimeError("vector_unavailable")

    def _validate_request(self, request: CommitDiffSummaryUpsertRequest) -> None:
        if len(request.embedding) != self._settings.commit_embedding_dim:
            raise ValueError(
                f"embedding length {len(request.embedding)} does not match configured "
                f"dimension {self._settings.commit_embedding_dim}"
            )
        if len(request.summary_text) > self._settings.max_summary_chars:
            raise ValueError(
                f"summary_text exceeds max_summary_chars={self._settings.max_summary_chars}"
            )

    @staticmethod
    def _summary_id(workspace_id: str, repo_id: str, commit_sha: str, embedding_model: str) -> str:
        raw = f"{workspace_id}|{repo_id}|{commit_sha}|{embedding_model}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _to_record(row: dict[str, Any]) -> CommitDiffSummaryRecord:
        return CommitDiffSummaryRecord(
            id=row["id"],
            workspace_id=row["workspace_id"],
            repo_id=row["repo_id"],
            commit_sha=row["commit_sha"],
            branch=row.get("branch", ""),
            summary_text=row["summary_text"],
            embedding_model=row["embedding_model"],
            embedding_dim=int(row.get("embedding_dim", 0)),
            metadata=row.get("metadata", {}),
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
            embedding=row.get("embedding", []),
        )

    async def upsert_summary_with_embedding(
        self,
        request: CommitDiffSummaryUpsertRequest,
    ) -> CommitDiffSummaryRecord:
        self._ensure_vector_ready()
        self._validate_request(request)

        summary_id = self._summary_id(
            request.workspace_id,
            request.repo_id,
            request.commit_sha,
            request.embedding_model,
        )
        await repo.upsert_commit_diff_summary(
            summary_id=summary_id,
            workspace_id=request.workspace_id,
            repo_id=request.repo_id,
            commit_sha=request.commit_sha,
            branch=request.branch,
            summary_text=request.summary_text,
            embedding_model=request.embedding_model,
            embedding=request.embedding,
            metadata=request.metadata,
        )
        stored = await repo.get_commit_diff_summary(
            request.workspace_id,
            request.repo_id,
            request.commit_sha,
            embedding_model=request.embedding_model,
            include_embedding=False,
        )
        assert stored is not None
        return self._to_record(stored)

    async def get_summary(
        self,
        workspace_id: str,
        repo_id: str,
        commit_sha: str,
        *,
        include_embedding: bool,
    ) -> CommitDiffSummaryGetResponse:
        self._ensure_vector_ready()
        row = await repo.get_commit_diff_summary(
            workspace_id,
            repo_id,
            commit_sha,
            include_embedding=include_embedding,
        )
        if row is None:
            return CommitDiffSummaryGetResponse(found=False, record=None)
        return CommitDiffSummaryGetResponse(found=True, record=self._to_record(row))

    async def search_summaries(
        self,
        request: CommitDiffSummarySearchRequest,
    ) -> CommitDiffSummarySearchResponse:
        self._ensure_vector_ready()
        if len(request.query_embedding) != self._settings.commit_embedding_dim:
            raise ValueError(
                f"query_embedding length {len(request.query_embedding)} does not match configured "
                f"dimension {self._settings.commit_embedding_dim}"
            )

        rows = await repo.search_commit_diff_summaries(
            query_embedding=request.query_embedding,
            top_k=request.top_k,
            workspace_id=request.workspace_id,
            repo_ids=request.repo_ids,
            branches=request.branches,
            commit_sha_prefix=request.commit_sha_prefix,
            created_after=request.created_after,
            score_threshold=request.score_threshold,
        )

        return CommitDiffSummarySearchResponse(
            hits=[
                CommitDiffSummaryHit(
                    id=row["id"],
                    workspace_id=row["workspace_id"],
                    repo_id=row["repo_id"],
                    commit_sha=row["commit_sha"],
                    branch=row.get("branch", ""),
                    summary_text=row["summary_text"],
                    embedding_model=row["embedding_model"],
                    metadata=row.get("metadata", {}),
                    score=float(row.get("score", 0.0)),
                    created_at=row.get("created_at", ""),
                )
                for row in rows
            ]
        )
