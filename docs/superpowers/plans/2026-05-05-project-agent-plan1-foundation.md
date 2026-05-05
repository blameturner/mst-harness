# Project Agent — Plan 1: Foundation Layer

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the shared infrastructure every project handler depends on: path validation, FS tools, NocoDB knowledge-layer accessors, knowledge module, and Gitea PR/branch methods.

**Architecture:** Three new modules under `tools/project/` (path_guard, fs_tools, knowledge), new methods on `NocodbClient` and `GiteaClient`, and a test suite for the security-critical path validator. No handler code yet — Plan 2 consumes these.

**Tech Stack:** Python 3.11+, FastAPI/Pydantic, NocoDB REST, Gitea REST v1, pytest

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `tools/project/__init__.py` | Create | Package marker |
| `tools/project/path_guard.py` | Create | `validate_path()` — the single security boundary for all FS tools |
| `tools/project/fs_tools.py` | Create | Read and write tools operating on NocoDB project FS |
| `tools/project/knowledge.py` | Create | Repo summary + index read/write via NocoDB |
| `tests/tools/__init__.py` | Create | Package marker |
| `tests/tools/project/__init__.py` | Create | Package marker |
| `tests/tools/project/test_path_guard.py` | Create | 8 required test cases |
| `infra/nocodb_client.py` | Modify | Add 4 knowledge-layer accessor methods |
| `infra/gitea_client.py` | Modify | Add 8 PR/branch methods |

---

## Task 1: Package scaffolding and path validator

**Files:**
- Create: `tools/__init__.py` (if absent)
- Create: `tools/project/__init__.py`
- Create: `tools/project/path_guard.py`
- Create: `tests/tools/__init__.py`
- Create: `tests/tools/project/__init__.py`
- Create: `tests/tools/project/test_path_guard.py`

- [ ] **Step 1: Check whether `tools/__init__.py` already exists**

```bash
ls tools/
```

If absent, create it as an empty file. Create the three `__init__.py` markers.

```bash
touch tools/__init__.py 2>/dev/null || true
mkdir -p tests/tools/project
touch tests/tools/__init__.py tests/tools/project/__init__.py
```

- [ ] **Step 2: Write the failing tests first**

Create `tests/tools/project/test_path_guard.py`:

```python
"""Tests for the path security boundary. All 8 cases are load-bearing."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from tools.project.path_guard import validate_path


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A temporary directory acting as the repo root."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# hello")
    return tmp_path


def test_absolute_path_rejected(repo: Path) -> None:
    with pytest.raises(ValueError, match="absolute path rejected"):
        validate_path(repo, "/etc/passwd")


def test_dotdot_traversal_rejected(repo: Path) -> None:
    with pytest.raises(ValueError, match="path escapes repo root"):
        validate_path(repo, "../outside.txt")


def test_symlink_escapes_root_rejected(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret")
    link = repo / "evil_link"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink escapes repo root"):
        validate_path(repo, "evil_link")


def test_git_dir_rejected(repo: Path) -> None:
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("")
    with pytest.raises(ValueError, match=r"denylist"):
        validate_path(repo, ".git/config")


def test_key_file_rejected(repo: Path) -> None:
    with pytest.raises(ValueError, match=r"denylist"):
        validate_path(repo, "secrets.key")


def test_custom_denylist_blocks(repo: Path) -> None:
    with pytest.raises(ValueError, match=r"denylist"):
        validate_path(repo, "passwords.csv", denylist=["passwords.csv"])


def test_valid_nested_path(repo: Path) -> None:
    result = validate_path(repo, "src/main.py")
    assert result == repo / "src" / "main.py"


def test_valid_nonexistent_path(repo: Path) -> None:
    # Validation does not require the file to exist.
    result = validate_path(repo, "src/new_module.py")
    assert result == repo / "src" / "new_module.py"
```

- [ ] **Step 3: Run tests to confirm they all fail (module missing)**

```bash
python -m pytest tests/tools/project/test_path_guard.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'tools.project.path_guard'`

- [ ] **Step 4: Write `tools/project/path_guard.py`**

```python
"""Path security boundary for all project FS tools.

A single function used by every tool before any filesystem action.
One place to audit, one place to fix bugs.
"""
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
    """Return a resolved absolute Path inside repo_root, or raise ValueError.

    Rules applied in order — fails fast, never falls back, always names the
    violated rule so callers can surface a clear error to the agent.
    """
    root = Path(repo_root).resolve()

    # Rule 1: reject absolute paths
    if Path(requested).is_absolute():
        raise ValueError(f"absolute path rejected: {requested!r}")

    # Rule 2: join and resolve all symlinks
    candidate = (root / requested).resolve()

    # Rule 3: must still be inside the root after resolution (catches both
    #         .. traversal and symlinks that escape the root)
    try:
        candidate.relative_to(root)
    except ValueError:
        # Distinguish symlink escapes from traversal by checking if the
        # requested path exists and is a symlink before resolution.
        if (root / requested).exists() and (root / requested).is_symlink():
            raise ValueError(f"symlink escapes repo root: {requested!r}")
        raise ValueError(f"path escapes repo root: {requested!r}")

    # Rule 4: denylist — match relative path against all patterns
    rel = str(candidate.relative_to(root))
    all_patterns = list(DEFAULT_DENYLIST) + (denylist or [])
    for pattern in all_patterns:
        if _matches(rel, pattern):
            raise ValueError(f"path matches denylist: {pattern!r}: {requested!r}")

    return candidate


def _matches(rel_path: str, pattern: str) -> bool:
    """Match a relative path against a single denylist pattern.

    Handles .git/** style prefix patterns explicitly because Python's
    fnmatch does not support **.
    """
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        if rel_path == prefix or rel_path.startswith(prefix + "/"):
            return True
        return False
    # Match against the full relative path and against just the filename.
    if fnmatch.fnmatch(rel_path, pattern):
        return True
    filename = Path(rel_path).name
    return fnmatch.fnmatch(filename, pattern)
```

- [ ] **Step 5: Run tests — all 8 must pass**

```bash
python -m pytest tests/tools/project/test_path_guard.py -v
```

Expected:
```
PASSED tests/tools/project/test_path_guard.py::test_absolute_path_rejected
PASSED tests/tools/project/test_path_guard.py::test_dotdot_traversal_rejected
PASSED tests/tools/project/test_path_guard.py::test_symlink_escapes_root_rejected
PASSED tests/tools/project/test_path_guard.py::test_git_dir_rejected
PASSED tests/tools/project/test_path_guard.py::test_key_file_rejected
PASSED tests/tools/project/test_path_guard.py::test_custom_denylist_blocks
PASSED tests/tools/project/test_path_guard.py::test_valid_nested_path
PASSED tests/tools/project/test_path_guard.py::test_valid_nonexistent_path
8 passed
```

- [ ] **Step 6: Commit**

```bash
git add tools/__init__.py tools/project/__init__.py tools/project/path_guard.py \
        tests/tools/__init__.py tests/tools/project/__init__.py \
        tests/tools/project/test_path_guard.py
git commit -m "feat: add project path security boundary with full test suite"
```

---

## Task 2: NocoDB knowledge-layer accessors

**Files:**
- Modify: `infra/nocodb_client.py` (append 4 methods before the closing of the class)

- [ ] **Step 1: Write failing tests**

Add to `tests/tools/project/test_path_guard.py` or create `tests/infra/test_nocodb_knowledge.py`. Create `tests/infra/__init__.py` if absent.

```python
"""Unit tests for NocoDB knowledge-layer accessors using a mocked client."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from infra.nocodb_client import NocodbClient


@pytest.fixture()
def client():
    c = NocodbClient.__new__(NocodbClient)
    c.tables = {"project_repo_summaries", "project_repo_index"}
    return c


def test_get_repo_summary_returns_none_when_absent(client):
    client._safe_get = MagicMock(return_value=None)
    assert client.get_repo_summary(1) is None


def test_get_repo_summary_returns_row(client):
    client._safe_get = MagicMock(return_value={"Id": 1, "project_id": 1, "content": "summary"})
    result = client.get_repo_summary(1)
    assert result["content"] == "summary"


def test_upsert_repo_summary_creates_when_absent(client):
    client.get_repo_summary = MagicMock(return_value=None)
    client._safe_post = MagicMock(return_value={"Id": 1})
    client._patch = MagicMock()
    result = client.upsert_repo_summary(1, "new content", model_used="t1_primary")
    client._safe_post.assert_called_once()
    call_payload = client._safe_post.call_args[0][1]
    assert call_payload["content"] == "new content"
    assert call_payload["project_id"] == 1


def test_upsert_repo_summary_patches_when_exists(client):
    client.get_repo_summary = MagicMock(return_value={"Id": 7, "project_id": 1})
    client._patch = MagicMock(return_value={"Id": 7})
    client._safe_post = MagicMock()
    client.upsert_repo_summary(1, "updated")
    client._patch.assert_called_once()
    client._safe_post.assert_not_called()


def test_list_repo_index_returns_empty_when_table_absent(client):
    client.tables = set()  # table missing
    result = client.list_repo_index(1)
    assert result == []


def test_upsert_repo_index_entries(client):
    client._safe_get = MagicMock(return_value=None)
    client._safe_post = MagicMock(return_value={"Id": 1})
    entries = [{"path": "src/foo.py", "purpose": "does stuff", "key_exports": ["Foo"], "dependencies": []}]
    client.upsert_repo_index_entries(1, entries)
    client._safe_post.assert_called_once()
```

- [ ] **Step 2: Run to confirm failures**

```bash
python -m pytest tests/infra/test_nocodb_knowledge.py -v 2>&1 | head -20
```

Expected: `AttributeError: 'NocodbClient' object has no attribute 'get_repo_summary'`

- [ ] **Step 3: Add the 4 methods to `infra/nocodb_client.py`**

Find the last method before the end of the `NocodbClient` class (around the `_safe_get` method) and append after it:

```python
    # ── Repo knowledge layer ──────────────────────────────────────────────────

    def get_repo_summary(self, project_id: int) -> dict | None:
        return self._safe_get(
            "project_repo_summaries",
            f"(project_id,eq,{project_id})",
        )

    def upsert_repo_summary(
        self,
        project_id: int,
        content: str,
        model_used: str = "",
    ) -> dict:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        existing = self.get_repo_summary(project_id)
        payload: dict = {
            "project_id": project_id,
            "content": content,
            "last_indexed_at": now,
            "model_used": model_used,
        }
        if existing:
            return self._patch("project_repo_summaries", int(existing["Id"]), payload) or existing
        return self._safe_post("project_repo_summaries", payload) or {}

    def list_repo_index(
        self,
        project_id: int,
        path_filter: str | None = None,
    ) -> list[dict]:
        where = f"(project_id,eq,{project_id})"
        if path_filter:
            where = f"{where}~and(path,like,{path_filter})"
        return self._safe_list("project_repo_index", where, sort="path")

    def upsert_repo_index_entries(
        self,
        project_id: int,
        entries: list[dict],
    ) -> None:
        """Upsert each entry on (project_id, path)."""
        import json as _json
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for entry in entries:
            path = str(entry.get("path") or "")
            if not path:
                continue
            payload = {
                "project_id": project_id,
                "path": path,
                "purpose": str(entry.get("purpose") or ""),
                "key_exports": _json.dumps(entry.get("key_exports") or []),
                "dependencies": _json.dumps(entry.get("dependencies") or []),
                "last_indexed_at": now,
            }
            existing = self._safe_get(
                "project_repo_index",
                f"(project_id,eq,{project_id})~and(path,eq,{path})",
            )
            if existing:
                self._patch("project_repo_index", int(existing["Id"]), payload)
            else:
                self._safe_post("project_repo_index", payload)
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/infra/test_nocodb_knowledge.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add infra/nocodb_client.py tests/infra/__init__.py tests/infra/test_nocodb_knowledge.py
git commit -m "feat: add repo summary and index accessors to NocodbClient"
```

---

## Task 3: Knowledge module

**Files:**
- Create: `tools/project/knowledge.py`

- [ ] **Step 1: Write the module**

```python
"""Read and write helpers for the repo knowledge layer (summary + index).

All functions take a NocodbClient instance rather than creating one, so
callers control the DB connection lifetime.
"""
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

    If `section` is given, the existing summary is parsed as
    '## Section\\n...\\n## Next' blocks and only the named section is replaced.
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


# ── internal helpers ──────────────────────────────────────────────────────────

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
```

- [ ] **Step 2: Add a quick smoke test**

Append to `tests/infra/test_nocodb_knowledge.py`:

```python
from tools.project.knowledge import read_repo_summary, write_repo_summary, read_repo_index, _replace_section


def test_read_repo_summary_empty_when_no_row():
    db = MagicMock()
    db.get_repo_summary = MagicMock(return_value=None)
    assert read_repo_summary(db, 1) == ""


def test_replace_section_updates_existing():
    text = "## Intro\nold content\n\n## Stack\nPython\n\n"
    result = _replace_section(text, "Stack", "Go")
    assert "Go" in result
    assert "Python" not in result
    assert "## Intro" in result


def test_replace_section_appends_when_missing():
    text = "## Intro\nsome text\n"
    result = _replace_section(text, "NewSection", "new content")
    assert "## NewSection" in result
    assert "new content" in result
```

- [ ] **Step 3: Run**

```bash
python -m pytest tests/infra/test_nocodb_knowledge.py -v
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tools/project/knowledge.py tests/infra/test_nocodb_knowledge.py
git commit -m "feat: add project knowledge module (repo summary + index read/write)"
```

---

## Task 4: FS read tools

**Files:**
- Create: `tools/project/fs_tools.py`

The path guard is used by all write tools. Read tools also validate paths to prevent agents from probing arbitrary paths even though they are read-only.

- [ ] **Step 1: Write `tools/project/fs_tools.py` — read section**

```python
"""Project filesystem tools for agent use.

Read tools: available to all agent roles (Coder, Architect, PO).
Write tools: Coder only.

All tools validate paths through validate_path() before any operation.
All write tools log at INFO with task_id, operation, and path.
"""
from __future__ import annotations

import fnmatch
import logging
from pathlib import Path, PurePosixPath

from tools.project.path_guard import validate_path, DEFAULT_DENYLIST

_log = logging.getLogger("project.fs_tools")

# Sentinel repo_root for NocoDB-backed virtual FS. Paths must be relative and
# non-traversing; we use a temporary root just to run the validator logic.
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
        # Depth check: count path separators after the prefix
        tail = rel[len(prefix):]
        if tail.count("/") < depth:
            out.append({"path": rel, "kind": f.get("kind", ""), "size_bytes": f.get("size_bytes", 0)})
    return out


def read_repo_tree(db, project_id: int, max_depth: int = 3) -> dict:
    """Return nested tree structure (no content). Each node: {name, path, kind, size_bytes, children?}."""
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
```

- [ ] **Step 2: Write tests for read tools**

Create `tests/tools/project/test_fs_tools_read.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tools.project.fs_tools import read_file, read_directory, search_repo


def _db(files=None, version_content="hello"):
    db = MagicMock()
    db.list_project_files.return_value = files or [
        {"path": "src/main.py", "kind": "file", "size_bytes": 5, "current_version_id": 1},
    ]
    db.get_project_file.return_value = {"current_version_id": 1}
    db.get_project_file_version.return_value = {"content": version_content}
    return db


def test_read_file_returns_content():
    db = _db(version_content="print('hello')")
    assert read_file(db, 1, "src/main.py") == "print('hello')"


def test_read_file_missing_raises():
    db = MagicMock()
    db.get_project_file.return_value = None
    with pytest.raises(KeyError):
        read_file(db, 1, "src/missing.py")


def test_read_file_rejects_absolute():
    db = MagicMock()
    with pytest.raises(ValueError, match="absolute path rejected"):
        read_file(db, 1, "/etc/passwd")


def test_read_directory_filters_prefix():
    db = MagicMock()
    db.list_project_files.return_value = [
        {"path": "src/a.py", "kind": "file", "size_bytes": 1, "current_version_id": 1},
        {"path": "tests/b.py", "kind": "file", "size_bytes": 1, "current_version_id": 2},
    ]
    result = read_directory(db, 1, "src")
    paths = [r["path"] for r in result]
    assert "src/a.py" in paths
    assert "tests/b.py" not in paths


def test_search_repo_finds_match():
    db = _db(version_content="def hello_world():\n    pass\n")
    results = search_repo(db, 1, "hello_world")
    assert len(results) == 1
    assert results[0]["line"] == 1
    assert "hello_world" in results[0]["snippet"]


def test_search_repo_no_match():
    db = _db(version_content="x = 1")
    assert search_repo(db, 1, "nonexistent_token") == []
```

- [ ] **Step 3: Run**

```bash
python -m pytest tests/tools/project/test_fs_tools_read.py -v
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tools/project/fs_tools.py tests/tools/project/test_fs_tools_read.py
git commit -m "feat: add project FS read tools (read_file, read_directory, read_repo_tree, search_repo)"
```

---

## Task 5: FS write tools

**Files:**
- Modify: `tools/project/fs_tools.py` (append write tools section)

- [ ] **Step 1: Append write tools to `tools/project/fs_tools.py`**

```python
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
    """Edit an existing file. mode: 'replace' | 'append' | 'patch'.

    Builds a markdown file-fence block and delegates to apply_file_fences()
    so all mode logic lives in one place.
    """
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
    """Move a file. Alias of rename_file with directory-move semantics name."""
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
```

- [ ] **Step 2: Write tests for write tools**

Create `tests/tools/project/test_fs_tools_write.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from tools.project.fs_tools import (
    create_file, delete_file, rename_file, delete_directory,
)


def _db_with_file(path="src/x.py", content="old"):
    db = MagicMock()
    db.get_project_file.return_value = {"current_version_id": 1, "path": path}
    db.get_project_file_version.return_value = {"content": content}
    db.write_project_file_version.return_value = (
        {"Id": 1, "path": path},
        {"version": 2},
        True,
    )
    db.archive_project_file.return_value = {"ok": True}
    return db


def test_create_file_calls_write():
    db = MagicMock()
    db.get_project_file.return_value = None
    db.write_project_file_version.return_value = ({"Id": 1}, {"version": 1}, True)
    create_file(db, 1, "src/new.py", "content", task_id="t1")
    db.write_project_file_version.assert_called_once()
    args = db.write_project_file_version.call_args
    assert args[0][2] == "content"


def test_create_file_rejects_duplicate():
    db = MagicMock()
    db.get_project_file.return_value = {"Id": 1}
    with pytest.raises(ValueError, match="already exists"):
        create_file(db, 1, "src/existing.py", "x")


def test_create_file_rejects_absolute():
    db = MagicMock()
    with pytest.raises(ValueError, match="absolute path rejected"):
        create_file(db, 1, "/etc/evil.py", "x")


def test_delete_file_archives():
    db = _db_with_file()
    result = delete_file(db, 1, "src/x.py", task_id="t1")
    db.archive_project_file.assert_called_once_with(1, "src/x.py", audit_actor="task:t1")
    assert result["archived"] is True


def test_rename_creates_new_and_deletes_old():
    db = _db_with_file("src/old.py", "code")
    db.get_project_file.side_effect = [
        {"current_version_id": 1},   # read during rename (old path check in read_file)
        None,                          # new path check in create_file
    ]
    rename_file(db, 1, "src/old.py", "src/new.py", task_id="t1")
    assert db.write_project_file_version.called
    assert db.archive_project_file.called


def test_delete_directory_non_empty_without_recursive_raises():
    db = MagicMock()
    db.list_project_files.return_value = [{"path": "dir/file.py"}]
    with pytest.raises(ValueError, match="recursive=True"):
        delete_directory(db, 1, "dir", recursive=False)


def test_delete_directory_recursive_archives_all():
    db = MagicMock()
    db.list_project_files.return_value = [
        {"path": "dir/a.py"},
        {"path": "dir/b.py"},
    ]
    db.archive_project_file.return_value = {"ok": True}
    result = delete_directory(db, 1, "dir", recursive=True, task_id="t1")
    assert result["deleted"] == 2
    assert db.archive_project_file.call_count == 2
```

- [ ] **Step 3: Run**

```bash
python -m pytest tests/tools/project/test_fs_tools_write.py -v
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tools/project/fs_tools.py tests/tools/project/test_fs_tools_write.py
git commit -m "feat: add project FS write tools (create, edit, delete, rename, move, mkdir, rmdir)"
```

---

## Task 6: Gitea PR/branch methods

**Files:**
- Modify: `infra/gitea_client.py`

- [ ] **Step 1: Write failing tests**

Create `tests/infra/test_gitea_pr.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from infra.gitea_client import GiteaClient


@pytest.fixture()
def client():
    return GiteaClient("http://git.example.com", "fake-token", username="bot")


def test_create_branch(client):
    with patch.object(client, "_post", return_value={"name": "feature/x"}) as mock_post:
        result = client.create_branch("owner", "repo", "feature/x", from_branch="staging")
    mock_post.assert_called_once_with(
        "/repos/owner/repo/branches",
        {"new_branch_name": "feature/x", "old_branch_name": "staging"},
    )
    assert result["name"] == "feature/x"


def test_create_pr(client):
    with patch.object(client, "_post", return_value={"number": 7}) as mock_post:
        result = client.create_pr("owner", "repo", "feat: add thing", "feature/x", "staging")
    mock_post.assert_called_once_with(
        "/repos/owner/repo/pulls",
        {"title": "feat: add thing", "head": "feature/x", "base": "staging", "body": ""},
    )
    assert result["number"] == 7


def test_get_pr_diff(client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "--- a/file.py\n+++ b/file.py\n"
    mock_resp.content = b"--- a/file.py\n+++ b/file.py\n"
    with patch("requests.get", return_value=mock_resp):
        diff = client.get_pr_diff("owner", "repo", 7)
    assert "--- a/file.py" in diff


def test_merge_pr(client):
    with patch.object(client, "_post", return_value=None) as mock_post:
        client.merge_pr("owner", "repo", 7)
    mock_post.assert_called_once_with(
        "/repos/owner/repo/pulls/7/merge",
        {"Do": "merge", "merge_message_field": ""},
    )


def test_close_pr(client):
    with patch.object(client, "_patch_raw", return_value={"state": "closed"}) as mock_patch:
        client.close_pr("owner", "repo", 7)
    mock_patch.assert_called_once()


def test_list_issues(client):
    with patch.object(client, "_get", return_value=[{"number": 1, "title": "bug"}]):
        issues = client.list_issues("owner", "repo", state="open")
    assert issues[0]["title"] == "bug"
```

- [ ] **Step 2: Run to confirm failures**

```bash
python -m pytest tests/infra/test_gitea_pr.py -v 2>&1 | head -20
```

Expected: `AttributeError: 'GiteaClient' has no attribute 'create_branch'`

- [ ] **Step 3: Add the 8 methods to `infra/gitea_client.py`**

Locate the `list_commits` method at the end of the file and append a new section after it. Also add `_patch_raw` helper if it doesn't exist.

First, check whether `_patch` already exists (it does — `_put` and `_post` exist; add `_patch_raw` for PATCH requests):

```python
    def _patch_raw(self, path: str, body: dict) -> Any:
        import requests as _req
        r = _req.patch(self._url(path), headers=self._headers(), json=body, timeout=REQUEST_TIMEOUT)
        return self._check(r)
```

Then add the PR/branch section:

```python
    # ---------- branches & PRs ----------

    def create_branch(
        self,
        owner: str,
        repo: str,
        branch_name: str,
        from_branch: str = "main",
    ) -> dict:
        """Create a new branch from from_branch."""
        return self._post(
            f"/repos/{_enc(owner)}/{_enc(repo)}/branches",
            {"new_branch_name": branch_name, "old_branch_name": from_branch},
        ) or {}

    def list_prs(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        limit: int = 50,
    ) -> list[dict]:
        data = self._get(
            f"/repos/{_enc(owner)}/{_enc(repo)}/pulls",
            params={"state": state, "limit": limit},
        )
        return data if isinstance(data, list) else []

    def create_pr(
        self,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
    ) -> dict:
        return self._post(
            f"/repos/{_enc(owner)}/{_enc(repo)}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
        ) or {}

    def get_pr(self, owner: str, repo: str, pr_id: int) -> dict:
        return self._get(f"/repos/{_enc(owner)}/{_enc(repo)}/pulls/{pr_id}") or {}

    def get_pr_diff(self, owner: str, repo: str, pr_id: int) -> str:
        """Return the unified diff for a PR as a plain string."""
        url = self._url(f"/repos/{_enc(owner)}/{_enc(repo)}/pulls/{pr_id}.diff")
        import requests as _req
        r = _req.get(url, headers={"Authorization": f"token {self.token}"}, timeout=REQUEST_TIMEOUT * 2)
        if r.status_code >= 400:
            raise GiteaError(f"get_pr_diff {pr_id} -> {r.status_code}: {r.text[:200]}", r.status_code)
        return r.text

    def merge_pr(
        self,
        owner: str,
        repo: str,
        pr_id: int,
        merge_method: str = "merge",
        message: str = "",
    ) -> None:
        self._post(
            f"/repos/{_enc(owner)}/{_enc(repo)}/pulls/{pr_id}/merge",
            {"Do": merge_method, "merge_message_field": message},
        )

    def close_pr(self, owner: str, repo: str, pr_id: int) -> dict:
        return self._patch_raw(
            f"/repos/{_enc(owner)}/{_enc(repo)}/pulls/{pr_id}",
            {"state": "closed"},
        ) or {}

    def list_issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        limit: int = 50,
    ) -> list[dict]:
        data = self._get(
            f"/repos/{_enc(owner)}/{_enc(repo)}/issues",
            params={"state": state, "type": "issues", "limit": limit},
        )
        return data if isinstance(data, list) else []
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/infra/test_gitea_pr.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add infra/gitea_client.py tests/infra/test_gitea_pr.py
git commit -m "feat: add Gitea PR/branch methods (create_branch, create_pr, get_pr_diff, merge_pr, close_pr, list_issues)"
```

---

**Plan 1 complete.** All foundation modules are tested and committed. Proceed to Plan 2 (Handlers).