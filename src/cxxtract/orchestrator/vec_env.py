"""sqlite-vec environment manager — auto-install under project bin/ on Windows."""

from __future__ import annotations

import io
import json
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_GITHUB_RELEASES_URL = "https://api.github.com/repos/asg017/sqlite-vec/releases/latest"
_ASSET_HINTS = ("windows", "win", "x64", "x86_64", "sqlite-vec", "sqlite_vec", "vec")
_DLL_HINTS = ("sqlite_vec.dll", "sqlite-vec.dll", "vec0.dll", "vec.dll")


def default_sqlite_vec_path() -> Path:
    """Return fixed sqlite-vec DLL location: <project_root>/bin/sqlite_vec.dll."""
    return Path(__file__).resolve().parents[3] / "bin" / "sqlite_vec.dll"


def find_sqlite_vec() -> Optional[str]:
    """Locate sqlite-vec only in project-local bin folder."""
    target = default_sqlite_vec_path()
    if target.is_file():
        return str(target.resolve())
    return None


def install_sqlite_vec() -> Optional[str]:
    """Download and install sqlite-vec DLL to fixed bin path."""
    target = default_sqlite_vec_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Attempting to download sqlite-vec from GitHub Releases...")
    try:
        req = Request(
            _GITHUB_RELEASES_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "cxxtract2"},
        )
        with urlopen(req, timeout=30) as resp:
            release_data = json.loads(resp.read().decode("utf-8"))

        assets = release_data.get("assets", [])
        download_url = ""
        asset_name = ""
        for asset in assets:
            name = str(asset.get("name", "")).lower()
            if not name.endswith(".zip"):
                continue
            if any(h in name for h in _ASSET_HINTS) and ("windows" in name or "win" in name):
                download_url = str(asset.get("browser_download_url", ""))
                asset_name = str(asset.get("name", ""))
                break

        if not download_url:
            for asset in assets:
                name = str(asset.get("name", "")).lower()
                if name.endswith(".zip") and "vec" in name:
                    download_url = str(asset.get("browser_download_url", ""))
                    asset_name = str(asset.get("name", ""))
                    break

        if not download_url:
            logger.error("No suitable sqlite-vec Windows zip asset found in latest release")
            return None

        logger.info("Downloading sqlite-vec asset %s", asset_name)
        req = Request(download_url, headers={"User-Agent": "cxxtract2"})
        with urlopen(req, timeout=120) as resp:
            zip_bytes = resp.read()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            preferred = None
            for entry in names:
                lname = entry.split("/")[-1].lower()
                if lname in _DLL_HINTS:
                    preferred = entry
                    break
            if preferred is None:
                for entry in names:
                    lname = entry.split("/")[-1].lower()
                    if lname.endswith(".dll") and "vec" in lname and ("sqlite" in lname or "vec0" in lname):
                        preferred = entry
                        break

            if preferred is None:
                logger.error("No sqlite-vec DLL found inside downloaded zip")
                return None

            with zf.open(preferred) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

        logger.info("Installed sqlite-vec DLL to %s", target)
        return str(target.resolve())

    except (URLError, OSError, json.JSONDecodeError, zipfile.BadZipFile, KeyError) as exc:
        logger.error("Failed to download/install sqlite-vec: %s", exc)
        return None


def ensure_sqlite_vec(settings: object) -> Optional[str]:
    """Ensure sqlite-vec DLL exists in fixed bin location when vector is enabled."""
    enabled = bool(getattr(settings, "enable_vector_features", False))
    if not enabled:
        return None

    found = find_sqlite_vec()
    if found:
        logger.info("Using sqlite-vec DLL at %s", found)
        return found

    logger.info("sqlite-vec not found under bin/ — attempting auto-install...")
    installed = install_sqlite_vec()
    if installed:
        return installed

    logger.error("sqlite-vec DLL is unavailable under bin/. Auto-install failed.")
    return None
