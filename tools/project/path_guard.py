"""Path security boundary for all project FS tools."""
from __future__ import annotations

import fnmatch
from pathlib import Path

DEFAULT_DENYLIST: list[str] = [
    ".git/**",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
]


def validate_path(
    repo_root: str | Path,
    requested: str,
    denylist: list[str] | None = None,
) -> Path:
    """Return a resolved absolute Path inside repo_root, or raise ValueError."""
    root = Path(repo_root).resolve()

    if Path(requested).is_absolute():
        raise ValueError(f"absolute path rejected: {requested!r}")

    candidate = (root / requested).resolve()

    try:
        candidate.relative_to(root)
    except ValueError:
        if (root / requested).exists() and (root / requested).is_symlink():
            raise ValueError(f"symlink escapes repo root: {requested!r}")
        raise ValueError(f"path escapes repo root: {requested!r}")

    rel = str(candidate.relative_to(root))
    all_patterns = list(DEFAULT_DENYLIST) + (denylist or [])
    for pattern in all_patterns:
        if _matches(rel, pattern):
            raise ValueError(f"path matches denylist: {pattern!r}: {requested!r}")

    return candidate


def _matches(rel_path: str, pattern: str) -> bool:
    """Match a relative path against a single denylist pattern."""
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        if rel_path == prefix or rel_path.startswith(prefix + "/"):
            return True
        return False
    if fnmatch.fnmatch(rel_path, pattern):
        return True
    filename = Path(rel_path).name
    return fnmatch.fnmatch(filename, pattern)
