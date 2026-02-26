CXXtract2 is a **Windows-first C++ semantic indexing/query service for AI code review agents**.  
Its core purpose is to answer questions like “where is this symbol defined/referenced/called?” with **AST-backed facts**, but only for relevant files, to keep latency and compute manageable.

**What it does**
- Exposes a FastAPI service with query endpoints for references, definitions, call graph, and file symbols in [routes.py](f:\personal_projects\cxxtract2\src\cxxtract\api\routes.py).
- Uses a 5-stage orchestration pipeline in [engine.py](f:\personal_projects\cxxtract2\src\cxxtract\orchestrator\engine.py):
1. recall candidate files with ripgrep,
2. compare cache freshness via hashes,
3. parse stale files with a C++ extractor,
4. merge cached + new facts,
5. return results with a confidence envelope.
- Persists semantic facts in SQLite (symbols, references, call edges, include deps, parse runs) defined in [migrations.sql](f:\personal_projects\cxxtract2\src\cxxtract\schema\migrations.sql) and managed by [repository.py](f:\personal_projects\cxxtract2\src\cxxtract\cache\repository.py).
- Computes cache invalidation using composite hash (content + includes + compile flags) in [hasher.py](f:\personal_projects\cxxtract2\src\cxxtract\cache\hasher.py).
- Parses C++ with a dedicated libclang-based executable in [extractor.cpp](f:\personal_projects\cxxtract2\cpp-extractor\src\extractor.cpp).

**Intended workflow**
- An AI reviewer sends symbol queries to this service.
- The service avoids full-repo parsing by using ripgrep recall first.
- It returns not just answers, but also **coverage/confidence metadata** (verified vs unparsed files), so downstream AI can reason about uncertainty.

**Project maturity and scope**
- Python side is heavily tested (cache, recall, parser, engine, API, config under `tests/`).
- Design notes in `.cursor/plans` confirm this is meant as a production-grade semantic backend.
- It is **not** full whole-program static analysis; results depend on:
1. recall quality,
2. available `compile_commands.json`,
3. successful parsing of recalled files.