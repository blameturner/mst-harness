from __future__ import annotations

import asyncio
from unittest.mock import patch


def test_handle_returns_failed_without_project_id():
    from workers.task_handlers.project_index import handle
    result = asyncio.run(handle({"input_payload": {}}))
    assert result["status"] == "failed"
    assert "project_id" in result["error"]


def test_handle_returns_done_for_empty_project():
    from workers.task_handlers.project_index import handle
    with patch("workers.task_handlers.project_index._run") as mock_run:
        mock_run.return_value = {"status": "done", "files_indexed": 0, "summary_chars": 0, "tokens_used": 0}
        result = asyncio.run(handle({"input_payload": {"project_id": 1}}))
    assert result["status"] == "done"
