"""Project filesystem tools for agent use."""
from __future__ import annotations

import fnmatch
import logging
from pathlib import Path, PurePosixPath

from tools.project.path_guard import validate_path, DEFAULT_DENYLIST

_log = logging.getLogger("project.fs_tools")

_VIRTUAL_ROOT = Path("/virtual/repo")


def _vpath(path: str, denylist: list[str] | None = None) -> str:
    """Validate a virtual FS path and return the clean relative string."""
    resolved = validate_path(_VIRTUAL_ROOT, path, denylist=denylist)
    return str(resolved.relative_to(_VIRTUAL_ROOT))


# ── Read tools ────────────────────────────────────────────────────────────────

def read_file(db, project_id: int, path: str) -> str:
    """Return content of the current file version. Raises KeyError if absent."""
    clean = _vpath(path)
    file_row = db.get_project_file(project_id, clean)
    if not file_row:
        raise KeyError(f"file not found: {path!r}")
    version_id = file_row.get("current_version_id")
    if not version_id:
        return ""
    version = db.get_project_file_version(int(version_id))
    return str(version.get("content") or "") if version else ""


def read_directory(db, project_id: int, path: str, depth: int = 1) -> list[dict]:
    """List files under path up to depth levels.

    Returns [{path, kind, size_bytes}].
    """
    clean = _vpath(path) if path and path not in (".", "/", "") else ""
    prefix = (clean + "/") if clean else ""
    all_files = db.list_project_files(project_id, prefix=prefix if prefix else None)
    out = []
    for f in all_files:
        rel = f.get("path", "")
        if prefix and not rel.startswith(prefix):
            continue
        tail = rel[len(prefix):]
        if tail.count("/") < depth:
            out.append({"path": rel, "kind": f.get("kind", ""), "size_bytes": f.get("size_bytes", 0)})
    return out


def read_repo_tree(db, project_id: int, max_depth: int = 3) -> dict:
    """Return nested tree structure (no content)."""
    all_files = db.list_project_files(project_id)
    root: dict = {"children": {}}
    for f in all_files:
        parts = PurePosixPath(f["path"]).parts
        if len(parts) - 1 > max_depth:
            continue
        node = root
        for part in parts[:-1]:
            node = node["children"].setdefault(part, {"children": {}})
        leaf = parts[-1]
        node["children"][leaf] = {
            "path": f["path"],
            "kind": f.get("kind", ""),
            "size_bytes": f.get("size_bytes", 0),
        }
    return _flatten_tree(root, "")


def _flatten_tree(node: dict, prefix: str) -> dict:
    out = {}
    for name, child in node.get("children", {}).items():
        path = f"{prefix}/{name}".lstrip("/")
        if "children" in child:
            out[name] = {"path": path, "children": _flatten_tree(child, path)}
        else:
            out[name] = child
    return out


def search_repo(
    db,
    project_id: int,
    query: str,
    glob_pattern: str | None = None,
) -> list[dict]:
    """Content search across file versions. Returns [{path, line, snippet}]."""
    all_files = db.list_project_files(project_id)
    results = []
    query_lower = query.lower()
    for f in all_files:
        fpath = f.get("path", "")
        if glob_pattern and not fnmatch.fnmatch(fpath, glob_pattern):
            continue
        version_id = f.get("current_version_id")
        if not version_id:
            continue
        version = db.get_project_file_version(int(version_id))
        content = str(version.get("content") or "") if version else ""
        for lineno, line in enumerate(content.splitlines(), 1):
            if query_lower in line.lower():
                results.append({"path": fpath, "line": lineno, "snippet": line.strip()[:200]})
    return results


# ── Write tools (Coder only) ──────────────────────────────────────────────────

def create_file(
    db,
    project_id: int,
    path: str,
    content: str,
    task_id: str = "",
) -> dict:
    """Create a new file. Raises ValueError if it already exists."""
    clean = _vpath(path)
    if db.get_project_file(project_id, clean):
        raise ValueError(f"file already exists: {path!r} — use edit_file to update")
    file_row, version_row, _ = db.write_project_file_version(
        project_id, clean, content,
        edit_summary=f"created by task {task_id}",
        created_by=f"task:{task_id}",
        audit_actor=f"task:{task_id}",
        audit_kind="file_create",
    )
    _log.info("fs create  task=%s  project=%s  path=%s", task_id, project_id, clean)
    return {"path": clean, "version": version_row.get("version"), "changed": True}


def edit_file(
    db,
    project_id: int,
    path: str,
    mode: str,
    content: str,
    content_hash: str | None = None,
    task_id: str = "",
) -> dict:
    """Edit an existing file. mode: 'replace' | 'append' | 'patch'."""
    if mode not in ("replace", "append", "patch"):
        raise ValueError(f"unsupported mode: {mode!r}")
    clean = _vpath(path)
    fence = f'```file path="{clean}" mode="{mode}" summary="task {task_id}"\n{content}\n```'
    from workers.code.fs_parser import apply_file_fences
    changes = apply_file_fences(
        db=db,
        project_id=project_id,
        response_text=fence,
    )
    _log.info("fs edit  task=%s  project=%s  mode=%s  path=%s", task_id, project_id, mode, clean)
    return changes[0] if changes else {"path": clean, "changed": False}


def delete_file(
    db,
    project_id: int,
    path: str,
    task_id: str = "",
) -> dict:
    """Archive a file (soft delete)."""
    clean = _vpath(path)
    result = db.archive_project_file(project_id, clean, audit_actor=f"task:{task_id}")
    _log.info("fs delete  task=%s  project=%s  path=%s", task_id, project_id, clean)
    return {"path": clean, "archived": True, **result}


def rename_file(
    db,
    project_id: int,
    old_path: str,
    new_path: str,
    task_id: str = "",
) -> dict:
    """Rename by writing content to new path and archiving old."""
    clean_old = _vpath(old_path)
    clean_new = _vpath(new_path)
    content = read_file(db, project_id, clean_old)
    create_file(db, project_id, clean_new, content, task_id=task_id)
    delete_file(db, project_id, clean_old, task_id=task_id)
    _log.info("fs rename  task=%s  project=%s  %s -> %s", task_id, project_id, clean_old, clean_new)
    return {"old_path": clean_old, "new_path": clean_new, "changed": True}


def move_file(
    db,
    project_id: int,
    src: str,
    dst: str,
    task_id: str = "",
) -> dict:
    """Move a file. Alias of rename_file."""
    return rename_file(db, project_id, src, dst, task_id=task_id)


def create_directory(
    db,
    project_id: int,
    path: str,
) -> dict:
    """Create a directory placeholder by writing .gitkeep inside it."""
    clean = _vpath(path)
    gitkeep = f"{clean}/.gitkeep"
    db.write_project_file_version(
        project_id, gitkeep, "",
        edit_summary="directory placeholder",
        created_by="system",
    )
    return {"path": clean, "created": True}


def delete_directory(
    db,
    project_id: int,
    path: str,
    recursive: bool = False,
    task_id: str = "",
) -> dict:
    """Archive all files under path. Requires recursive=True for non-empty."""
    clean = _vpath(path)
    prefix = clean.rstrip("/") + "/"
    files = db.list_project_files(project_id, prefix=prefix)
    if files and not recursive:
        raise ValueError(
            f"directory not empty ({len(files)} files); pass recursive=True to delete"
        )
    for f in files:
        db.archive_project_file(project_id, f["path"], audit_actor=f"task:{task_id}")
    _log.info(
        "fs rmdir  task=%s  project=%s  path=%s  files=%d  recursive=%s",
        task_id, project_id, clean, len(files), recursive,
    )
    return {"path": clean, "deleted": len(files)}
