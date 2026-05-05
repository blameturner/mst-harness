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
