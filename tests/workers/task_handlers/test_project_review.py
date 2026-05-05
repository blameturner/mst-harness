from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch


def test_handle_missing_fields():
    from workers.task_handlers.project_review import handle
    result = asyncio.run(handle({"input_payload": {"project_id": 1}}))
    assert result["status"] == "failed"
    assert "required" in result["error"]


def test_max_revisions_constant():
    from workers.task_handlers.project_review import _MAX_REVISIONS
    assert _MAX_REVISIONS == 2


def test_do_reject_closes_pr():
    from workers.task_handlers.project_review import _do_reject
    gitea = MagicMock()
    result = _do_reject(gitea, "owner", "repo", 7, "too complex", 0)
    gitea.close_pr.assert_called_once_with("owner", "repo", 7)
    assert result["verdict"] == "reject"
    assert result["status"] == "done"
