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
