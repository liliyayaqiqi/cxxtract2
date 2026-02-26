"""sqlite-vec environment manager — auto-install under project bin/ on Windows."""

from __future__ import annotations

import io
import json
import logging
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_GITHUB_RELEASES_URL = "https://api.github.com/repos/asg017/sqlite-vec/releases/latest"
_ASSET_HINTS = ("windows", "win", "x64", "x86_64", "sqlite-vec", "sqlite_vec", "vec")
_DLL_HINTS = ("sqlite_vec.dll", "sqlite-vec.dll", "vec0.dll", "vec.dll")
_ASSET_EXCLUDE_HINTS = ("amalgamation", "source", "src", "headers", "static", "cli", "cosmopolitan")


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
        if not isinstance(assets, list) or not assets:
            logger.error("No release assets found for sqlite-vec")
            return None

        # 1) Best case: direct DLL asset.
        for asset in assets:
            asset_name = str(asset.get("name", ""))
            lname = asset_name.lower()
            if not lname.endswith(".dll"):
                continue
            if any(ex in lname for ex in _ASSET_EXCLUDE_HINTS):
                continue
            if "vec" not in lname:
                continue
            if not ("win" in lname or "windows" in lname):
                continue
            download_url = str(asset.get("browser_download_url", ""))
            if not download_url:
                continue
            logger.info("Downloading sqlite-vec DLL asset %s", asset_name)
            req = Request(download_url, headers={"User-Agent": "cxxtract2"})
            with urlopen(req, timeout=120) as resp:
                dll_bytes = resp.read()
            with open(target, "wb") as dst:
                dst.write(dll_bytes)
            logger.info("Installed sqlite-vec DLL to %s", target)
            return str(target.resolve())

        # 2) Otherwise: evaluate archive assets and pick likely loadable-extension package.
        archive_candidates: list[tuple[int, str, str]] = []
        for asset in assets:
            asset_name = str(asset.get("name", ""))
            lname = asset_name.lower()
            is_zip = lname.endswith(".zip")
            is_targz = lname.endswith(".tar.gz")
            if not (is_zip or is_targz):
                continue
            if "vec" not in lname:
                continue
            if any(ex in lname for ex in _ASSET_EXCLUDE_HINTS):
                continue
            url = str(asset.get("browser_download_url", ""))
            if not url:
                continue

            score = 0
            if "windows" in lname or "win" in lname:
                score += 100
            if "x64" in lname or "x86_64" in lname:
                score += 40
            if "loadable" in lname or "extension" in lname:
                score += 30
            if "debug" in lname:
                score -= 20
            if is_targz:
                score += 10
            archive_candidates.append((score, asset_name, url))

        if not archive_candidates:
            logger.error("No suitable sqlite-vec Windows binary asset found in latest release")
            return None

        archive_candidates.sort(key=lambda x: x[0], reverse=True)

        for _score, asset_name, download_url in archive_candidates:
            logger.info("Downloading sqlite-vec asset %s", asset_name)
            req = Request(download_url, headers={"User-Agent": "cxxtract2"})
            with urlopen(req, timeout=120) as resp:
                archive_bytes = resp.read()

            lname_asset = asset_name.lower()
            if lname_asset.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
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
                        logger.warning("Asset %s does not contain a loadable sqlite-vec DLL", asset_name)
                        continue

                    with zf.open(preferred) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    logger.info("Installed sqlite-vec DLL to %s", target)
                    return str(target.resolve())

            elif lname_asset.endswith(".tar.gz"):
                with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tf:
                    members = tf.getmembers()
                    preferred_member = None
                    for m in members:
                        lname = Path(m.name).name.lower()
                        if lname in _DLL_HINTS:
                            preferred_member = m
                            break
                    if preferred_member is None:
                        for m in members:
                            lname = Path(m.name).name.lower()
                            if lname.endswith(".dll") and "vec" in lname and ("sqlite" in lname or "vec0" in lname):
                                preferred_member = m
                                break

                    if preferred_member is None:
                        logger.warning("Asset %s does not contain a loadable sqlite-vec DLL", asset_name)
                        continue

                    src = tf.extractfile(preferred_member)
                    if src is None:
                        logger.warning("Failed to extract DLL member from asset %s", asset_name)
                        continue
                    with src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    logger.info("Installed sqlite-vec DLL to %s", target)
                    return str(target.resolve())

        logger.error("No downloaded sqlite-vec asset contained a loadable DLL")
        return None

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
