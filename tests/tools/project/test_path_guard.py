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
