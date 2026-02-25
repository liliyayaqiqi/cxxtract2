"""Composite hashing for cache invalidation.

The composite hash for a translation unit is:

    SHA-256( content_hash || includes_hash || flags_hash )

where each component is itself a hex-encoded SHA-256 digest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def compute_content_hash(file_path: str | Path) -> str:
    """Return the SHA-256 hex digest of a file's raw bytes.

    Parameters
    ----------
    file_path:
        Absolute or relative path to the file.

    Returns
    -------
    str
        Hex-encoded SHA-256 digest, or empty string if the file cannot be read.
    """
    try:
        data = Path(file_path).read_bytes()
        return hashlib.sha256(data).hexdigest()
    except OSError:
        return ""


def compute_flags_hash(flags: list[str]) -> str:
    """Return the SHA-256 hex digest of a sorted list of compiler flags.

    Sorting ensures that flag reordering doesn't cause spurious invalidation.
    """
    normalized = "\0".join(sorted(flags))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_includes_hash(included_file_hashes: list[str]) -> str:
    """Return the SHA-256 hex digest over sorted hashes of included files.

    Parameters
    ----------
    included_file_hashes:
        A list of content hashes for each directly/transitively included header.
        Pass an empty list if include deps are not yet available.

    Returns
    -------
    str
        Hex-encoded SHA-256 digest.
    """
    combined = "\0".join(sorted(included_file_hashes))
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def compute_composite_hash(
    content_hash: str,
    includes_hash: str,
    flags_hash: str,
) -> str:
    """Combine the three component hashes into a single composite hash.

    Parameters
    ----------
    content_hash:
        SHA-256 of the source file content.
    includes_hash:
        SHA-256 over included-file content hashes.
    flags_hash:
        SHA-256 of the compiler flags.

    Returns
    -------
    str
        Hex-encoded SHA-256 digest of the composite.
    """
    composite_input = f"{content_hash}||{includes_hash}||{flags_hash}"
    return hashlib.sha256(composite_input.encode("utf-8")).hexdigest()
