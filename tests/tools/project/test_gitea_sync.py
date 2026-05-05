from unittest.mock import MagicMock, patch

from infra.gitea_client import GiteaError
from tools.project.gitea_sync import push_files_to_gitea


def _make_db(files: list[dict]) -> MagicMock:
    db = MagicMock()
    db.list_project_files.return_value = files
    return db


def _make_gitea(existing_sha: str = "") -> MagicMock:
    g = MagicMock()
    g.get_file_content.return_value = ("old content", existing_sha)
    g.put_file.return_value = {"content": {"sha": "abc"}}
    return g


def test_push_new_file():
    db = _make_db([{"path": "src/main.py", "content": "print('hi')", "Id": 1}])
    gitea = _make_gitea(existing_sha="")
    pushed = push_files_to_gitea(db, gitea, "owner", "repo", "feat-branch", 42, ["src/main.py"])
    assert pushed == ["src/main.py"]
    gitea.put_file.assert_called_once()
    call_kwargs = gitea.put_file.call_args
    assert call_kwargs.args[5] == "feat-branch"  # branch arg
    assert call_kwargs.args[4] != ""             # commit message non-empty


def test_push_existing_file_passes_sha():
    db = _make_db([{"path": "src/main.py", "content": "new content", "Id": 1}])
    gitea = _make_gitea(existing_sha="deadbeef")
    push_files_to_gitea(db, gitea, "owner", "repo", "feat-branch", 42, ["src/main.py"])
    call_kwargs = gitea.put_file.call_args
    assert call_kwargs.kwargs["sha"] == "deadbeef"


def test_push_skips_oversized_file(caplog):
    import logging
    big_content = "x" * 600_000
    db = _make_db([{"path": "large.bin", "content": big_content, "Id": 2}])
    gitea = _make_gitea()
    with caplog.at_level(logging.WARNING):
        pushed = push_files_to_gitea(db, gitea, "o", "r", "b", 1, ["large.bin"])
    assert pushed == []
    gitea.put_file.assert_not_called()


def test_push_skips_missing_file():
    db = _make_db([])  # file not found
    gitea = _make_gitea()
    pushed = push_files_to_gitea(db, gitea, "o", "r", "b", 1, ["missing.py"])
    assert pushed == []


def test_push_handles_gitea_error(caplog):
    import logging
    db = _make_db([{"path": "bad.py", "content": "code", "Id": 3}])
    gitea = _make_gitea()
    gitea.put_file.side_effect = GiteaError("conflict", 409)
    with caplog.at_level(logging.WARNING):
        pushed = push_files_to_gitea(db, gitea, "o", "r", "b", 1, ["bad.py"])
    assert pushed == []


def test_push_returns_only_succeeded():
    db = _make_db([
        {"path": "ok.py", "content": "good", "Id": 1},
        {"path": "bad.py", "content": "fail", "Id": 2},
    ])
    gitea = MagicMock()
    gitea.get_file_content.return_value = ("", "")
    gitea.put_file.side_effect = [{"content": {}}, Exception("boom")]
    pushed = push_files_to_gitea(db, gitea, "o", "r", "b", 1, ["ok.py", "bad.py"])
    assert pushed == ["ok.py"]
