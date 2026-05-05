from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch


def test_handle_missing_project_id():
    from workers.task_handlers.project_propose import handle
    result = asyncio.run(handle({"input_payload": {}}))
    assert result["status"] == "failed"


def test_handle_blocks_when_no_summary():
    from workers.task_handlers.project_propose import handle
    with patch("workers.task_handlers.project_propose._run") as mock_run:
        mock_run.return_value = {
            "status": "blocked",
            "error": "no repo summary available; project_index enqueued",
            "proposals": [],
            "queued": 0,
            "tokens_used": 0,
        }
        result = asyncio.run(handle({"input_payload": {"project_id": 1}}))
    assert result["status"] == "blocked"
    assert result["queued"] == 0
