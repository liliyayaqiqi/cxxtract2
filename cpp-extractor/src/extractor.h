/**
 * @file extractor.h
 * @brief AST extraction interface using the libclang C API.
 *
 * The Extractor uses libclang to parse a C++ translation unit and collects:
 *   - Symbol definitions (functions, classes, variables, etc.)
 *   - Symbol references (calls, reads, writes, address-of)
 *   - Call edges (caller -> callee pairs)
 *   - Include dependencies (direct and transitive)
 *
 * Results are serialised to JSON via nlohmann/json and written to stdout.
 */

#pragma once

#include <algorithm>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace cxxtract {

// ------------------------------------------------------------------
// Action filter — controls what the extractor collects
// ------------------------------------------------------------------

enum class ActionFilter {
    ExtractAll,      // symbols + references + call edges + include deps
    ExtractSymbols,  // symbols only
    ExtractRefs,     // references + call edges only
};

/// Parse the CLI action string into an ActionFilter enum.
inline ActionFilter parse_action(const std::string& action) {
    if (action == "extract-symbols") return ActionFilter::ExtractSymbols;
    if (action == "extract-refs")    return ActionFilter::ExtractRefs;
    return ActionFilter::ExtractAll;
}

// ------------------------------------------------------------------
// Data structures matching the Python ExtractorOutput model
// ------------------------------------------------------------------

struct SymbolInfo {
    std::string name;
    std::string qualified_name;
    std::string kind;            // matches Python SymbolKind enum values
    int line = 0;
    int col = 0;
    int extent_end_line = 0;
};

struct ReferenceInfo {
    std::string symbol;          // qualified name of referenced symbol
    int line = 0;
    int col = 0;
    std::string kind = "unknown"; // matches Python RefKind enum values
};

struct CallEdge {
    std::string caller;          // qualified name
    std::string callee;          // qualified name
    int line = 0;
};

struct IncludeDep {
    std::string path;
    int depth = 1;
};

struct ExtractionResult {
    std::string file;
    std::vector<SymbolInfo> symbols;
    std::vector<ReferenceInfo> references;
    std::vector<CallEdge> call_edges;
    std::vector<IncludeDep> include_deps;
    bool success = true;
    std::vector<std::string> diagnostics;
};

// ------------------------------------------------------------------
// Utility: normalise file paths (forward slashes, consistent casing)
// ------------------------------------------------------------------

/// Replace all backslashes with forward slashes for cross-platform consistency.
inline std::string normalise_path(std::string path) {
    std::replace(path.begin(), path.end(), '\\', '/');
    return path;
}

// ------------------------------------------------------------------
// JSON serialisation (nlohmann/json ADL)
// ------------------------------------------------------------------

void to_json(nlohmann::json& j, const SymbolInfo& s);
void to_json(nlohmann::json& j, const ReferenceInfo& r);
void to_json(nlohmann::json& j, const CallEdge& e);
void to_json(nlohmann::json& j, const IncludeDep& d);
void to_json(nlohmann::json& j, const ExtractionResult& r);

// ------------------------------------------------------------------
// Main extraction entry point
// ------------------------------------------------------------------

/**
 * Run the libclang AST extraction pipeline on a single source file.
 *
 * @param file_path   Absolute path to the source file.
 * @param action      One of: "extract-all", "extract-symbols", "extract-refs".
 * @param clang_args  Compiler flags forwarded to libclang.
 * @return ExtractionResult with all collected facts.
 */
ExtractionResult run_extraction(
    const std::string& file_path,
    const std::string& action,
    const std::vector<std::string>& clang_args
);

}  // namespace cxxtract
