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
