"""Shared test fixtures for CXXtract2."""

import asyncio
import tempfile
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
