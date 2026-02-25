"""Pydantic models for API requests, responses, and internal data transfer."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ============================================================
# Enumerations
# ============================================================

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


# ============================================================
# Internal / cpp-extractor output models
# ============================================================

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
    symbol: str  # qualified name of the referenced symbol
    line: int
    col: int
    kind: str = "unknown"


class ExtractedCallEdge(BaseModel):
    """A call edge extracted from the AST."""
    caller: str  # qualified name
    callee: str  # qualified name
    line: int


class ExtractedIncludeDep(BaseModel):
    """An include dependency extracted from the AST."""
    path: str
    depth: int = 1


class ExtractorOutput(BaseModel):
    """JSON output schema from cpp-extractor.exe for a single file."""
    file: str
    symbols: list[ExtractedSymbol] = Field(default_factory=list)
    references: list[ExtractedReference] = Field(default_factory=list)
    call_edges: list[ExtractedCallEdge] = Field(default_factory=list)
    include_deps: list[ExtractedIncludeDep] = Field(default_factory=list)
    success: bool = True
    diagnostics: list[str] = Field(default_factory=list)


# ============================================================
# Recall models
# ============================================================

class RecallHit(BaseModel):
    """A candidate file identified by ripgrep."""
    file_path: str
    line_number: int
    line_text: str


# ============================================================
# Confidence envelope
# ============================================================

class ConfidenceEnvelope(BaseModel):
    """Communicates exactly how much of the codebase was semantically verified."""
    verified_files: list[str] = Field(default_factory=list)
    stale_files: list[str] = Field(default_factory=list)
    unparsed_files: list[str] = Field(default_factory=list)
    total_candidates: int = 0
    verified_ratio: float = 0.0


# ============================================================
# API request models
# ============================================================

class SymbolQueryRequest(BaseModel):
    """Request body for /query/references and /query/definition."""
    symbol: str = Field(..., description="Qualified or unqualified C++ symbol name")
    repo_root: str = Field(..., description="Absolute path to repository root")
    compile_commands: Optional[str] = Field(
        None,
        description="Path to compile_commands.json (overrides default)",
    )
    max_recall_files: Optional[int] = Field(
        None,
        description="Override max candidate files from ripgrep",
    )
    max_parse_workers: Optional[int] = Field(
        None,
        description="Override max concurrent cpp-extractor processes",
    )


class CallGraphRequest(BaseModel):
    """Request body for /query/call-graph."""
    symbol: str = Field(..., description="Qualified function name")
    repo_root: str = Field(..., description="Absolute path to repository root")
    compile_commands: Optional[str] = None
    direction: CallGraphDirection = CallGraphDirection.BOTH
    max_depth: int = Field(3, ge=1, le=10, description="Max traversal depth")
    max_recall_files: Optional[int] = None
    max_parse_workers: Optional[int] = None


class FileSymbolsRequest(BaseModel):
    """Request body for /query/file-symbols."""
    file_path: str = Field(..., description="Absolute path to the source file")
    repo_root: str = Field(..., description="Absolute path to repository root")
    compile_commands: Optional[str] = None


class CacheInvalidateRequest(BaseModel):
    """Request body for /cache/invalidate."""
    file_paths: Optional[list[str]] = Field(
        None,
        description="Specific files to invalidate. If None, invalidate entire cache.",
    )


# ============================================================
# API response models
# ============================================================

class SymbolLocation(BaseModel):
    """A symbol definition location in a response."""
    file: str
    line: int
    col: int
    kind: str
    qualified_name: str = ""
    extent_end_line: int = 0


class ReferenceLocation(BaseModel):
    """A reference location in a response."""
    file: str
    line: int
    col: int
    kind: str = "unknown"


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
    file: str
    line: int


class CallGraphResponse(BaseModel):
    """Response for /query/call-graph."""
    symbol: str
    edges: list[CallEdgeResponse] = Field(default_factory=list)
    confidence: ConfidenceEnvelope


class FileSymbolsResponse(BaseModel):
    """Response for /query/file-symbols."""
    file: str
    symbols: list[SymbolLocation] = Field(default_factory=list)
    confidence: ConfidenceEnvelope


class CacheInvalidateResponse(BaseModel):
    """Response for /cache/invalidate."""
    invalidated_files: int
    message: str


class HealthResponse(BaseModel):
    """Response for /health."""
    status: str = "ok"
    version: str
    cache_file_count: int = 0
    cache_symbol_count: int = 0
    rg_available: bool = False
    extractor_available: bool = False
