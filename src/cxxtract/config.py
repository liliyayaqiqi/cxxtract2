"""Application settings loaded from environment variables or YAML config."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration for CXXtract2.

    Values are resolved in order: constructor kwargs > env vars > YAML file > defaults.
    Environment variables are prefixed with ``CXXTRACT_`` (e.g. ``CXXTRACT_DB_PATH``).
    """

    # -- External tool paths --------------------------------------------------
    rg_binary: str = "rg"
    extractor_binary: str = "./cpp-extractor/build/Release/cpp-extractor.exe"
    default_compile_commands: str = ""
    workspace_manifest_name: str = "workspace.yaml"

    # -- Cache ----------------------------------------------------------------
    db_path: str = "./cxxtract_cache.db"

    # -- Concurrency & limits -------------------------------------------------
    max_parse_workers: int = 4
    max_recall_files: int = 200
    recall_timeout_s: int = 30
    parse_timeout_s: int = 120
    writer_queue_size: int = 1024
    writer_batch_size: int = 10
    writer_retry_attempts: int = 3
    writer_retry_delay_ms: int = 200

    # -- Server ---------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000

    # -- Overlay controls -----------------------------------------------------
    max_overlay_files: int = 5000
    max_overlay_rows: int = 2_000_000
    context_ttl_hours: int = 72
    context_disk_budget_bytes: int = 4 * 1024 * 1024 * 1024

    model_config = {
        "env_prefix": "CXXTRACT_",
    }


def load_settings(config_path: Optional[str | Path] = None) -> Settings:
    """Load settings, optionally merging values from a YAML file.

    Parameters
    ----------
    config_path:
        Path to a YAML config file.  If *None*, only env vars and defaults
        are used.

    Returns
    -------
    Settings
        A fully-resolved settings instance.
    """
    overrides: dict = {}
    if config_path is not None:
        p = Path(config_path)
        if p.exists():
            with p.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
            if isinstance(raw, dict):
                # Only keep keys that are valid settings fields
                valid_keys = set(Settings.model_fields.keys())
                overrides = {k: v for k, v in raw.items() if k in valid_keys and v is not None}
    return Settings(**overrides)
