"""Canonical tool registry for MCP and function-calling artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from cxxtract.models import (
    CacheInvalidateRequest,
    CacheInvalidateResponse,
    CallGraphRequest,
    CallGraphResponse,
    ClassifyFreshnessRequest,
    ClassifyFreshnessResponse,
    CommitDiffSummaryGetResponse,
    CommitDiffSummaryRecord,
    CommitDiffSummarySearchRequest,
    CommitDiffSummarySearchResponse,
    CommitDiffSummaryUpsertRequest,
    ContextCreateOverlayRequest,
    ContextCreateOverlayResponse,
    ContextExpireResponse,
    DefinitionResponse,
    FetchCallEdgesRequest,
    FetchCallEdgesResponse,
    FetchReferencesRequest,
    FetchReferencesResponse,
    FetchSymbolsRequest,
    FetchSymbolsResponse,
    FileSymbolsRequest,
    FileSymbolsResponse,
    GetCompileCommandRequest,
    GetCompileCommandResponse,
    GetConfidenceRequest,
    GetConfidenceResponse,
    HealthResponse,
    ListCandidatesRequest,
    ListCandidatesResponse,
    ParseFileRequest,
    ParseFileResponse,
    ReadFileRequest,
    ReadFileResponse,
    RepoSyncBatchRequest,
    RepoSyncBatchResponse,
    RepoSyncAllRequest,
    RepoSyncAllResponse,
    RepoSyncJobResponse,
    RepoSyncRequest,
    RepoSyncStatusResponse,
    ReferencesResponse,
    RgSearchRequest,
    RgSearchResponse,
    SymbolQueryRequest,
    WebhookGitLabRequest,
    WebhookGitLabResponse,
    WorkspaceInfoResponse,
    WorkspaceRefreshResponse,
    WorkspaceRegisterRequest,
)

ToolClass = Literal["aggregated", "atomic", "operational"]


@dataclass(frozen=True)
class ToolSpec:
    """Canonical definition for one agent-facing tool."""

    name: str
    method: Literal["GET", "POST"]
    path: str
    group: str
    tool_class: ToolClass
    side_effectful: bool
    what: str
    prerequisites: str
    output_notes: str
    request_model: type[BaseModel] | None = None
    response_model: type[BaseModel] | None = None
    path_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()


def _spec(
    *,
    name: str,
    method: Literal["GET", "POST"],
    path: str,
    group: str,
    tool_class: ToolClass,
    side_effectful: bool,
    what: str,
    prerequisites: str,
    output_notes: str,
    request_model: type[BaseModel] | None = None,
    response_model: type[BaseModel] | None = None,
    path_params: tuple[str, ...] = (),
    query_params: tuple[str, ...] = (),
) -> ToolSpec:
    return ToolSpec(
        name=name,
        method=method,
        path=path,
        group=group,
        tool_class=tool_class,
        side_effectful=side_effectful,
        what=what,
        prerequisites=prerequisites,
        output_notes=output_notes,
        request_model=request_model,
        response_model=response_model,
        path_params=path_params,
        query_params=query_params,
    )


TOOL_SPECS: tuple[ToolSpec, ...] = (
    _spec(
        name="cxxtract.query.references",
        method="POST",
        path="/query/references",
        group="query",
        tool_class="aggregated",
        side_effectful=False,
        what="Resolve a symbol, refresh semantic facts as needed, and return references with confidence.",
        prerequisites="Known or plausible symbol string and workspace_id.",
        output_notes="Returns references plus ConfidenceEnvelope.",
        request_model=SymbolQueryRequest,
        response_model=ReferencesResponse,
    ),
    _spec(
        name="cxxtract.query.definition",
        method="POST",
        path="/query/definition",
        group="query",
        tool_class="aggregated",
        side_effectful=False,
        what="Resolve one or more symbol definitions in a single fast-track call.",
        prerequisites="Known or plausible symbol string and workspace_id.",
        output_notes="Returns definitions plus ConfidenceEnvelope.",
        request_model=SymbolQueryRequest,
        response_model=DefinitionResponse,
    ),
    _spec(
        name="cxxtract.query.call_graph",
        method="POST",
        path="/query/call-graph",
        group="query",
        tool_class="aggregated",
        side_effectful=False,
        what="Return call edges around a function symbol with one-shot orchestration.",
        prerequisites="Qualified function symbol and workspace_id.",
        output_notes="Returns edges plus ConfidenceEnvelope.",
        request_model=CallGraphRequest,
        response_model=CallGraphResponse,
    ),
    _spec(
        name="cxxtract.query.file_symbols",
        method="POST",
        path="/query/file-symbols",
        group="query",
        tool_class="aggregated",
        side_effectful=False,
        what="Return symbols for one canonical file key.",
        prerequisites="workspace_id and canonical file_key (repo_id:rel/path).",
        output_notes="Returns file symbols plus ConfidenceEnvelope.",
        request_model=FileSymbolsRequest,
        response_model=FileSymbolsResponse,
    ),
    _spec(
        name="cxxtract.explore.rg_search",
        method="POST",
        path="/explore/rg-search",
        group="explore",
        tool_class="atomic",
        side_effectful=False,
        what="Run bounded lexical recall with evidence for candidate discovery.",
        prerequisites="workspace_id and query string.",
        output_notes="Returns hits, EvidenceItem[], CostEnvelope, CoverageEnvelope.",
        request_model=RgSearchRequest,
        response_model=RgSearchResponse,
    ),
    _spec(
        name="cxxtract.explore.read_file",
        method="POST",
        path="/explore/read-file",
        group="explore",
        tool_class="atomic",
        side_effectful=False,
        what="Read a bounded line/byte slice from one canonical file key.",
        prerequisites="workspace_id and canonical file_key.",
        output_notes="Returns content slice, truncation signal, CostEnvelope.",
        request_model=ReadFileRequest,
        response_model=ReadFileResponse,
    ),
    _spec(
        name="cxxtract.explore.get_compile_command",
        method="POST",
        path="/explore/get-compile-command",
        group="explore",
        tool_class="atomic",
        side_effectful=False,
        what="Inspect the effective compile command and flags hash for semantic trust checks.",
        prerequisites="workspace_id and canonical file_key.",
        output_notes="Returns compile match_type, cwd, args, flags_hash.",
        request_model=GetCompileCommandRequest,
        response_model=GetCompileCommandResponse,
    ),
    _spec(
        name="cxxtract.explore.list_candidates",
        method="POST",
        path="/explore/list-candidates",
        group="explore",
        tool_class="atomic",
        side_effectful=False,
        what="Generate merged candidate file keys with provenance and truncation metadata.",
        prerequisites="workspace_id and target symbol.",
        output_notes="Returns candidates, provenance, CostEnvelope, CoverageEnvelope.",
        request_model=ListCandidatesRequest,
        response_model=ListCandidatesResponse,
    ),
    _spec(
        name="cxxtract.explore.classify_freshness",
        method="POST",
        path="/explore/classify-freshness",
        group="explore",
        tool_class="atomic",
        side_effectful=False,
        what="Classify candidate files into fresh/stale/unparsed and emit parse queue descriptors.",
        prerequisites="workspace_id and candidate_file_keys.",
        output_notes="Returns freshness buckets, parse_queue, CostEnvelope, CoverageEnvelope.",
        request_model=ClassifyFreshnessRequest,
        response_model=ClassifyFreshnessResponse,
    ),
    _spec(
        name="cxxtract.explore.parse_file",
        method="POST",
        path="/explore/parse-file",
        group="explore",
        tool_class="atomic",
        side_effectful=True,
        what="Run on-demand semantic parsing and persist facts through single-writer pipeline.",
        prerequisites="workspace_id and file_keys selected for parsing.",
        output_notes="Returns parsed/failed/skipped lists, persisted_fact_rows, CostEnvelope, CoverageEnvelope.",
        request_model=ParseFileRequest,
        response_model=ParseFileResponse,
    ),
    _spec(
        name="cxxtract.explore.fetch_symbols",
        method="POST",
        path="/explore/fetch-symbols",
        group="explore",
        tool_class="atomic",
        side_effectful=False,
        what="Fetch semantic symbol rows from cache for explicit candidate sets.",
        prerequisites="workspace_id and target symbol; candidate_file_keys optional but recommended.",
        output_notes="Returns symbol rows with evidence and Cost/Coverage envelopes.",
        request_model=FetchSymbolsRequest,
        response_model=FetchSymbolsResponse,
    ),
    _spec(
        name="cxxtract.explore.fetch_references",
        method="POST",
        path="/explore/fetch-references",
        group="explore",
        tool_class="atomic",
        side_effectful=False,
        what="Fetch semantic reference rows from cache for explicit candidate sets.",
        prerequisites="workspace_id and target symbol; candidate_file_keys optional but recommended.",
        output_notes="Returns references with evidence and Cost/Coverage envelopes.",
        request_model=FetchReferencesRequest,
        response_model=FetchReferencesResponse,
    ),
    _spec(
        name="cxxtract.explore.fetch_call_edges",
        method="POST",
        path="/explore/fetch-call-edges",
        group="explore",
        tool_class="atomic",
        side_effectful=False,
        what="Fetch call edges for explicit symbols and direction from semantic cache.",
        prerequisites="workspace_id, symbol, direction, and candidate_file_keys for high precision.",
        output_notes="Returns call edges with evidence and Cost/Coverage envelopes.",
        request_model=FetchCallEdgesRequest,
        response_model=FetchCallEdgesResponse,
    ),
    _spec(
        name="cxxtract.explore.get_confidence",
        method="POST",
        path="/explore/get-confidence",
        group="explore",
        tool_class="atomic",
        side_effectful=False,
        what="Compute ConfidenceEnvelope from explicit verified/stale/unparsed file sets.",
        prerequisites="Provide verification sets from prior exploration steps.",
        output_notes="Returns ConfidenceEnvelope and CoverageEnvelope.",
        request_model=GetConfidenceRequest,
        response_model=GetConfidenceResponse,
    ),
    _spec(
        name="cxxtract.cache.invalidate",
        method="POST",
        path="/cache/invalidate",
        group="cache",
        tool_class="operational",
        side_effectful=True,
        what="Invalidate cached semantic facts for one context or a selected file set.",
        prerequisites="workspace_id and optional context_id/file_keys.",
        output_notes="Returns invalidated_files count and status message.",
        request_model=CacheInvalidateRequest,
        response_model=CacheInvalidateResponse,
    ),
    _spec(
        name="cxxtract.workspace.register",
        method="POST",
        path="/workspace/register",
        group="workspace",
        tool_class="operational",
        side_effectful=True,
        what="Register or refresh workspace manifest binding for API operations.",
        prerequisites="workspace_id, root_path, and manifest_path.",
        output_notes="Returns workspace metadata and known contexts.",
        request_model=WorkspaceRegisterRequest,
        response_model=WorkspaceInfoResponse,
    ),
    _spec(
        name="cxxtract.workspace.get",
        method="GET",
        path="/workspace/{workspace_id}",
        group="workspace",
        tool_class="operational",
        side_effectful=False,
        what="Read workspace metadata, repos, and active contexts.",
        prerequisites="workspace_id path parameter.",
        output_notes="Returns WorkspaceInfoResponse.",
        response_model=WorkspaceInfoResponse,
        path_params=("workspace_id",),
    ),
    _spec(
        name="cxxtract.workspace.refresh_manifest",
        method="POST",
        path="/workspace/{workspace_id}/refresh-manifest",
        group="workspace",
        tool_class="operational",
        side_effectful=True,
        what="Reload workspace manifest from disk and refresh repo mapping.",
        prerequisites="workspace_id path parameter.",
        output_notes="Returns repos_synced count and message.",
        response_model=WorkspaceRefreshResponse,
        path_params=("workspace_id",),
    ),
    _spec(
        name="cxxtract.context.create_pr_overlay",
        method="POST",
        path="/context/create-pr-overlay",
        group="context",
        tool_class="operational",
        side_effectful=True,
        what="Create a PR overlay context on top of a baseline for sparse review workflows.",
        prerequisites="workspace_id and PR/base/head identifiers.",
        output_notes="Returns new context_id and overlay mode stats.",
        request_model=ContextCreateOverlayRequest,
        response_model=ContextCreateOverlayResponse,
    ),
    _spec(
        name="cxxtract.context.expire",
        method="POST",
        path="/context/{context_id}/expire",
        group="context",
        tool_class="operational",
        side_effectful=True,
        what="Expire an overlay context and release associated resources.",
        prerequisites="context_id path parameter.",
        output_notes="Returns expired boolean and message.",
        response_model=ContextExpireResponse,
        path_params=("context_id",),
    ),
    _spec(
        name="cxxtract.webhook.gitlab",
        method="POST",
        path="/webhooks/gitlab",
        group="webhook",
        tool_class="operational",
        side_effectful=True,
        what="Ingest GitLab webhook payloads and enqueue downstream sync/index work.",
        prerequisites="event_type and payload with workspace/repo identifiers.",
        output_notes="Returns accepted flag and optional job ids.",
        request_model=WebhookGitLabRequest,
        response_model=WebhookGitLabResponse,
    ),
    _spec(
        name="cxxtract.sync.repo",
        method="POST",
        path="/workspace/{workspace_id}/sync-repo",
        group="sync",
        tool_class="operational",
        side_effectful=True,
        what="Enqueue deterministic sync for one repo at exact commit SHA.",
        prerequisites="workspace_id path parameter and repo sync request body.",
        output_notes="Returns RepoSyncJobResponse.",
        request_model=RepoSyncRequest,
        response_model=RepoSyncJobResponse,
        path_params=("workspace_id",),
    ),
    _spec(
        name="cxxtract.sync.batch",
        method="POST",
        path="/workspace/{workspace_id}/sync-batch",
        group="sync",
        tool_class="operational",
        side_effectful=True,
        what="Enqueue sync jobs for multiple repos in one request.",
        prerequisites="workspace_id path parameter and targets list.",
        output_notes="Returns list of created sync jobs.",
        request_model=RepoSyncBatchRequest,
        response_model=RepoSyncBatchResponse,
        path_params=("workspace_id",),
    ),
    _spec(
        name="cxxtract.sync.all_repos",
        method="POST",
        path="/workspace/{workspace_id}/sync-all-repos",
        group="sync",
        tool_class="operational",
        side_effectful=True,
        what="Enqueue sync jobs for all sync-enabled repos in manifest.",
        prerequisites="workspace_id path parameter.",
        output_notes="Returns jobs list and skipped repos.",
        request_model=RepoSyncAllRequest,
        response_model=RepoSyncAllResponse,
        path_params=("workspace_id",),
    ),
    _spec(
        name="cxxtract.sync.job_get",
        method="GET",
        path="/sync-jobs/{job_id}",
        group="sync",
        tool_class="operational",
        side_effectful=False,
        what="Read sync job status and diagnostic fields.",
        prerequisites="job_id path parameter.",
        output_notes="Returns RepoSyncJobResponse.",
        response_model=RepoSyncJobResponse,
        path_params=("job_id",),
    ),
    _spec(
        name="cxxtract.sync.status",
        method="GET",
        path="/workspace/{workspace_id}/repos/{repo_id}/sync-status",
        group="sync",
        tool_class="operational",
        side_effectful=False,
        what="Read latest sync status snapshot for one repository.",
        prerequisites="workspace_id and repo_id path parameters.",
        output_notes="Returns RepoSyncStatusResponse.",
        response_model=RepoSyncStatusResponse,
        path_params=("workspace_id", "repo_id"),
    ),
    _spec(
        name="cxxtract.vector.upsert",
        method="POST",
        path="/commit-diff-summaries/upsert",
        group="vector",
        tool_class="operational",
        side_effectful=True,
        what="Store or update commit diff summary and embedding vector.",
        prerequisites="workspace/repo/commit identifiers, summary text, embedding payload.",
        output_notes="Returns stored CommitDiffSummaryRecord.",
        request_model=CommitDiffSummaryUpsertRequest,
        response_model=CommitDiffSummaryRecord,
    ),
    _spec(
        name="cxxtract.vector.search",
        method="POST",
        path="/commit-diff-summaries/search",
        group="vector",
        tool_class="operational",
        side_effectful=False,
        what="Run top-k vector search over commit summary embeddings.",
        prerequisites="query_embedding and workspace scope.",
        output_notes="Returns ranked commit summary hits.",
        request_model=CommitDiffSummarySearchRequest,
        response_model=CommitDiffSummarySearchResponse,
    ),
    _spec(
        name="cxxtract.vector.get",
        method="GET",
        path="/commit-diff-summaries/{workspace_id}/{repo_id}/{commit_sha}",
        group="vector",
        tool_class="operational",
        side_effectful=False,
        what="Fetch one commit diff summary record by composite key.",
        prerequisites="workspace_id, repo_id, commit_sha path parameters.",
        output_notes="Returns found flag and optional record.",
        response_model=CommitDiffSummaryGetResponse,
        path_params=("workspace_id", "repo_id", "commit_sha"),
        query_params=("include_embedding",),
    ),
    _spec(
        name="cxxtract.health.get",
        method="GET",
        path="/health",
        group="health",
        tool_class="operational",
        side_effectful=False,
        what="Read service health, queue depths, tool availability, and vector status.",
        prerequisites="No inputs.",
        output_notes="Returns HealthResponse counters and availability flags.",
        response_model=HealthResponse,
    ),
)


COMMON_COMPONENT_MODELS: tuple[type[BaseModel], ...] = ()


def get_tool_spec(name: str) -> ToolSpec | None:
    """Return tool spec by canonical name."""
    for spec in TOOL_SPECS:
        if spec.name == name:
            return spec
    return None


def _get_path_or_query_param_schema(name: str) -> dict[str, Any]:
    if name == "include_embedding":
        return {
            "type": "boolean",
            "default": False,
            "description": "Include embedding values in vector.get response when true.",
        }
    return {
        "type": "string",
        "minLength": 1,
        "description": f"{name} path/query parameter.",
    }


def _base_input_schema(spec: ToolSpec) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
        "x-tool-class": spec.tool_class,
        "x-side-effectful": spec.side_effectful,
    }


def get_input_schema(spec: ToolSpec) -> dict[str, Any]:
    """Build the tool input JSON schema for MCP/function definitions."""
    schema = _base_input_schema(spec)
    properties = schema["properties"]
    required = set(schema["required"])

    for param in spec.path_params:
        properties[param] = _get_path_or_query_param_schema(param)
        required.add(param)
    for param in spec.query_params:
        properties[param] = _get_path_or_query_param_schema(param)

    if spec.request_model is not None:
        model_schema = spec.request_model.model_json_schema(ref_template="#/$defs/{model}")
        defs = model_schema.get("$defs")
        if isinstance(defs, dict) and defs:
            schema["$defs"] = defs
        for key, value in model_schema.get("properties", {}).items():
            properties[key] = value
        for key in model_schema.get("required", []):
            required.add(key)
        if model_schema.get("description"):
            schema["description"] = model_schema["description"]

    schema["required"] = sorted(required)
    if not schema["required"]:
        schema.pop("required")
    return schema


def _description_for_tool_class(spec: ToolSpec) -> str:
    if spec.tool_class == "aggregated":
        return (
            "When to use: Prefer this fast-track aggregated tool when the target symbol/function is already known and "
            "you want a direct answer in one round trip.\n"
            "When not to use: Avoid this when symbol identity is uncertain or you need explicit stepwise evidence "
            "and bounded intermediate costs; use `cxxtract.explore.*` tools instead."
        )
    if spec.tool_class == "atomic":
        return (
            "When to use: Use this atomic tool in iterative exploration chains to control recall, freshness, parsing, "
            "and semantic verification step-by-step with explicit evidence and bounded Cost/Coverage envelopes.\n"
            "When not to use: Avoid this for simple known-symbol lookups where a `cxxtract.query.*` fast-track tool "
            "can answer directly with lower orchestration overhead."
        )
    return (
        "When to use: Use this operational tool only when workspace/context/cache/sync/vector state actions are "
        "explicitly intended by the task.\n"
        "When not to use: Avoid for pure read-only semantic analysis unless this exact operational state/control "
        "operation is required."
    )


def build_tool_description(spec: ToolSpec) -> str:
    """Build detailed, agent-friendly description text."""
    caution = ""
    if spec.side_effectful:
        caution = (
            "\nSide effects: This tool mutates service/workspace state. Call only with explicit intent and include "
            "clear rationale in your plan."
        )

    response_name = spec.response_model.__name__ if spec.response_model is not None else "object"
    return (
        f"What this tool does: {spec.what}\n"
        f"{_description_for_tool_class(spec)}\n"
        f"Input prerequisites: {spec.prerequisites}\n"
        f"Expected output: {spec.output_notes} Response model: `{response_name}`."
        f"{caution}"
    )


def validate_arguments(spec: ToolSpec, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Validate raw tool arguments and return normalized path/query/body buckets."""
    args = arguments or {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be a JSON object")

    path_values: dict[str, Any] = {}
    query_values: dict[str, Any] = {}

    for key in spec.path_params:
        value = args.get(key, "")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"missing required path parameter: {key}")
        path_values[key] = value

    for key in spec.query_params:
        if key not in args:
            continue
        value = args[key]
        if key == "include_embedding":
            query_values[key] = bool(value)
        elif isinstance(value, str) and value.strip():
            query_values[key] = value
        else:
            raise ValueError(f"invalid query parameter: {key}")

    excluded = set(spec.path_params) | set(spec.query_params)
    body_input = {k: v for k, v in args.items() if k not in excluded}

    if spec.method == "GET":
        if body_input:
            unknown = ", ".join(sorted(body_input.keys()))
            raise ValueError(f"unexpected arguments for GET tool: {unknown}")
        return {"path": path_values, "query": query_values, "body": None}

    if spec.request_model is None:
        if body_input:
            unknown = ", ".join(sorted(body_input.keys()))
            raise ValueError(f"unexpected body fields: {unknown}")
        return {"path": path_values, "query": query_values, "body": {}}

    validated_body = spec.request_model.model_validate(body_input).model_dump(
        mode="json",
        exclude_none=False,
    )
    return {"path": path_values, "query": query_values, "body": validated_body}


def export_mcp_tool_definition(spec: ToolSpec) -> dict[str, Any]:
    """Convert a ToolSpec into MCP tools/list shape."""
    return {
        "name": spec.name,
        "description": build_tool_description(spec),
        "inputSchema": get_input_schema(spec),
        "x-tool-class": spec.tool_class,
        "x-side-effectful": spec.side_effectful,
        "x-http": {"method": spec.method, "path": spec.path},
        "x-expected-output": (
            {
                "model": spec.response_model.__name__,
                "$ref": f"#/components/schemas/{spec.response_model.__name__}",
            }
            if spec.response_model is not None
            else {}
        ),
    }


def collect_model_classes() -> list[type[BaseModel]]:
    """Collect request/response/common model classes used by registry."""
    seen: dict[str, type[BaseModel]] = {}
    for spec in TOOL_SPECS:
        for model in (spec.request_model, spec.response_model):
            if model is None:
                continue
            seen[model.__name__] = model
    for model in COMMON_COMPONENT_MODELS:
        seen[model.__name__] = model
    return [seen[k] for k in sorted(seen.keys())]


def collect_component_schemas() -> dict[str, Any]:
    """Collect JSON schemas for all referenced models with flattened defs."""
    components: dict[str, Any] = {}

    def add_model(model: type[BaseModel]) -> None:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        defs = schema.pop("$defs", {})
        if isinstance(defs, dict):
            for key, value in defs.items():
                components.setdefault(key, value)
        components[model.__name__] = schema

    for model in collect_model_classes():
        add_model(model)
    return components


def route_inventory() -> set[tuple[str, str]]:
    """Return (METHOD, PATH) inventory from specs."""
    return {(spec.method, spec.path) for spec in TOOL_SPECS}

