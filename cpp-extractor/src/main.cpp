/**
 * @file main.cpp
 * @brief CLI entry point for cpp-extractor.
 *
 * Usage:
 *   cpp-extractor --action <action> --file <source_file> [-- <clang_flags...>]
 *
 * Actions:
 *   extract-all      Emit definitions, references, call edges, and include deps.
 *   extract-symbols  Emit only symbol definitions.
 *   extract-refs     Emit only references.
 *
 * Output is a single JSON object written to stdout.
 * Errors and diagnostics go to stderr.
 */

#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#include "extractor.h"

static void print_usage(const char* prog) {
    std::cerr
        << "Usage: " << prog
        << " --action <extract-all|extract-symbols|extract-refs>"
        << " --file <source_file>"
        << " [-- <clang_flags...>]\n";
}

int main(int argc, char* argv[]) {
    std::string action;
    std::string file_path;
    std::vector<std::string> clang_args;

    // --- Parse CLI arguments ---
    bool after_separator = false;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];

        if (arg == "--") {
            after_separator = true;
            continue;
        }

        if (after_separator) {
            clang_args.push_back(arg);
            continue;
        }

        if (arg == "--action" && i + 1 < argc) {
            action = argv[++i];
        } else if (arg == "--file" && i + 1 < argc) {
            file_path = argv[++i];
        } else if (arg == "--help" || arg == "-h") {
            print_usage(argv[0]);
            return 0;
        } else {
            std::cerr << "Unknown argument: " << arg << "\n";
            print_usage(argv[0]);
            return 1;
        }
    }

    // --- Validate ---
    if (action.empty()) {
        std::cerr << "Error: --action is required\n";
        print_usage(argv[0]);
        return 1;
    }
    if (file_path.empty()) {
        std::cerr << "Error: --file is required\n";
        print_usage(argv[0]);
        return 1;
    }
    if (action != "extract-all" && action != "extract-symbols" && action != "extract-refs") {
        std::cerr << "Error: unknown action '" << action << "'\n";
        print_usage(argv[0]);
        return 1;
    }

    // --- Run extraction ---
    auto result = cxxtract::run_extraction(file_path, action, clang_args);

    // --- Emit JSON to stdout ---
    nlohmann::json output;
    cxxtract::to_json(output, result);
    std::cout << output.dump(2) << std::endl;

    return result.success ? 0 : 1;
}
