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
#include <limits>
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

bool cursor_equals(CXCursor lhs, CXCursor rhs) {
    return clang_equalCursors(lhs, rhs) != 0;
}


/// RAII wrapper for token buffers.
class ClangTokens {
public:
    ClangTokens(CXTranslationUnit tu, CXToken* tokens, unsigned count)
        : tu_(tu), tokens_(tokens), count_(count) {}

    ~ClangTokens() {
        if (tokens_ != nullptr) {
            clang_disposeTokens(tu_, tokens_, count_);
        }
    }

    ClangTokens(const ClangTokens&) = delete;
    ClangTokens& operator=(const ClangTokens&) = delete;

    CXToken* data() const { return tokens_; }
    unsigned size() const { return count_; }

private:
    CXTranslationUnit tu_ = nullptr;
    CXToken* tokens_ = nullptr;
    unsigned count_ = 0;
};

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


/// Check if a cursor kind represents a callable declaration target.
bool is_callable_decl_kind(CXCursorKind kind) {
    switch (kind) {
        case CXCursor_FunctionDecl:
        case CXCursor_CXXMethod:
        case CXCursor_Constructor:
        case CXCursor_Destructor:
        case CXCursor_FunctionTemplate:
        case CXCursor_ConversionFunction:
            return true;
        default:
            return false;
    }
}


bool is_assignment_like_token(const std::string& token) {
    return token == "=" || token == "+=" || token == "-=" || token == "*=" ||
           token == "/=" || token == "%=" || token == "<<=" || token == ">>=" ||
           token == "&=" || token == "^=" || token == "|=";
}


bool should_drop_compile_arg(const std::string& arg) {
    return arg == "/nologo" || arg == "/Zi" || arg == "/Z7" || arg == "/FS" ||
           arg == "/RTC1" || arg == "/RTCc" || arg == "/RTCs" || arg == "/RTCu" ||
           arg == "/Od" || arg == "/Ob0" || arg == "/EHsc" || arg == "/utf-8" ||
           arg == "/permissive-" || arg == "/Zc:twoPhase-" || arg == "-MD" ||
           arg == "-MDd" || arg == "-MT" || arg == "-MTd" || arg == "/c" ||
           arg == "-c" || arg.rfind("/Fo", 0) == 0 || arg.rfind("/Fd", 0) == 0;
}


bool is_call_target_expr_kind(CXCursorKind kind) {
    switch (kind) {
        case CXCursor_MemberRefExpr:
        case CXCursor_MemberRef:
        case CXCursor_DeclRefExpr:
        case CXCursor_OverloadedDeclRef:
        case CXCursor_UnexposedExpr:
        case CXCursor_CallExpr:
        case CXCursor_TypeRef:
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


std::vector<std::string> get_cursor_tokens(CXTranslationUnit tu, CXCursor cursor) {
    std::vector<std::string> tokens_out;
    if (tu == nullptr) {
        return tokens_out;
    }

    CXSourceRange range = clang_getCursorExtent(cursor);
    if (clang_Range_isNull(range)) {
        return tokens_out;
    }

    CXToken* tokens = nullptr;
    unsigned token_count = 0;
    clang_tokenize(tu, range, &tokens, &token_count);
    ClangTokens holder(tu, tokens, token_count);

    tokens_out.reserve(token_count);
    for (unsigned i = 0; i < token_count; ++i) {
        ClangString spelling(clang_getTokenSpelling(tu, holder.data()[i]));
        tokens_out.push_back(spelling.str());
    }
    return tokens_out;
}


/// Collect direct AST children for manual recursive traversal and subtree queries.
std::vector<CXCursor> get_children(CXCursor cursor) {
    struct ChildCollector {
        std::vector<CXCursor>* children = nullptr;
    } collector;
    std::vector<CXCursor> children;
    collector.children = &children;

    clang_visitChildren(
        cursor,
        [](CXCursor child, CXCursor, CXClientData data) {
            auto* collector_ctx = static_cast<ChildCollector*>(data);
            collector_ctx->children->push_back(child);
            return CXChildVisit_Continue;
        },
        &collector
    );
    return children;
}


bool cursor_is_descendant_of(CXCursor cursor, CXCursor ancestor) {
    if (clang_Cursor_isNull(cursor) || clang_Cursor_isNull(ancestor)) {
        return false;
    }
    if (cursor_equals(cursor, ancestor)) {
        return true;
    }
    for (const auto& child : get_children(ancestor)) {
        if (cursor_is_descendant_of(cursor, child)) {
            return true;
        }
    }
    return false;
}


bool cursor_is_in_first_child_subtree(CXCursor parent, CXCursor cursor) {
    const auto children = get_children(parent);
    if (children.empty()) {
        return false;
    }
    return cursor_is_descendant_of(cursor, children.front());
}


CXCursor canonical_callable_cursor(CXCursor cursor) {
    if (clang_Cursor_isNull(cursor)) {
        return clang_getNullCursor();
    }

    const auto pick_if_callable = [](CXCursor candidate) -> CXCursor {
        if (!clang_Cursor_isNull(candidate) &&
            is_callable_decl_kind(clang_getCursorKind(candidate))) {
            return candidate;
        }
        return clang_getNullCursor();
    };

    CXCursor original = pick_if_callable(cursor);
    CXCursor canonical = pick_if_callable(clang_getCanonicalCursor(cursor));
    CXCursor definition = pick_if_callable(clang_getCursorDefinition(cursor));

    if (!clang_Cursor_isNull(definition)) {
        return definition;
    }
    if (!clang_Cursor_isNull(canonical)) {
        CXCursor canonical_definition = pick_if_callable(clang_getCursorDefinition(canonical));
        if (!clang_Cursor_isNull(canonical_definition)) {
            return canonical_definition;
        }
        return canonical;
    }
    return original;
}


CXCursor resolve_reference_target(
    CXCursor expr_cursor,
    CXCursor /*parent*/,
    CXTranslationUnit /*tu*/,
    bool in_call_context
) {
    CXCursor referenced = clang_getCursorReferenced(expr_cursor);
    if (clang_Cursor_isNull(referenced)) {
        return clang_getNullCursor();
    }

    CXCursor callable = canonical_callable_cursor(referenced);
    if (!clang_Cursor_isNull(callable)) {
        return callable;
    }

    if (in_call_context) {
        return clang_getNullCursor();
    }
    return referenced;
}


int call_target_cursor_score(CXCursor node, int depth) {
    int score = 0;
    switch (clang_getCursorKind(node)) {
        case CXCursor_MemberRefExpr:
        case CXCursor_MemberRef:
            score = 500;
            break;
        case CXCursor_DeclRefExpr:
            score = 400;
            break;
        case CXCursor_OverloadedDeclRef:
            score = 350;
            break;
        case CXCursor_CallExpr:
            score = 300;
            break;
        case CXCursor_TypeRef:
            score = 250;
            break;
        case CXCursor_UnexposedExpr:
            score = 200;
            break;
        default:
            score = 100;
            break;
    }
    return score - (depth * 10);
}


struct CallTargetCandidate {
    CXCursor target = clang_getNullCursor();
    int score = std::numeric_limits<int>::min();
};


void collect_callable_candidates(
    CXCursor cursor,
    int depth,
    CallTargetCandidate* best_candidate
) {
    if (clang_Cursor_isNull(cursor) || best_candidate == nullptr) {
        return;
    }

    CXCursor referenced = clang_getCursorReferenced(cursor);
    CXCursor callable = canonical_callable_cursor(referenced);
    if (clang_Cursor_isNull(callable) &&
        is_callable_decl_kind(clang_getCursorKind(cursor))) {
        callable = canonical_callable_cursor(cursor);
    }

    if (!clang_Cursor_isNull(callable)) {
        int score = call_target_cursor_score(cursor, depth);
        if (score > best_candidate->score) {
            best_candidate->target = callable;
            best_candidate->score = score;
        }
    }

    for (const auto& child : get_children(cursor)) {
        collect_callable_candidates(child, depth + 1, best_candidate);
    }
}


CXCursor get_call_callee_root(CXCursor call_cursor) {
    const auto children = get_children(call_cursor);
    if (children.empty()) {
        return clang_getNullCursor();
    }

    for (const auto& child : children) {
        CXCursorKind kind = clang_getCursorKind(child);
        if (is_call_target_expr_kind(kind)) {
            return child;
        }
    }
    return children.front();
}


CXCursor resolve_call_target(CXCursor call_cursor, CXTranslationUnit /*tu*/) {
    CXCursor direct = canonical_callable_cursor(clang_getCursorReferenced(call_cursor));
    if (!clang_Cursor_isNull(direct)) {
        return direct;
    }

    CXCursor callee_root = get_call_callee_root(call_cursor);
    if (clang_Cursor_isNull(callee_root)) {
        return clang_getNullCursor();
    }

    CallTargetCandidate best_candidate;
    collect_callable_candidates(callee_root, 0, &best_candidate);
    return best_candidate.target;
}


std::vector<std::string> sanitise_clang_args(const std::vector<std::string>& clang_args) {
    std::vector<std::string> out;
    out.reserve(clang_args.size() + 8);

    for (const auto& raw_arg : clang_args) {
        if (raw_arg.empty() || should_drop_compile_arg(raw_arg)) {
            continue;
        }

        if (raw_arg.rfind("/D", 0) == 0 && raw_arg.size() > 2) {
            out.push_back("-D" + raw_arg.substr(2));
            continue;
        }

        if (raw_arg.rfind("/I", 0) == 0 && raw_arg.size() > 2) {
            out.push_back("-I" + raw_arg.substr(2));
            continue;
        }

        if (raw_arg.rfind("/FI", 0) == 0 && raw_arg.size() > 3) {
            out.push_back("-include");
            out.push_back(raw_arg.substr(3));
            continue;
        }

        if (raw_arg.rfind("/std:", 0) == 0 && raw_arg.size() > 5) {
            out.push_back("-std=" + raw_arg.substr(5));
            continue;
        }

        if (raw_arg == "-TP" || raw_arg == "/TP") {
            continue;
        }

        out.push_back(raw_arg);
    }

    return out;
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
    std::vector<CXCursor> ancestors;

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

bool cursor_is_under_call_callee_path(CXCursor cursor, const VisitorContext& ctx) {
    for (auto it = ctx.ancestors.rbegin(); it != ctx.ancestors.rend(); ++it) {
        if (clang_getCursorKind(*it) != CXCursor_CallExpr) {
            continue;
        }
        CXCursor callee_root = get_call_callee_root(*it);
        if (clang_Cursor_isNull(callee_root)) {
            return false;
        }
        return cursor_is_descendant_of(cursor, callee_root);
    }
    return false;
}


bool is_nonlocal_reference_target(CXCursor referenced) {
    if (clang_Cursor_isNull(referenced) || is_in_system_header(referenced)) {
        return false;
    }

    CXCursorKind ref_decl_kind = clang_getCursorKind(referenced);
    if (ref_decl_kind == CXCursor_ParmDecl) {
        return false;
    }
    if (ref_decl_kind == CXCursor_VarDecl) {
        CXCursor ref_parent = clang_getCursorSemanticParent(referenced);
        if (is_function_like(clang_getCursorKind(ref_parent))) {
            return false;
        }
    }
    return true;
}


std::string classify_ref_kind(CXCursor cursor, const VisitorContext& ctx) {
    for (auto it = ctx.ancestors.rbegin(); it != ctx.ancestors.rend(); ++it) {
        CXCursor ancestor = *it;
        CXCursorKind kind = clang_getCursorKind(ancestor);

        if (kind == CXCursor_UnexposedExpr || kind == CXCursor_ParenExpr ||
            kind == CXCursor_MemberRefExpr || kind == CXCursor_MemberRef ||
            kind == CXCursor_DeclRefExpr) {
            continue;
        }

        if (kind == CXCursor_CompoundAssignOperator) {
            if (cursor_is_in_first_child_subtree(ancestor, cursor)) {
                return "write";
            }
            break;
        }

        if (kind == CXCursor_BinaryOperator) {
            if (cursor_is_in_first_child_subtree(ancestor, cursor)) {
                for (const auto& token : get_cursor_tokens(ctx.tu, ancestor)) {
                    if (is_assignment_like_token(token)) {
                        return "write";
                    }
                }
            }
            break;
        }

        if (kind == CXCursor_UnaryOperator) {
            if (cursor_is_in_first_child_subtree(ancestor, cursor)) {
                const auto tokens = get_cursor_tokens(ctx.tu, ancestor);
                for (const auto& token : tokens) {
                    if (token == "++" || token == "--") {
                        return "write";
                    }
                }
                for (const auto& token : tokens) {
                    if (token == "&") {
                        return "addr";
                    }
                }
            }
            break;
        }

        if (kind == CXCursor_CallExpr) {
            break;
        }
    }

    return "read";
}


CXCursor find_enclosing_function_in_ancestors(const VisitorContext& ctx) {
    for (auto it = ctx.ancestors.rbegin(); it != ctx.ancestors.rend(); ++it) {
        if (is_function_like(clang_getCursorKind(*it))) {
            return *it;
        }
    }
    return clang_getNullCursor();
}


void emit_call_from_cursor(CXCursor cursor, VisitorContext* ctx) {
    CXCursor referenced = resolve_call_target(cursor, ctx->tu);
    if (clang_Cursor_isNull(referenced) || is_in_system_header(referenced)) {
        return;
    }

    auto loc_info = get_cursor_loc(cursor);
    if (!loc_info.valid) {
        return;
    }

    std::string callee_name = build_qualified_name(referenced);
    ctx->result->references.push_back(ReferenceInfo{
        callee_name,
        static_cast<int>(loc_info.line),
        static_cast<int>(loc_info.col),
        "call"
    });

    CXCursor caller = find_enclosing_function_in_ancestors(*ctx);
    if (clang_Cursor_isNull(caller)) {
        caller = find_enclosing_function(cursor, ctx->tu);
    }
    if (!clang_Cursor_isNull(caller)) {
        std::string caller_name = build_qualified_name(caller);
        ctx->result->call_edges.push_back(CallEdge{
            caller_name,
            callee_name,
            static_cast<int>(loc_info.line)
        });
    }
}


void visit_cursor_recursive(CXCursor cursor, CXCursor parent, VisitorContext* ctx) {
    CXCursorKind kind = clang_getCursorKind(cursor);

    if (is_in_system_header(cursor)) {
        return;
    }

    if (ctx->should_collect_symbols() && is_symbol_kind(kind)) {
        if (kind == CXCursor_ClassDecl || kind == CXCursor_StructDecl ||
            kind == CXCursor_UnionDecl || kind == CXCursor_EnumDecl) {
            if (!clang_isCursorDefinition(cursor)) {
                goto recurse_children;
            }
        }

        if (kind == CXCursor_VarDecl) {
            CXCursor sem_parent = clang_getCursorSemanticParent(cursor);
            if (is_function_like(clang_getCursorKind(sem_parent))) {
                goto recurse_children;
            }
        }

        if (kind == CXCursor_Namespace) {
            ClangString ns_name(clang_getCursorSpelling(cursor));
            if (std::strlen(ns_name.c_str()) == 0) {
                goto recurse_children;
            }
        }

        auto loc_info = get_cursor_loc(cursor);
        if (loc_info.valid) {
            ClangString name(clang_getCursorSpelling(cursor));
            std::string qname = build_qualified_name(cursor);
            std::string kind_str = cursor_kind_to_symbol_kind(kind, cursor);
            unsigned end_line = get_extent_end_line(cursor);

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

    if (ctx->should_collect_refs()) {
        if (kind == CXCursor_CallExpr) {
            emit_call_from_cursor(cursor, ctx);
        }

        if (kind == CXCursor_DeclRefExpr || kind == CXCursor_MemberRefExpr ||
            kind == CXCursor_MemberRef) {
            bool in_call_context = cursor_is_under_call_callee_path(cursor, *ctx);
            CXCursor referenced = resolve_reference_target(cursor, parent, ctx->tu, in_call_context);
            if (!clang_Cursor_isNull(referenced) && is_nonlocal_reference_target(referenced)) {
                if (!(in_call_context &&
                      !clang_Cursor_isNull(canonical_callable_cursor(referenced)))) {
                    auto loc_info = get_cursor_loc(cursor);
                    if (loc_info.valid) {
                        ctx->result->references.push_back(ReferenceInfo{
                            build_qualified_name(referenced),
                            static_cast<int>(loc_info.line),
                            static_cast<int>(loc_info.col),
                            classify_ref_kind(cursor, *ctx)
                        });
                    }
                }
            }
        }

        if (kind == CXCursor_TypeRef) {
            CXCursor referenced = clang_getCursorReferenced(cursor);
            if (!clang_Cursor_isNull(referenced) && !is_in_system_header(referenced)) {
                auto loc_info = get_cursor_loc(cursor);
                if (loc_info.valid) {
                    ctx->result->references.push_back(ReferenceInfo{
                        build_qualified_name(referenced),
                        static_cast<int>(loc_info.line),
                        static_cast<int>(loc_info.col),
                        "type_ref"
                    });
                }
            }
        }
    }

recurse_children:
    ctx->ancestors.push_back(cursor);
    for (const auto& child : get_children(cursor)) {
        visit_cursor_recursive(child, cursor, ctx);
    }
    ctx->ancestors.pop_back();
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

    const auto normalised_args = sanitise_clang_args(clang_args);
    for (const auto& arg : normalised_args) {
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

    visit_cursor_recursive(root, clang_getNullCursor(), &ctx);

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
