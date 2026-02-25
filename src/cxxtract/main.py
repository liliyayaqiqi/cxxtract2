"""FastAPI application factory with async lifespan management."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

import uvicorn
from fastapi import FastAPI

from cxxtract import __version__
from cxxtract.api.routes import router
from cxxtract.cache.db import close_db, init_db
from cxxtract.config import Settings, load_settings
from cxxtract.orchestrator.engine import OrchestratorEngine
from cxxtract.orchestrator.rg_env import ensure_rg

logger = logging.getLogger("cxxtract")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown of the application.

    On startup:
      - Initialise the SQLite database.
      - Create the OrchestratorEngine.

    On shutdown:
      - Close the database connection.
    """
    settings: Settings = app.state.settings
    logger.info("CXXtract2 v%s starting up", __version__)

    # Ensure ripgrep is available (auto-detect / auto-install)
    rg_path, rg_version_info = ensure_rg(settings)
    if rg_path:
        app.state.rg_version = rg_version_info.raw if rg_version_info else ""
        logger.info("ripgrep ready: %s", app.state.rg_version or rg_path)
    else:
        app.state.rg_version = ""
        logger.warning(
            "ripgrep not available — recall-based queries will not work, "
            "but cached results can still be served"
        )

    # Initialise database
    await init_db(settings.db_path)

    # Create orchestrator
    engine = OrchestratorEngine(settings)
    app.state.engine = engine

    logger.info("Ready — listening on %s:%d", settings.host, settings.port)
    yield

    # Shutdown
    logger.info("Shutting down…")
    await close_db()


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build and return a configured FastAPI application.

    Parameters
    ----------
    settings:
        Optional pre-built settings.  If *None*, settings are loaded from
        environment variables and an optional ``config.yaml`` in the CWD.

    Returns
    -------
    FastAPI
        The fully-wired application, ready to be served by Uvicorn.
    """
    if settings is None:
        config_path = Path("config.yaml")
        settings = load_settings(config_path if config_path.exists() else None)

    app = FastAPI(
        title="CXXtract2",
        description=(
            "Lazy-evaluated C++ code semantic understanding service. "
            "Provides on-demand AST-level facts (definitions, references, "
            "call graphs) for AI-driven code review."
        ),
        version=__version__,
        lifespan=lifespan,
    )

    # Attach settings to app state so routes can access them
    app.state.settings = settings

    # Register routes
    app.include_router(router)

    return app


def main() -> None:
    """CLI entry point: load config and run the server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config_path = Path("config.yaml")
    settings = load_settings(config_path if config_path.exists() else None)
    app = create_app(settings)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
