/**
 * @file extractor.cpp
 * @brief Production implementation using the libclang C API.
 *
 * Uses CXIndex / CXTranslationUnit to parse C++ source and
 * clang_visitChildren to walk the AST.  Collects:
 *   - Symbol definitions via declaration cursor kinds
 *   - References via CXCursor_DeclRefExpr / CXCursor_MemberRefExpr
 *   - Call edges via CXCursor_CallExpr
 *   - Include deps via clang_getInclusions()
 *   - Diagnostics via clang_getDiagnostic()
 */

#include "extractor.h"

#include <clang-c/Index.h>

#include <algorithm>
#include <cstring>
#include <iostream>
#include <set>
#include <string>
#include <unordered_set>
#include <vector>

namespace cxxtract {

// ==================================================================
// JSON serialisation
// ==================================================================

void to_json(nlohmann::json& j, const SymbolInfo& s) {
    j = nlohmann::json{
        {"name",            s.name},
        {"qualified_name",  s.qualified_name},
        {"kind",            s.kind},
        {"line",            s.line},
        {"col",             s.col},
        {"extent_end_line", s.extent_end_line}
    };
}

void to_json(nlohmann::json& j, const ReferenceInfo& r) {
    j = nlohmann::json{
        {"symbol", r.symbol},
        {"line",   r.line},
        {"col",    r.col},
        {"kind",   r.kind}
    };
}

void to_json(nlohmann::json& j, const CallEdge& e) {
    j = nlohmann::json{
        {"caller", e.caller},
        {"callee", e.callee},
        {"line",   e.line}
    };
}

void to_json(nlohmann::json& j, const IncludeDep& d) {
    j = nlohmann::json{
        {"path",  d.path},
        {"depth", d.depth}
    };
}

void to_json(nlohmann::json& j, const ExtractionResult& r) {
    j = nlohmann::json{
        {"file",         r.file},
        {"symbols",      r.symbols},
        {"references",   r.references},
        {"call_edges",   r.call_edges},
        {"include_deps", r.include_deps},
        {"success",      r.success},
        {"diagnostics",  r.diagnostics}
    };
}


// ==================================================================
// Internal helpers
// ==================================================================

namespace {

/// RAII wrapper for CXString.
class ClangString {
public:
    explicit ClangString(CXString s) : s_(s) {}
    ~ClangString() { clang_disposeString(s_); }
    ClangString(const ClangString&) = delete;
    ClangString& operator=(const ClangString&) = delete;

    const char* c_str() const {
        const char* p = clang_getCString(s_);
        return p ? p : "";
    }

    std::string str() const { return std::string(c_str()); }

private:
    CXString s_;
};


/// Build a fully qualified name by walking the semantic parent chain.
std::string build_qualified_name(CXCursor cursor) {
    std::vector<std::string> parts;
    CXCursor current = cursor;

    while (!clang_Cursor_isNull(current) &&
           clang_getCursorKind(current) != CXCursor_TranslationUnit) {
        ClangString name(clang_getCursorSpelling(current));
        std::string name_str = name.str();

        CXCursorKind kind = clang_getCursorKind(current);

        // For destructors: libclang's spelling already includes "~" in the
        // name (e.g. "~Session"), so do NOT prepend another tilde.
        // For constructors: spelling is the class name, which is correct.
        (void)kind;  // no special handling needed

        if (!name_str.empty()) {
            parts.push_back(name_str);
        }

        current = clang_getCursorSemanticParent(current);
    }

    // Reverse to get outer::inner::name order
    std::reverse(parts.begin(), parts.end());

    std::string result;
    for (size_t i = 0; i < parts.size(); ++i) {
        if (i > 0) result += "::";
        result += parts[i];
    }
    return result.empty() ? "(unnamed)" : result;
}


/// Map a CXCursorKind to our SymbolKind string (matching Python enum).
std::string cursor_kind_to_symbol_kind(CXCursorKind kind, CXCursor cursor) {
    switch (kind) {
        case CXCursor_FunctionDecl:          return "Function";
        case CXCursor_CXXMethod:             return "CXXMethod";
        case CXCursor_Constructor:           return "Constructor";
        case CXCursor_Destructor:            return "Destructor";
        case CXCursor_FunctionTemplate:      return "FunctionTemplate";
        case CXCursor_ClassTemplate:         return "ClassTemplate";
        case CXCursor_ClassDecl:             return "ClassDecl";
        case CXCursor_StructDecl:            return "StructDecl";
        case CXCursor_UnionDecl:             return "StructDecl"; // map union -> struct
        case CXCursor_EnumDecl:              return "EnumDecl";
        case CXCursor_EnumConstantDecl:      return "EnumConstant";
        case CXCursor_VarDecl:               return "VarDecl";
        case CXCursor_FieldDecl:             return "FieldDecl";
        case CXCursor_TypedefDecl:           return "Typedef";
        case CXCursor_TypeAliasDecl:         return "TypeAlias";
        case CXCursor_Namespace:             return "Namespace";
        case CXCursor_MacroDefinition:       return "Macro";
        default:                             return "Unknown";
    }
}


/// Check if a cursor kind represents a symbol definition we want to collect.
bool is_symbol_kind(CXCursorKind kind) {
    switch (kind) {
        case CXCursor_FunctionDecl:
        case CXCursor_CXXMethod:
        case CXCursor_Constructor:
        case CXCursor_Destructor:
        case CXCursor_FunctionTemplate:
        case CXCursor_ClassTemplate:
        case CXCursor_ClassDecl:
        case CXCursor_StructDecl:
        case CXCursor_UnionDecl:
        case CXCursor_EnumDecl:
        case CXCursor_EnumConstantDecl:
        case CXCursor_VarDecl:
        case CXCursor_FieldDecl:
        case CXCursor_TypedefDecl:
        case CXCursor_TypeAliasDecl:
        case CXCursor_Namespace:
            return true;
        default:
            return false;
    }
}


/// Check if a cursor kind is a function-like definition (can be a caller).
bool is_function_like(CXCursorKind kind) {
    switch (kind) {
        case CXCursor_FunctionDecl:
        case CXCursor_CXXMethod:
        case CXCursor_Constructor:
        case CXCursor_Destructor:
        case CXCursor_FunctionTemplate:
        case CXCursor_LambdaExpr:
            return true;
        default:
            return false;
    }
}


/// Check if a cursor is in the main file (not a system/included header).
bool is_in_main_file(CXCursor cursor, CXTranslationUnit tu) {
    CXSourceLocation loc = clang_getCursorLocation(cursor);
    return clang_Location_isFromMainFile(loc) != 0;
}


/// Check if a cursor location is in a system header.
bool is_in_system_header(CXCursor cursor) {
    CXSourceLocation loc = clang_getCursorLocation(cursor);
    return clang_Location_isInSystemHeader(loc) != 0;
}


/// Get line/col from a cursor location.
struct LocInfo {
    unsigned line = 0;
    unsigned col  = 0;
    bool valid    = false;
};

LocInfo get_cursor_loc(CXCursor cursor) {
    CXSourceLocation loc = clang_getCursorLocation(cursor);
    if (clang_equalLocations(loc, clang_getNullLocation())) {
        return {};
    }
    unsigned line = 0, col = 0;
    clang_getSpellingLocation(loc, nullptr, &line, &col, nullptr);
    return {line, col, line > 0};
}


/// Get the end line of a cursor's extent.
unsigned get_extent_end_line(CXCursor cursor) {
    CXSourceRange range = clang_getCursorExtent(cursor);
    CXSourceLocation end = clang_getRangeEnd(range);
    unsigned line = 0;
    clang_getSpellingLocation(end, nullptr, &line, nullptr, nullptr);
    return line;
}


/// Find the nearest enclosing function-like cursor by walking parents.
CXCursor find_enclosing_function(CXCursor cursor, CXTranslationUnit tu) {
    CXCursor parent = clang_getCursorSemanticParent(cursor);
    int depth = 0;
    while (!clang_Cursor_isNull(parent) &&
           clang_getCursorKind(parent) != CXCursor_TranslationUnit &&
           depth < 50) {
        if (is_function_like(clang_getCursorKind(parent))) {
            return parent;
        }
        parent = clang_getCursorSemanticParent(parent);
        ++depth;
    }
    return clang_getNullCursor();
}


// ------------------------------------------------------------------
// Visitor context
// ------------------------------------------------------------------

struct VisitorContext {
    ExtractionResult* result = nullptr;
    ActionFilter filter = ActionFilter::ExtractAll;
    CXTranslationUnit tu = nullptr;
    std::string main_file;

    // Caller stack for tracking call edges
    std::vector<CXCursor> caller_stack;

    // Dedup sets
    std::set<std::string> seen_symbols;  // qualified_name + kind + line for dedup

    bool should_collect_symbols() const {
        return filter == ActionFilter::ExtractAll ||
               filter == ActionFilter::ExtractSymbols;
    }

    bool should_collect_refs() const {
        return filter == ActionFilter::ExtractAll ||
               filter == ActionFilter::ExtractRefs;
    }
};


/// Recursive AST visitor callback for libclang.
CXChildVisitResult visit_cursor(CXCursor cursor, CXCursor parent, CXClientData client_data) {
    auto* ctx = static_cast<VisitorContext*>(client_data);
    CXCursorKind kind = clang_getCursorKind(cursor);

    // Skip system headers entirely
    if (is_in_system_header(cursor)) {
        return CXChildVisit_Continue;
    }

    // ---- Symbol collection ----
    if (ctx->should_collect_symbols() && is_symbol_kind(kind)) {
        // For classes/structs/enums, only collect definitions
        if (kind == CXCursor_ClassDecl || kind == CXCursor_StructDecl ||
            kind == CXCursor_UnionDecl || kind == CXCursor_EnumDecl) {
            if (!clang_isCursorDefinition(cursor)) {
                return CXChildVisit_Recurse;
            }
        }

        // Skip VarDecl that are local variables (inside functions)
        if (kind == CXCursor_VarDecl) {
            CXCursor sem_parent = clang_getCursorSemanticParent(cursor);
            CXCursorKind parent_kind = clang_getCursorKind(sem_parent);
            if (is_function_like(parent_kind)) {
                // This is a local variable — skip it
                return CXChildVisit_Recurse;
            }
        }

        // Skip anonymous namespaces
        if (kind == CXCursor_Namespace) {
            ClangString ns_name(clang_getCursorSpelling(cursor));
            if (std::strlen(ns_name.c_str()) == 0) {
                return CXChildVisit_Recurse;
            }
        }

        auto loc_info = get_cursor_loc(cursor);
        if (loc_info.valid) {
            ClangString name(clang_getCursorSpelling(cursor));
            std::string qname = build_qualified_name(cursor);
            std::string kind_str = cursor_kind_to_symbol_kind(kind, cursor);
            unsigned end_line = get_extent_end_line(cursor);

            // Dedup key
            std::string dedup_key = qname + "|" + kind_str + "|" +
                                    std::to_string(loc_info.line);
            if (ctx->seen_symbols.find(dedup_key) == ctx->seen_symbols.end()) {
                ctx->seen_symbols.insert(dedup_key);
                ctx->result->symbols.push_back(SymbolInfo{
                    name.str(),
                    qname,
                    kind_str,
                    static_cast<int>(loc_info.line),
                    static_cast<int>(loc_info.col),
                    static_cast<int>(end_line)
                });
            }
        }
    }

    // ---- Reference & call-edge collection ----
    if (ctx->should_collect_refs()) {
        // Call expressions -> call edges + call references
        if (kind == CXCursor_CallExpr) {
            CXCursor referenced = clang_getCursorReferenced(cursor);
            if (!clang_Cursor_isNull(referenced) && !is_in_system_header(referenced)) {
                auto loc_info = get_cursor_loc(cursor);
                if (loc_info.valid) {
                    std::string callee_name = build_qualified_name(referenced);

                    // Add reference
                    ctx->result->references.push_back(ReferenceInfo{
                        callee_name,
                        static_cast<int>(loc_info.line),
                        static_cast<int>(loc_info.col),
                        "call"
                    });

                    // Find caller for call edge
                    CXCursor caller = find_enclosing_function(cursor, ctx->tu);
                    if (!clang_Cursor_isNull(caller)) {
                        std::string caller_name = build_qualified_name(caller);
                        ctx->result->call_edges.push_back(CallEdge{
                            caller_name,
                            callee_name,
                            static_cast<int>(loc_info.line)
                        });
                    }
                }
            }
        }

        // DeclRefExpr -> read/write/addr references (non-call only)
        if (kind == CXCursor_DeclRefExpr) {
            CXCursor referenced = clang_getCursorReferenced(cursor);
            if (!clang_Cursor_isNull(referenced) && !is_in_system_header(referenced)) {
                CXCursorKind ref_decl_kind = clang_getCursorKind(referenced);

                // Skip local variables and parameters
                if (ref_decl_kind == CXCursor_ParmDecl) {
                    return CXChildVisit_Recurse;
                }
                if (ref_decl_kind == CXCursor_VarDecl) {
                    CXCursor ref_parent = clang_getCursorSemanticParent(referenced);
                    if (is_function_like(clang_getCursorKind(ref_parent))) {
                        return CXChildVisit_Recurse;
                    }
                }

                // Skip if parent is a CallExpr (already handled above)
                CXCursorKind parent_kind = clang_getCursorKind(parent);
                if (parent_kind == CXCursor_CallExpr) {
                    return CXChildVisit_Recurse;
                }

                // Skip function-like decls whose DeclRefExpr appears
                // inside an UnexposedExpr wrapping a CallExpr — those
                // are already captured by the CallExpr handler above.
                if (is_function_like(ref_decl_kind) ||
                    ref_decl_kind == CXCursor_FunctionTemplate) {
                    // Walk up: if any ancestor up to 3 levels is a CallExpr,
                    // this reference is the callee of that call — skip it.
                    CXCursor ancestor = parent;
                    for (int depth = 0; depth < 3; ++depth) {
                        if (clang_Cursor_isNull(ancestor)) break;
                        CXCursorKind ak = clang_getCursorKind(ancestor);
                        if (ak == CXCursor_CallExpr) {
                            goto skip_declref;
                        }
                        if (ak == CXCursor_TranslationUnit) break;
                        ancestor = clang_getCursorLexicalParent(ancestor);
                    }
                }

                {
                    auto loc_info = get_cursor_loc(cursor);
                    if (loc_info.valid) {
                        std::string ref_name = build_qualified_name(referenced);
                        ctx->result->references.push_back(ReferenceInfo{
                            ref_name,
                            static_cast<int>(loc_info.line),
                            static_cast<int>(loc_info.col),
                            "read"
                        });
                    }
                }
                skip_declref:;
            }
        }

        // MemberRefExpr -> member references (non-call only)
        // When the parent is a CallExpr, the CallExpr handler already
        // captured this as a "call" reference + call edge, so skip.
        if (kind == CXCursor_MemberRefExpr) {
            CXCursorKind parent_kind = clang_getCursorKind(parent);
            if (parent_kind == CXCursor_CallExpr) {
                // Already handled by CallExpr visitor — skip to avoid duplicates
                return CXChildVisit_Recurse;
            }

            CXCursor referenced = clang_getCursorReferenced(cursor);
            if (!clang_Cursor_isNull(referenced) && !is_in_system_header(referenced)) {
                auto loc_info = get_cursor_loc(cursor);
                if (loc_info.valid) {
                    std::string ref_name = build_qualified_name(referenced);
                    ctx->result->references.push_back(ReferenceInfo{
                        ref_name,
                        static_cast<int>(loc_info.line),
                        static_cast<int>(loc_info.col),
                        "read"
                    });
                }
            }
        }

        // Type references
        if (kind == CXCursor_TypeRef) {
            CXCursor referenced = clang_getCursorReferenced(cursor);
            if (!clang_Cursor_isNull(referenced) && !is_in_system_header(referenced)) {
                auto loc_info = get_cursor_loc(cursor);
                if (loc_info.valid) {
                    std::string ref_name = build_qualified_name(referenced);
                    ctx->result->references.push_back(ReferenceInfo{
                        ref_name,
                        static_cast<int>(loc_info.line),
                        static_cast<int>(loc_info.col),
                        "type_ref"
                    });
                }
            }
        }
    }

    return CXChildVisit_Recurse;
}


/// Inclusion visitor callback for clang_getInclusions().
void inclusion_visitor(
    CXFile included_file,
    CXSourceLocation* inclusion_stack,
    unsigned include_len,
    CXClientData client_data
) {
    auto* result = static_cast<ExtractionResult*>(client_data);

    ClangString file_name(clang_getFileName(included_file));
    std::string path = normalise_path(file_name.str());

    if (path.empty()) return;

    // include_len is the depth of the inclusion stack
    // (0 means the file itself, 1 means directly included, etc.)
    int depth = static_cast<int>(include_len);
    if (depth < 1) depth = 1;

    result->include_deps.push_back(IncludeDep{path, depth});
}

}  // anonymous namespace


// ==================================================================
// Public entry point
// ==================================================================

ExtractionResult run_extraction(
    const std::string& file_path,
    const std::string& action,
    const std::vector<std::string>& clang_args
) {
    ExtractionResult result;
    result.file = normalise_path(file_path);

    ActionFilter filter = parse_action(action);

    // Build the command-line arguments for libclang
    std::vector<const char*> c_args;
    // Always add -fsyntax-only (no codegen)
    c_args.push_back("-fsyntax-only");
    // Add -x c++ to ensure C++ parsing for header files
    c_args.push_back("-x");
    c_args.push_back("c++");

    for (const auto& arg : clang_args) {
        c_args.push_back(arg.c_str());
    }

    // Create index (excludeDeclarationsFromPCH=0, displayDiagnostics=0)
    CXIndex index = clang_createIndex(0, 0);
    if (!index) {
        result.success = false;
        result.diagnostics.push_back("Failed to create CXIndex");
        return result;
    }

    // Parse the translation unit
    unsigned parse_options =
        CXTranslationUnit_DetailedPreprocessingRecord |
        CXTranslationUnit_SkipFunctionBodies * 0 |  // We WANT function bodies for refs
        CXTranslationUnit_KeepGoing;                 // Continue on errors

    CXTranslationUnit tu = nullptr;
    CXErrorCode err = clang_parseTranslationUnit2(
        index,
        file_path.c_str(),
        c_args.data(),
        static_cast<int>(c_args.size()),
        nullptr,  // no unsaved files
        0,
        parse_options,
        &tu
    );

    if (err != CXError_Success || !tu) {
        result.success = false;
        std::string err_msg = "Failed to parse translation unit (error code: " +
                              std::to_string(static_cast<int>(err)) + ")";
        result.diagnostics.push_back(err_msg);
        if (index) clang_disposeIndex(index);
        return result;
    }

    // Collect diagnostics
    unsigned num_diags = clang_getNumDiagnostics(tu);
    bool has_errors = false;
    for (unsigned i = 0; i < num_diags; ++i) {
        CXDiagnostic diag = clang_getDiagnostic(tu, i);
        CXDiagnosticSeverity severity = clang_getDiagnosticSeverity(diag);

        if (severity >= CXDiagnostic_Error) {
            has_errors = true;
            ClangString diag_text(
                clang_formatDiagnostic(diag, clang_defaultDiagnosticDisplayOptions())
            );
            result.diagnostics.push_back(diag_text.str());
        }

        clang_disposeDiagnostic(diag);
    }

    // Collect include deps (always, regardless of action filter)
    clang_getInclusions(tu, inclusion_visitor, &result);

    // Walk the AST
    CXCursor root = clang_getTranslationUnitCursor(tu);

    VisitorContext ctx;
    ctx.result = &result;
    ctx.filter = filter;
    ctx.tu = tu;
    ctx.main_file = normalise_path(file_path);

    clang_visitChildren(root, visit_cursor, &ctx);

    // --- Post-processing: deduplicate all result vectors ---

    // Deduplicate include deps (same header may appear multiple times)
    {
        auto& deps = result.include_deps;
        std::sort(deps.begin(), deps.end(), [](const IncludeDep& a, const IncludeDep& b) {
            if (a.path != b.path) return a.path < b.path;
            return a.depth < b.depth;
        });
        auto it = std::unique(deps.begin(), deps.end(), [](const IncludeDep& a, const IncludeDep& b) {
            return a.path == b.path;
        });
        deps.erase(it, deps.end());
    }

    // Deduplicate references.
    // 1. Sort so that "call" refs come before "read" refs at the same location
    //    (because "call" < "read" lexicographically).
    // 2. If a "call" and "read" exist at the same (symbol, line, col), keep
    //    only the "call" — the "read" is a spurious DeclRefExpr within a CallExpr.
    {
        auto& refs = result.references;
        std::sort(refs.begin(), refs.end(), [](const ReferenceInfo& a, const ReferenceInfo& b) {
            if (a.symbol != b.symbol) return a.symbol < b.symbol;
            if (a.line != b.line) return a.line < b.line;
            if (a.col != b.col) return a.col < b.col;
            return a.kind < b.kind;  // "call" < "read" < "type_ref"
        });
        // Remove exact duplicates first
        auto it = std::unique(refs.begin(), refs.end(), [](const ReferenceInfo& a, const ReferenceInfo& b) {
            return a.symbol == b.symbol && a.line == b.line && a.col == b.col && a.kind == b.kind;
        });
        refs.erase(it, refs.end());

        // Then remove "read" refs that are shadowed by a "call" at the same location
        std::vector<ReferenceInfo> filtered;
        filtered.reserve(refs.size());
        for (size_t i = 0; i < refs.size(); ++i) {
            if (refs[i].kind == "read") {
                // Check if a "call" exists at the same (symbol, line, col)
                bool shadowed = false;
                for (size_t j = 0; j < refs.size(); ++j) {
                    if (j != i && refs[j].kind == "call" &&
                        refs[j].symbol == refs[i].symbol &&
                        refs[j].line == refs[i].line &&
                        refs[j].col == refs[i].col) {
                        shadowed = true;
                        break;
                    }
                }
                if (shadowed) continue;
            }
            filtered.push_back(refs[i]);
        }
        refs = std::move(filtered);
    }

    // Deduplicate call edges (caller + callee + line)
    {
        auto& edges = result.call_edges;
        std::sort(edges.begin(), edges.end(), [](const CallEdge& a, const CallEdge& b) {
            if (a.caller != b.caller) return a.caller < b.caller;
            if (a.callee != b.callee) return a.callee < b.callee;
            return a.line < b.line;
        });
        auto it = std::unique(edges.begin(), edges.end(), [](const CallEdge& a, const CallEdge& b) {
            return a.caller == b.caller && a.callee == b.callee && a.line == b.line;
        });
        edges.erase(it, edges.end());
    }

    // Success if we could parse at all (even with errors, we have partial facts)
    result.success = (err == CXError_Success);

    // Cleanup
    clang_disposeTranslationUnit(tu);
    clang_disposeIndex(index);

    return result;
}

}  // namespace cxxtract
