"""Shared test fixtures for CXXtract2."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio

from cxxtract.config import Settings


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory for test artifacts."""
    return tmp_path


@pytest.fixture
def test_settings(tmp_dir: Path) -> Settings:
    """Provide test settings with a temporary database."""
    return Settings(
        db_path=str(tmp_dir / "test_cache.db"),
        rg_binary="rg",
        extractor_binary="cpp-extractor",
        max_parse_workers=2,
        max_recall_files=50,
        recall_timeout_s=10,
        parse_timeout_s=30,
    )


@pytest_asyncio.fixture
async def db_conn():
    """Provide a fresh in-memory SQLite database for each test.

    Initialises the schema via ``init_db(":memory:")``, yields the
    connection, and closes it afterwards.  Also resets the module-level
    ``_connection`` global so tests are fully isolated.
    """
    from cxxtract.cache.db import close_db, init_db

    conn = await init_db(":memory:")
    yield conn
    await close_db()
