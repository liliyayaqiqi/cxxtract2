"""Tests for application settings (config.py).

Covers:
  - Settings: default values, constructor kwargs
  - load_settings: YAML loading, unknown keys, None values, missing file
  - Environment variable overrides
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cxxtract.config import Settings, load_settings


# ====================================================================
# Settings defaults
# ====================================================================

class TestSettingsDefaults:

    def test_defaults(self):
        s = Settings()
        assert s.rg_binary == "rg"
        assert s.extractor_binary == "./bin/cpp-extractor.exe"
        assert s.default_compile_commands == ""
        assert s.db_path == "./cxxtract_cache.db"
        assert s.max_parse_workers == 4
        assert s.max_recall_files == 200
        assert s.recall_timeout_s == 30
        assert s.parse_timeout_s == 120
        assert s.host == "127.0.0.1"
        assert s.port == 8000

    def test_constructor_kwargs_override(self):
        s = Settings(
            db_path="/custom/cache.db",
            max_parse_workers=8,
            port=9000,
        )
        assert s.db_path == "/custom/cache.db"
        assert s.max_parse_workers == 8
        assert s.port == 9000

    def test_env_prefix(self):
        assert Settings.model_config.get("env_prefix") == "CXXTRACT_"


# ====================================================================
# load_settings with YAML
# ====================================================================

class TestLoadSettings:

    def test_load_from_yaml(self, tmp_path: Path):
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(
            "db_path: /my/cache.db\n"
            "max_parse_workers: 16\n"
            "port: 9999\n"
        )
        s = load_settings(yaml_file)
        assert s.db_path == "/my/cache.db"
        assert s.max_parse_workers == 16
        assert s.port == 9999
        # Other fields should have defaults
        assert s.rg_binary == "rg"

    def test_yaml_with_unknown_keys(self, tmp_path: Path):
        """Unknown keys in YAML are silently ignored."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(
            "db_path: /test.db\n"
            "unknown_key: value\n"
            "another_unknown: 42\n"
        )
        s = load_settings(yaml_file)
        assert s.db_path == "/test.db"
        # Should not raise and unknown keys should not appear

    def test_yaml_with_none_values(self, tmp_path: Path):
        """None values in YAML are skipped, defaults used instead."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(
            "db_path: ~\n"  # YAML null
            "max_parse_workers: 8\n"
        )
        s = load_settings(yaml_file)
        assert s.db_path == "./cxxtract_cache.db"  # default
        assert s.max_parse_workers == 8

    def test_yaml_missing_file(self, tmp_path: Path):
        """Non-existent YAML file → defaults used."""
        s = load_settings(tmp_path / "nonexistent.yaml")
        assert s.db_path == "./cxxtract_cache.db"

    def test_yaml_none_path(self):
        """None config_path → defaults used."""
        s = load_settings(None)
        assert s.db_path == "./cxxtract_cache.db"

    def test_yaml_empty_file(self, tmp_path: Path):
        """Empty YAML file → defaults used (yaml.safe_load returns None)."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("")
        s = load_settings(yaml_file)
        assert s.db_path == "./cxxtract_cache.db"

    def test_yaml_non_dict_content(self, tmp_path: Path):
        """YAML with non-dict content (e.g. a list) → defaults used."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("- item1\n- item2\n")
        s = load_settings(yaml_file)
        assert s.db_path == "./cxxtract_cache.db"


# ====================================================================
# Environment variable overrides
# ====================================================================

class TestEnvVarOverrides:

    def test_env_var_db_path(self):
        with patch.dict(os.environ, {"CXXTRACT_DB_PATH": "/env/cache.db"}):
            s = Settings()
            assert s.db_path == "/env/cache.db"

    def test_env_var_port(self):
        with patch.dict(os.environ, {"CXXTRACT_PORT": "7777"}):
            s = Settings()
            assert s.port == 7777

    def test_env_var_max_workers(self):
        with patch.dict(os.environ, {"CXXTRACT_MAX_PARSE_WORKERS": "16"}):
            s = Settings()
            assert s.max_parse_workers == 16

    def test_constructor_kwargs_beat_env(self):
        """Constructor kwargs should override env vars."""
        with patch.dict(os.environ, {"CXXTRACT_PORT": "7777"}):
            s = Settings(port=9999)
            assert s.port == 9999


class TestDotenvLoading:

    def test_load_settings_reads_dotenv_for_prefixed_values(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        dotenv = tmp_path / ".env"
        dotenv.write_text("CXXTRACT_PORT=8765\n")
        monkeypatch.delenv("CXXTRACT_PORT", raising=False)

        s = load_settings(None)
        assert s.port == 8765

    def test_load_settings_injects_token_env_for_runtime_lookup(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        dotenv = tmp_path / ".env"
        dotenv.write_text("CXXTRACT_GITLAB_TOKEN_REPOA=secret_value\n")
        monkeypatch.delenv("CXXTRACT_GITLAB_TOKEN_REPOA", raising=False)

        _ = load_settings(None)
        assert os.environ.get("CXXTRACT_GITLAB_TOKEN_REPOA") == "secret_value"

    def test_existing_env_wins_over_dotenv(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        dotenv = tmp_path / ".env"
        dotenv.write_text("CXXTRACT_PORT=7001\n")
        monkeypatch.setenv("CXXTRACT_PORT", "9001")

        s = load_settings(None)
        assert s.port == 9001
