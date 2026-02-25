cmake_minimum_required(VERSION 3.20)
project(cpp-extractor LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# ==============================================================
# Find LLVM installation (for libclang C API)
# ==============================================================
# The standard LLVM Windows installer ships libclang.lib and the
# clang-c/ headers — everything we need for AST traversal.
#
# Set LLVM_ROOT or CMAKE_PREFIX_PATH to your LLVM installation.
#   e.g. cmake -DLLVM_ROOT="C:/Program Files/LLVM" ..

# Try common LLVM installation directories
set(LLVM_SEARCH_PATHS
    "C:/Program Files/LLVM"
    "$ENV{ProgramFiles}/LLVM"
    "$ENV{LLVM_ROOT}"
)

# Find the LLVM include directory (contains clang-c/)
find_path(LLVM_INCLUDE_DIR
    NAMES clang-c/Index.h
    PATHS ${LLVM_SEARCH_PATHS}
    PATH_SUFFIXES include
)

# Find libclang.lib
find_library(LIBCLANG_LIB
    NAMES libclang
    PATHS ${LLVM_SEARCH_PATHS}
    PATH_SUFFIXES lib
)

# Find libclang.dll for runtime
find_file(LIBCLANG_DLL
    NAMES libclang.dll
    PATHS ${LLVM_SEARCH_PATHS}
    PATH_SUFFIXES bin
)

if(NOT LLVM_INCLUDE_DIR)
    message(FATAL_ERROR
        "Could not find clang-c/Index.h. "
        "Set LLVM_ROOT to your LLVM installation directory."
    )
endif()

if(NOT LIBCLANG_LIB)
    message(FATAL_ERROR
        "Could not find libclang.lib. "
        "Set LLVM_ROOT to your LLVM installation directory."
    )
endif()

message(STATUS "Found LLVM include: ${LLVM_INCLUDE_DIR}")
message(STATUS "Found libclang lib:  ${LIBCLANG_LIB}")
if(LIBCLANG_DLL)
    message(STATUS "Found libclang dll:  ${LIBCLANG_DLL}")
endif()

# ==============================================================
# nlohmann/json (header-only, fetched via FetchContent)
# ==============================================================
include(FetchContent)
FetchContent_Declare(
    nlohmann_json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG        v3.11.3
)
FetchContent_MakeAvailable(nlohmann_json)

# ==============================================================
# cpp-extractor executable
# ==============================================================
add_executable(cpp-extractor
    src/main.cpp
    src/extractor.cpp
)

target_include_directories(cpp-extractor PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${LLVM_INCLUDE_DIR}
)

target_link_libraries(cpp-extractor
    PRIVATE
        ${LIBCLANG_LIB}
        nlohmann_json::nlohmann_json
)

# On Windows with MSVC
if(MSVC)
    target_compile_options(cpp-extractor PRIVATE /EHsc /W3 /utf-8)
    target_compile_definitions(cpp-extractor PRIVATE
        _SILENCE_ALL_CXX17_DEPRECATION_WARNINGS
    )
endif()

# Copy libclang.dll next to the executable for runtime
if(LIBCLANG_DLL)
    add_custom_command(TARGET cpp-extractor POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${LIBCLANG_DLL}"
            "$<TARGET_FILE_DIR:cpp-extractor>"
        COMMENT "Copying libclang.dll to output directory"
    )
endif()
