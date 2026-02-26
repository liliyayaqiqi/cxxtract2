"""Pydantic models for API requests, responses, and internal data transfer."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SymbolKind(str, Enum):
    """Clang AST cursor kinds we care about."""

    FUNCTION = "Function"
    CXX_METHOD = "CXXMethod"
    CONSTRUCTOR = "Constructor"
    DESTRUCTOR = "Destructor"
    CLASS_DECL = "ClassDecl"
    STRUCT_DECL = "StructDecl"
    ENUM_DECL = "EnumDecl"
    ENUM_CONSTANT = "EnumConstant"
    NAMESPACE = "Namespace"
    TYPEDEF = "Typedef"
    TYPE_ALIAS = "TypeAlias"
    VAR_DECL = "VarDecl"
    FIELD_DECL = "FieldDecl"
    TEMPLATE_FUNCTION = "FunctionTemplate"
    TEMPLATE_CLASS = "ClassTemplate"
    MACRO = "Macro"
    UNKNOWN = "Unknown"


class RefKind(str, Enum):
    """How a symbol is referenced."""

    CALL = "call"
    READ = "read"
    WRITE = "write"
    ADDR = "addr"
    TYPE_REF = "type_ref"
    UNKNOWN = "unknown"


class CallGraphDirection(str, Enum):
    """Direction for call-graph queries."""

    OUTGOING = "outgoing"
    INCOMING = "incoming"
    BOTH = "both"


class AnalysisMode(str, Enum):
    """Analysis context mode."""

    BASELINE = "baseline"
    PR = "pr"


class OverlayMode(str, Enum):
    """How overlay facts are materialized."""

    FULL = "full"
    SPARSE = "sparse"
    PARTIAL_OVERLAY = "partial_overlay"


class ContextFileStateKind(str, Enum):
    """Per-file state inside a context overlay."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    UNCHANGED = "unchanged"


class RepoSyncJobStatus(str, Enum):
    """Lifecycle state for a repo sync job."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class ExtractedSymbol(BaseModel):
    """A symbol definition extracted from the AST."""

    name: str
    qualified_name: str
    kind: str
    line: int
    col: int
    extent_end_line: int = 0


class ExtractedReference(BaseModel):
    """A symbol reference extracted from the AST."""

    symbol: str
    line: int
    col: int
    kind: str = "unknown"


class ExtractedCallEdge(BaseModel):
    """A call edge extracted from the AST."""

    caller: str
    callee: str
    line: int


class ExtractedIncludeDep(BaseModel):
    """An include dependency extracted from the AST."""

    path: str
    depth: int = 1


class ExtractorOutput(BaseModel):
    """JSON output schema from cpp-extractor for a single file."""

    file: str
    symbols: list[ExtractedSymbol] = Field(default_factory=list)
    references: list[ExtractedReference] = Field(default_factory=list)
    call_edges: list[ExtractedCallEdge] = Field(default_factory=list)
    include_deps: list[ExtractedIncludeDep] = Field(default_factory=list)
    success: bool = True
    diagnostics: list[str] = Field(default_factory=list)


class ResolvedIncludeDep(BaseModel):
    """Include dependency after workspace/path-remap normalization."""

    raw_path: str
    resolved_file_key: str = ""
    resolved_abs_path: str = ""
    resolved: bool = False
    depth: int = 1


class ParsePayload(BaseModel):
    """Output produced by parse workers and consumed by single writer."""

    context_id: str
    file_key: str
    repo_id: str
    rel_path: str
    abs_path: str
    output: ExtractorOutput
    resolved_include_deps: list[ResolvedIncludeDep] = Field(default_factory=list)
    content_hash: str
    flags_hash: str
    includes_hash: str
    composite_hash: str
    warnings: list[str] = Field(default_factory=list)


class RecallHit(BaseModel):
    """A candidate file identified by ripgrep."""

    file_path: str
    line_number: int
    line_text: str


class RecallResult(BaseModel):
    """Structured result from a ripgrep recall invocation."""

    hits: list[RecallHit] = Field(default_factory=list)
    error: Optional[str] = None
    rg_exit_code: Optional[int] = None
    elapsed_ms: float = 0.0
    pattern: str = ""


class AnalysisContextSpec(BaseModel):
    """Selects baseline/pr context for a query."""

    mode: AnalysisMode = AnalysisMode.BASELINE
    context_id: str = ""
    base_ref: str = ""
    head_ref: str = ""
    pr_id: str = ""


class QueryScope(BaseModel):
    """Controls repo traversal scope for a query."""

    entry_repos: list[str] = Field(default_factory=list)
    max_repo_hops: int = Field(2, ge=0, le=10)


class RepoOverride(BaseModel):
    """Per-repo runtime override settings for query execution."""

    compile_commands: str = ""


class ContextFileState(BaseModel):
    """State of a file in a context overlay."""

    context_id: str
    file_key: str
    state: ContextFileStateKind
    replaced_from_file_key: str = ""


class ConfidenceEnvelope(BaseModel):
    """Communicates exactly how much of the codebase was semantically verified."""

    verified_files: list[str] = Field(default_factory=list)
    stale_files: list[str] = Field(default_factory=list)
    unparsed_files: list[str] = Field(default_factory=list)
    total_candidates: int = 0
    verified_ratio: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    overlay_mode: OverlayMode = OverlayMode.SPARSE
    repo_coverage: dict[str, float] = Field(default_factory=dict)


class SymbolQueryRequest(BaseModel):
    """Request body for /query/references and /query/definition."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., description="Qualified or unqualified C++ symbol name")
    workspace_id: str = Field(..., min_length=1, description="Workspace identifier")
    analysis_context: AnalysisContextSpec = Field(default_factory=AnalysisContextSpec)
    scope: QueryScope = Field(default_factory=QueryScope)
    repo_overrides: dict[str, RepoOverride] = Field(default_factory=dict)
    max_recall_files: Optional[int] = Field(
        None,
        description="Override max candidate files from recall",
    )
    max_parse_workers: Optional[int] = Field(
        None,
        description="Override max concurrent cpp-extractor processes",
    )


class CallGraphRequest(BaseModel):
    """Request body for /query/call-graph."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., description="Qualified function name")
    workspace_id: str = Field(..., min_length=1, description="Workspace identifier")
    analysis_context: AnalysisContextSpec = Field(default_factory=AnalysisContextSpec)
    scope: QueryScope = Field(default_factory=QueryScope)
    repo_overrides: dict[str, RepoOverride] = Field(default_factory=dict)
    direction: CallGraphDirection = CallGraphDirection.BOTH
    max_depth: int = Field(3, ge=1, le=10, description="Max traversal depth")
    max_recall_files: Optional[int] = None
    max_parse_workers: Optional[int] = None


class FileSymbolsRequest(BaseModel):
    """Request body for /query/file-symbols."""

    model_config = ConfigDict(extra="forbid")

    file_key: str = Field(..., description="Canonical file key: repo_id:rel/path.cpp")
    workspace_id: str = Field(..., min_length=1, description="Workspace identifier")
    analysis_context: AnalysisContextSpec = Field(default_factory=AnalysisContextSpec)
    repo_overrides: dict[str, RepoOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_file_key(self) -> "FileSymbolsRequest":
        if ":" not in self.file_key:
            raise ValueError("file_key must be canonical repo_id:rel/path")
        return self


class CacheInvalidateRequest(BaseModel):
    """Request body for /cache/invalidate."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(..., min_length=1, description="Workspace identifier")
    context_id: str = Field("", description="Context ID. Empty means active baseline.")
    file_keys: Optional[list[str]] = Field(
        None,
        description="Specific canonical file keys to invalidate. If None, clear the context cache.",
    )


class WorkspaceRegisterRequest(BaseModel):
    """Register a workspace and its manifest path."""

    workspace_id: str
    root_path: str
    manifest_path: str = ""


class ContextCreateOverlayRequest(BaseModel):
    """Create a PR overlay context."""

    workspace_id: str
    pr_id: str
    base_ref: str = ""
    head_ref: str = ""
    context_id: str = ""


class WebhookGitLabRequest(BaseModel):
    """Generic GitLab webhook payload wrapper."""

    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RepoSyncRequest(BaseModel):
    """Request for deterministic repo sync at exact commit SHA."""

    repo_id: str
    commit_sha: str
    branch: str = ""
    force_clean: bool = True

    @field_validator("commit_sha")
    @classmethod
    def _validate_commit_sha(cls, value: str) -> str:
        value = value.strip()
        if len(value) != 40 or any(c not in "0123456789abcdefABCDEF" for c in value):
            raise ValueError("commit_sha must be a 40-character hex SHA")
        return value.lower()


class RepoSyncBatchRequest(BaseModel):
    """Batch sync request for multiple repositories."""

    targets: list[RepoSyncRequest] = Field(..., min_length=1)


class RepoSyncJobResponse(BaseModel):
    """Response describing sync job status."""

    job_id: str
    workspace_id: str
    repo_id: str
    requested_commit_sha: str
    requested_branch: str = ""
    requested_force_clean: bool = True
    resolved_commit_sha: str = ""
    status: RepoSyncJobStatus
    attempts: int = 0
    max_attempts: int = 0
    error_code: str = ""
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""
    started_at: str = ""
    finished_at: str = ""


class RepoSyncBatchResponse(BaseModel):
    """Batch enqueue result."""

    jobs: list[RepoSyncJobResponse] = Field(default_factory=list)


class RepoSyncStatusResponse(BaseModel):
    """Latest sync status for a repository."""

    workspace_id: str
    repo_id: str
    last_synced_commit_sha: str = ""
    last_synced_branch: str = ""
    last_success_at: str = ""
    last_failure_at: str = ""
    last_error_code: str = ""
    last_error_message: str = ""


class CommitDiffSummaryUpsertRequest(BaseModel):
    """Caller-provided merged commit diff summary and embedding."""

    workspace_id: str
    repo_id: str
    commit_sha: str
    branch: str = ""
    summary_text: str
    embedding_model: str
    embedding: list[float]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("commit_sha")
    @classmethod
    def _validate_commit_sha(cls, value: str) -> str:
        value = value.strip()
        if len(value) != 40 or any(c not in "0123456789abcdefABCDEF" for c in value):
            raise ValueError("commit_sha must be a 40-character hex SHA")
        return value.lower()


class CommitDiffSummarySearchRequest(BaseModel):
    """Top-k vector search over stored commit diff summaries."""

    query_embedding: list[float]
    top_k: int = Field(10, ge=1, le=100)
    workspace_id: str
    repo_ids: list[str] = Field(default_factory=list)
    branches: list[str] = Field(default_factory=list)
    commit_sha_prefix: str = ""
    created_after: str = ""
    score_threshold: float = 0.0


class CommitDiffSummaryHit(BaseModel):
    """Search hit entry."""

    id: str
    workspace_id: str
    repo_id: str
    commit_sha: str
    branch: str
    summary_text: str
    embedding_model: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float
    created_at: str


class CommitDiffSummarySearchResponse(BaseModel):
    """Search response envelope."""

    hits: list[CommitDiffSummaryHit] = Field(default_factory=list)


class CommitDiffSummaryRecord(BaseModel):
    """Stored summary record."""

    id: str
    workspace_id: str
    repo_id: str
    commit_sha: str
    branch: str
    summary_text: str
    embedding_model: str
    embedding_dim: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    embedding: list[float] = Field(default_factory=list)


class CommitDiffSummaryGetResponse(BaseModel):
    """Fetch-by-key response."""

    found: bool
    record: Optional[CommitDiffSummaryRecord] = None


class SymbolLocation(BaseModel):
    """A symbol definition location in a response."""

    file_key: str
    line: int
    col: int
    kind: str
    qualified_name: str = ""
    extent_end_line: int = 0
    abs_path: str = ""
    context_id: str = ""


class ReferenceLocation(BaseModel):
    """A reference location in a response."""

    file_key: str
    line: int
    col: int
    kind: str = "unknown"
    abs_path: str = ""
    context_id: str = ""


class ReferencesResponse(BaseModel):
    """Response for /query/references."""

    symbol: str
    definition: Optional[SymbolLocation] = None
    references: list[ReferenceLocation] = Field(default_factory=list)
    confidence: ConfidenceEnvelope


class DefinitionResponse(BaseModel):
    """Response for /query/definition."""

    symbol: str
    definitions: list[SymbolLocation] = Field(default_factory=list)
    confidence: ConfidenceEnvelope


class CallEdgeResponse(BaseModel):
    """A single call edge in a call-graph response."""

    caller: str
    callee: str
    file_key: str
    line: int
    abs_path: str = ""
    context_id: str = ""


class CallGraphResponse(BaseModel):
    """Response for /query/call-graph."""

    symbol: str
    edges: list[CallEdgeResponse] = Field(default_factory=list)
    confidence: ConfidenceEnvelope


class FileSymbolsResponse(BaseModel):
    """Response for /query/file-symbols."""

    file_key: str
    symbols: list[SymbolLocation] = Field(default_factory=list)
    confidence: ConfidenceEnvelope


class CacheInvalidateResponse(BaseModel):
    """Response for /cache/invalidate."""

    invalidated_files: int
    message: str


class WorkspaceInfoResponse(BaseModel):
    """Workspace details."""

    workspace_id: str
    root_path: str
    manifest_path: str
    repos: list[str] = Field(default_factory=list)
    contexts: list[str] = Field(default_factory=list)


class WorkspaceRefreshResponse(BaseModel):
    """Manifest refresh result."""

    workspace_id: str
    repos_synced: int
    message: str


class ContextCreateOverlayResponse(BaseModel):
    """Overlay context creation result."""

    context_id: str
    workspace_id: str
    base_context_id: str
    overlay_mode: OverlayMode
    overlay_file_count: int = 0
    overlay_row_count: int = 0
    partial_overlay: bool = False


class ContextExpireResponse(BaseModel):
    """Overlay context expiry response."""

    context_id: str
    expired: bool
    message: str


class WebhookGitLabResponse(BaseModel):
    """GitLab webhook ingestion response."""

    accepted: bool
    index_job_id: str = ""
    sync_job_id: str = ""
    message: str = ""


class HealthResponse(BaseModel):
    """Response for /health."""

    status: str = "ok"
    version: str
    cache_file_count: int = 0
    cache_symbol_count: int = 0
    rg_available: bool = False
    rg_version: str = ""
    extractor_available: bool = False
    writer_queue_depth: int = 0
    writer_lag_ms: float = 0.0
    active_context_count: int = 0
    overlay_disk_usage_bytes: int = 0
    index_queue_depth: int = 0
    oldest_pending_job_age_s: float = 0.0
    sync_queue_depth: int = 0
    active_sync_jobs: int = 0
    last_sync_failure_count_1h: int = 0
    sqlite_vec_loaded: bool = False
