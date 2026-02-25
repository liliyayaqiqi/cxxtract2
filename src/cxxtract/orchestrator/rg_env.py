"""Ripgrep environment manager — auto-detection, version check, and install.

This module ensures that a suitable ``rg.exe`` binary is available at
runtime.  It probes common installation locations, validates the version
(>= 13.0 for stable ``--json`` output), and can auto-download a release
from GitHub as a last resort.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# Minimum ripgrep version required for stable --json output.
_MIN_RG_MAJOR = 13
_MIN_RG_MINOR = 0

# GitHub API endpoint for latest ripgrep release.
_GITHUB_RELEASES_URL = "https://api.github.com/repos/BurntSushi/ripgrep/releases/latest"

# Asset name pattern for Windows x64 MSVC builds.
_ASSET_PATTERN = re.compile(
    r"ripgrep-[\d.]+-x86_64-pc-windows-msvc\.zip", re.IGNORECASE
)


# ====================================================================
# Version info
# ====================================================================

class RgVersionInfo:
    """Parsed ripgrep version information."""

    __slots__ = ("major", "minor", "patch", "raw")

    def __init__(self, major: int, minor: int, patch: int, raw: str) -> None:
        self.major = major
        self.minor = minor
        self.patch = patch
        self.raw = raw

    @property
    def semver(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def meets_minimum(self) -> bool:
        """Return True if this version is >= the minimum required."""
        if self.major > _MIN_RG_MAJOR:
            return True
        if self.major == _MIN_RG_MAJOR and self.minor >= _MIN_RG_MINOR:
            return True
        return False

    def __repr__(self) -> str:
        return f"RgVersionInfo({self.semver})"


# ====================================================================
# Public API
# ====================================================================

def find_rg(configured_path: str = "rg") -> Optional[str]:
    """Locate the ripgrep binary on the system.

    Search order:
      1. *configured_path* if it exists as a file or is on PATH.
      2. ``shutil.which("rg")``.
      3. Common Windows installation directories.

    Returns
    -------
    str | None
        Absolute path to rg.exe, or None if not found.
    """
    # 1. Configured path — could be absolute or just "rg"
    if configured_path and configured_path != "rg":
        p = Path(configured_path)
        if p.is_file():
            return str(p.resolve())
        # Maybe it's on PATH but with a custom name
        found = shutil.which(configured_path)
        if found:
            return str(Path(found).resolve())

    # 2. Default PATH lookup
    found = shutil.which("rg")
    if found:
        return str(Path(found).resolve())

    # 3. Probe common Windows locations
    candidates = _get_candidate_paths()
    for candidate in candidates:
        if candidate.is_file():
            logger.debug("Found rg at probed location: %s", candidate)
            return str(candidate.resolve())

    return None


def check_rg_version(rg_path: str) -> Optional[RgVersionInfo]:
    """Run ``rg --version`` and parse the output.

    Parameters
    ----------
    rg_path:
        Absolute path to the rg binary.

    Returns
    -------
    RgVersionInfo | None
        Parsed version, or None if the version could not be determined.
    """
    try:
        result = subprocess.run(
            [rg_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Failed to run rg --version: %s", exc)
        return None

    raw = result.stdout.strip()
    if not raw:
        return None

    # ripgrep outputs something like "ripgrep 14.1.0" or
    # "ripgrep 14.1.0 (rev abc1234)\n..."
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    if not match:
        logger.warning("Could not parse rg version from: %r", raw)
        return None

    return RgVersionInfo(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3)),
        raw=raw.splitlines()[0],
    )


def install_rg(target_dir: Optional[str | Path] = None) -> Optional[str]:
    """Download and install ripgrep from GitHub Releases.

    Downloads the latest Windows x64 MSVC zip, extracts ``rg.exe``
    into *target_dir*.

    Parameters
    ----------
    target_dir:
        Directory where rg.exe will be placed.  Defaults to a ``bin/``
        directory at the project root.

    Returns
    -------
    str | None
        Absolute path to the installed rg.exe, or None on failure.
    """
    if target_dir is None:
        # Default to <project_root>/bin/
        target_dir = Path(__file__).resolve().parents[3] / "bin"
    else:
        target_dir = Path(target_dir)

    target_dir.mkdir(parents=True, exist_ok=True)
    rg_exe = target_dir / "rg.exe"

    logger.info("Attempting to download ripgrep from GitHub Releases...")

    try:
        # Fetch release metadata
        req = Request(
            _GITHUB_RELEASES_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "cxxtract2"},
        )
        with urlopen(req, timeout=30) as resp:
            release_data = json.loads(resp.read().decode("utf-8"))

        # Find the Windows x64 MSVC asset
        assets = release_data.get("assets", [])
        download_url: Optional[str] = None
        asset_name: str = ""
        for asset in assets:
            name = asset.get("name", "")
            if _ASSET_PATTERN.match(name):
                download_url = asset.get("browser_download_url")
                asset_name = name
                break

        if not download_url:
            logger.error("No Windows x64 MSVC asset found in latest ripgrep release")
            return None

        tag = release_data.get("tag_name", "unknown")
        logger.info("Downloading %s (release %s)...", asset_name, tag)

        # Download the zip
        req = Request(download_url, headers={"User-Agent": "cxxtract2"})
        with urlopen(req, timeout=120) as resp:
            zip_bytes = resp.read()

        # Extract rg.exe from the zip
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            rg_entry: Optional[str] = None
            for name in zf.namelist():
                if name.lower().endswith("rg.exe"):
                    rg_entry = name
                    break

            if not rg_entry:
                logger.error("rg.exe not found inside the downloaded zip")
                return None

            # Extract the single file
            with zf.open(rg_entry) as src, open(rg_exe, "wb") as dst:
                dst.write(src.read())

        logger.info("Installed rg.exe to %s", rg_exe)
        return str(rg_exe.resolve())

    except (URLError, OSError, json.JSONDecodeError, zipfile.BadZipFile, KeyError) as exc:
        logger.error("Failed to download/install ripgrep: %s", exc)
        return None


def ensure_rg(settings: object) -> tuple[Optional[str], Optional[RgVersionInfo]]:
    """Ensure a valid ripgrep binary is available.  Called at startup.

    This function:
      1. Tries to find rg via ``find_rg(settings.rg_binary)``
      2. Validates the version via ``check_rg_version()``
      3. If not found, attempts ``install_rg()``
      4. Updates ``settings.rg_binary`` in-place with the resolved path

    Parameters
    ----------
    settings:
        An object with an ``rg_binary`` attribute (typically ``Settings``).

    Returns
    -------
    tuple[str | None, RgVersionInfo | None]
        The resolved rg path and version info.  Both are None if rg
        could not be found or installed.
    """
    configured = getattr(settings, "rg_binary", "rg")

    # Step 1: Try to find existing installation
    rg_path = find_rg(configured)

    if rg_path:
        version = check_rg_version(rg_path)
        if version and version.meets_minimum():
            logger.info("Using ripgrep %s at %s", version.semver, rg_path)
            # Update settings in-place
            if hasattr(settings, "rg_binary"):
                object.__setattr__(settings, "rg_binary", rg_path)
            return rg_path, version
        elif version:
            logger.warning(
                "ripgrep %s at %s is below minimum %d.%d — will try to install newer version",
                version.semver, rg_path, _MIN_RG_MAJOR, _MIN_RG_MINOR,
            )
        else:
            logger.warning("Could not determine ripgrep version at %s", rg_path)

    # Step 2: Attempt auto-install
    logger.info("ripgrep not found on system — attempting auto-install...")
    installed_path = install_rg()
    if installed_path:
        version = check_rg_version(installed_path)
        if version and version.meets_minimum():
            logger.info("Auto-installed ripgrep %s at %s", version.semver, installed_path)
            if hasattr(settings, "rg_binary"):
                object.__setattr__(settings, "rg_binary", installed_path)
            return installed_path, version
        elif version:
            logger.warning(
                "Auto-installed ripgrep %s is below minimum %d.%d",
                version.semver, _MIN_RG_MAJOR, _MIN_RG_MINOR,
            )
            # Still usable, just warn
            if hasattr(settings, "rg_binary"):
                object.__setattr__(settings, "rg_binary", installed_path)
            return installed_path, version

    # Step 3: All attempts failed
    logger.error(
        "ripgrep is not available. Install it manually:\n"
        "  winget install BurntSushi.ripgrep.MSVC\n"
        "  -or- choco install ripgrep\n"
        "  -or- scoop install ripgrep\n"
        "  -or- cargo install ripgrep"
    )
    return None, None


# ====================================================================
# Internal helpers
# ====================================================================

def _get_candidate_paths() -> list[Path]:
    """Return a list of common rg.exe locations to probe on Windows."""
    candidates: list[Path] = []

    # Project-local bin/
    project_bin = Path(__file__).resolve().parents[3] / "bin" / "rg.exe"
    candidates.append(project_bin)

    # Chocolatey
    choco = os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey")
    candidates.append(Path(choco) / "bin" / "rg.exe")

    # Scoop (user)
    scoop_root = os.environ.get("SCOOP", "")
    if scoop_root:
        candidates.append(Path(scoop_root) / "shims" / "rg.exe")
        candidates.append(Path(scoop_root) / "apps" / "ripgrep" / "current" / "rg.exe")
    else:
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            candidates.append(Path(userprofile) / "scoop" / "shims" / "rg.exe")

    # Cargo
    cargo_home = os.environ.get("CARGO_HOME", "")
    if cargo_home:
        candidates.append(Path(cargo_home) / "bin" / "rg.exe")
    else:
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            candidates.append(Path(userprofile) / ".cargo" / "bin" / "rg.exe")

    # winget / typical install locations
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates.append(Path(pf) / "ripgrep" / "rg.exe")

    localappdata = os.environ.get("LOCALAPPDATA", "")
    if localappdata:
        # WinGet Links (symlink directory)
        candidates.append(
            Path(localappdata) / "Microsoft" / "WinGet" / "Links" / "rg.exe"
        )
        # WinGet Packages — search installed package directories
        winget_pkgs = Path(localappdata) / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.is_dir():
            for pkg_dir in winget_pkgs.iterdir():
                if "ripgrep" in pkg_dir.name.lower():
                    # rg.exe may be in a subdirectory (e.g. ripgrep-15.1.0-x86_64-.../rg.exe)
                    for rg_candidate in pkg_dir.rglob("rg.exe"):
                        candidates.append(rg_candidate)

    return candidates
