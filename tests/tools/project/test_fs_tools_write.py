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
