import json
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.tasks import router


def _make_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _task_row(task_type="project_human_review", payload=None):
    p = payload or {"project_id": 10, "human_feedback": None, "changed_paths": ["a.py"]}
    return {"Id": 55, "task_type": task_type, "status": "blocked",
            "input_payload": json.dumps(p), "agent": "project:10",
            "created_at": None, "updated_at": None, "output_payload": None,
            "error": None, "model": None, "prompt_template_id": None}


def test_approve_without_feedback_sets_ready():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [_task_row()]}

    with patch("app.routers.tasks.NocodbClient", return_value=mock_db):
        resp = client.post("/tasks/projects/10/human-reviews/55/approve", json={})

    assert resp.status_code == 200
    data = resp.json()
    assert data["approved"] is True
    assert data["has_feedback"] is False
    mock_db._patch.assert_called_once()
    patch_call = mock_db._patch.call_args
    assert patch_call.args[2]["status"] == "ready"


def test_approve_with_feedback_stores_it():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [_task_row()]}

    with patch("app.routers.tasks.NocodbClient", return_value=mock_db):
        resp = client.post(
            "/tasks/projects/10/human-reviews/55/approve",
            json={"feedback": "Fix error handling in a.py"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_feedback"] is True
    patch_call = mock_db._patch.call_args
    patched_payload = json.loads(patch_call.args[2]["input_payload"])
    assert patched_payload["human_feedback"] == "Fix error handling in a.py"


def test_approve_wrong_task_type_returns_400():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [_task_row(task_type="project_review")]}

    with patch("app.routers.tasks.NocodbClient", return_value=mock_db):
        resp = client.post("/tasks/projects/10/human-reviews/55/approve", json={})

    assert resp.status_code == 400


def test_approve_task_not_found_returns_404():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._get.return_value = {"list": []}

    with patch("app.routers.tasks.NocodbClient", return_value=mock_db):
        resp = client.post("/tasks/projects/10/human-reviews/99/approve", json={})

    assert resp.status_code == 404


def test_autonomy_settings_has_threshold_field():
    from app.routers.tasks import AutonomySettings
    assert "human_review_threshold_files" in AutonomySettings.model_fields
