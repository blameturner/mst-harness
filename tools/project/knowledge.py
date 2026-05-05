"""Read and write helpers for the repo knowledge layer (summary + index)."""
from __future__ import annotations

import json
import logging

_log = logging.getLogger("project.knowledge")


def read_repo_summary(db, project_id: int) -> str:
    """Return the persisted prose summary, or '' if none exists."""
    row = db.get_repo_summary(project_id)
    return str(row.get("content") or "") if row else ""


def write_repo_summary(
    db,
    project_id: int,
    content: str,
    section: str | None = None,
    model_used: str = "",
) -> dict:
    """Replace the full summary, or update a named section.

    If `section` is given, only the named ## Section block is replaced.
    """
    if section:
        existing = read_repo_summary(db, project_id)
        content = _replace_section(existing, section, content)
    return db.upsert_repo_summary(project_id, content, model_used=model_used)


def read_repo_index(
    db,
    project_id: int,
    path_filter: str | None = None,
) -> list[dict]:
    """Return per-file index entries, optionally filtered by path glob."""
    rows = db.list_repo_index(project_id, path_filter=path_filter)
    out = []
    for row in rows:
        out.append({
            "path": row.get("path", ""),
            "purpose": row.get("purpose", ""),
            "key_exports": _parse_json_list(row.get("key_exports")),
            "dependencies": _parse_json_list(row.get("dependencies")),
            "last_indexed_at": row.get("last_indexed_at"),
        })
    return out


def write_repo_index(
    db,
    project_id: int,
    entries: list[dict],
) -> dict:
    """Upsert per-file index entries. Each entry must have 'path'."""
    db.upsert_repo_index_entries(project_id, entries)
    return {"upserted": len(entries)}


def _parse_json_list(value: object) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _replace_section(full_text: str, section: str, new_content: str) -> str:
    """Replace a '## Section' block in markdown text.

    If the section is not found, appends it at the end.
    """
    import re
    heading = f"## {section}"
    pattern = re.compile(
        rf"(^## {re.escape(section)}\n)(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    replacement = f"{heading}\n{new_content.rstrip()}\n\n"
    if pattern.search(full_text):
        return pattern.sub(replacement, full_text)
    return full_text.rstrip() + f"\n\n{replacement}"
