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


def test_check_autonomy_is_called():
    """project_index must call check_autonomy so the daily token cap and
    hourly rate apply — it is auto-enqueued frequently and was previously exempt."""
    from workers.task_handlers import project_index as mod
    from unittest.mock import MagicMock, patch

    db = MagicMock()
    db.list_project_files.return_value = []

    with patch("infra.nocodb_client.NocodbClient", return_value=db), \
         patch.object(mod, "resolve_agent_model", return_value="project_po"), \
         patch.object(mod, "check_autonomy") as mock_check:
        asyncio.run(mod.handle({"Id": 1, "input_payload": {"project_id": 5}}))

    mock_check.assert_called_once()
