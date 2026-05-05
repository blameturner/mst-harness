from __future__ import annotations

import asyncio
from unittest.mock import patch


def test_handle_missing_required_fields():
    from workers.task_handlers.project_feature import handle
    result = asyncio.run(handle({"input_payload": {"project_id": 1}}))
    assert result["status"] == "failed"
    assert "required" in result["error"]


def test_handle_missing_project_id():
    from workers.task_handlers.project_feature import handle
    result = asyncio.run(handle({"input_payload": {}}))
    assert result["status"] == "failed"


def test_handle_no_gitea_connection():
    from workers.task_handlers.project_feature import handle
    with patch("workers.task_handlers.project_feature._run") as mock_run:
        mock_run.return_value = {"status": "failed", "error": "no Gitea connection configured"}
        result = asyncio.run(handle({
            "input_payload": {
                "project_id": 1,
                "feature_description": "Add auth",
                "branch_name": "feature/auth",
            }
        }))
    assert result["status"] == "failed"
