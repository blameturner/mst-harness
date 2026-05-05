"""Router-level tests for /teaching endpoints."""
import json
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.teaching import router


def _make_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _curricula_row(curriculum_id=1, modules=None):
    mods = modules or [{"id": "m1", "title": "Intro", "status": "pending"}]
    return {
        "Id": curriculum_id,
        "topic": "Python",
        "org_id": 5,
        "root_goal": "Learn Python",
        "modules": json.dumps(mods),
        "current_module_index": 0,
        "UpdatedAt": "2024-01-01T00:00:00Z",
    }


def _lesson_row(lesson_id=10, task_id=99):
    return {
        "Id": lesson_id,
        "task_id": task_id,
        "curriculum_id": 1,
        "module_id": "m1",
        "lesson_markdown": "# Lesson",
        "anki_cards": "Q: ? A: !",
        "session_summary": "Covered basics.",
        "sources": json.dumps([{"url": "https://example.com", "title": "Ref"}]),
        "checks": json.dumps([]),
        "CreatedAt": "2024-01-01T00:00:00Z",
    }


def _task_row(task_type="teaching_curriculum", task_id=42):
    return {
        "Id": task_id,
        "task_type": task_type,
        "status": "ready",
        "input_payload": "{}",
        "agent": "teaching:5",
        "created_at": None,
        "updated_at": None,
        "output_payload": None,
        "error": None,
        "model": None,
    }


# ── GET /teaching/topics ─────────────────────────────────────────────────────

def test_list_topics_returns_topics():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._safe_list.return_value = [_curricula_row()]

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        resp = client.get("/teaching/topics?org_id=5")

    assert resp.status_code == 200
    topics = resp.json()["topics"]
    assert len(topics) == 1
    assert topics[0]["topic"] == "Python"
    assert topics[0]["module_count"] == 1


def test_list_topics_empty():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._safe_list.return_value = []

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        resp = client.get("/teaching/topics?org_id=5")

    assert resp.status_code == 200
    assert resp.json()["topics"] == []


# ── GET /teaching/curricula/{id} ─────────────────────────────────────────────

def test_get_curriculum_happy_path():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._safe_get.return_value = _curricula_row(curriculum_id=3)

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        resp = client.get("/teaching/curricula/3")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "3"
    assert body["topic"] == "Python"
    assert len(body["modules"]) == 1


def test_get_curriculum_not_found():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._safe_get.return_value = None

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        resp = client.get("/teaching/curricula/999")

    assert resp.status_code == 404


# ── POST /teaching/curricula ─────────────────────────────────────────────────

def test_create_curriculum_submits_task():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [_task_row("teaching_curriculum", 42)]}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db), \
         patch("workers.kanban.submit", return_value=42) as mock_submit:
        resp = client.post("/teaching/curricula", json={"topic": "Python", "org_id": 5})

    assert resp.status_code == 200
    assert resp.json()["id"] == "42"
    mock_submit.assert_called_once()
    call_args = mock_submit.call_args
    assert call_args.args[1] == "teaching_curriculum"
    assert call_args.args[2]["topic"] == "Python"


# ── GET /teaching/curricula/{id}/lessons ─────────────────────────────────────

def test_list_lessons_returns_list():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._safe_list.return_value = [_lesson_row()]

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        resp = client.get("/teaching/curricula/1/lessons")

    assert resp.status_code == 200
    lessons = resp.json()["lessons"]
    assert len(lessons) == 1
    assert lessons[0]["module_id"] == "m1"


# ── GET /teaching/lessons/{id} ───────────────────────────────────────────────

def test_get_lesson_happy_path():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._safe_get.return_value = _lesson_row(lesson_id=10)

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        resp = client.get("/teaching/lessons/10")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "10"
    assert body["lesson_markdown"] == "# Lesson"
    assert isinstance(body["sources"], list)
    assert isinstance(body["checks"], list)


def test_get_lesson_not_found():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._safe_get.return_value = None

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        resp = client.get("/teaching/lessons/999")

    assert resp.status_code == 404


# ── POST /teaching/lessons ────────────────────────────────────────────────────

def test_create_lesson_submits_task():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [_task_row("teaching_lesson", 77)]}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db), \
         patch("workers.kanban.submit", return_value=77) as mock_submit:
        resp = client.post("/teaching/lessons", json={
            "topic": "Python", "org_id": 5, "curriculum_id": 1, "module_id": "m1",
        })

    assert resp.status_code == 200
    assert resp.json()["id"] == "77"
    mock_submit.assert_called_once()
    assert mock_submit.call_args.args[1] == "teaching_lesson"


# ── POST /teaching/lessons/{id}/revise ───────────────────────────────────────

def test_revise_lesson_happy_path():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._safe_get.return_value = _lesson_row(lesson_id=10, task_id=99)
    mock_db._get.return_value = {"list": [_task_row("teaching_revision", 88)]}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db), \
         patch("workers.kanban.submit", return_value=88) as mock_submit:
        resp = client.post("/teaching/lessons/10/revise", json={"revision_instructions": "Fix examples"})

    assert resp.status_code == 200
    mock_submit.assert_called_once()
    payload = mock_submit.call_args.args[2]
    assert payload["parent_task_id"] == 99
    assert payload["revision_instructions"] == "Fix examples"


def test_revise_lesson_no_task_id_returns_404():
    client = _make_client()
    mock_db = MagicMock()
    row = _lesson_row()
    row["task_id"] = 0
    mock_db._safe_get.return_value = row

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        resp = client.post("/teaching/lessons/10/revise", json={"revision_instructions": "x"})

    assert resp.status_code == 404


def test_revise_lesson_not_found_returns_404():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._safe_get.return_value = None

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        resp = client.post("/teaching/lessons/999/revise", json={"revision_instructions": "x"})

    assert resp.status_code == 404


# ── POST /teaching/lessons/{id}/check ────────────────────────────────────────

def test_check_lesson_happy_path():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._safe_get.return_value = _lesson_row(lesson_id=10, task_id=99)
    mock_db._get.return_value = {"list": [_task_row("teaching_check", 55)]}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db), \
         patch("workers.kanban.submit", return_value=55) as mock_submit:
        resp = client.post("/teaching/lessons/10/check", json={"count": 3, "difficulty": "easy"})

    assert resp.status_code == 200
    payload = mock_submit.call_args.args[2]
    assert payload["parent_task_id"] == 99
    assert payload["count"] == 3
    assert payload["difficulty"] == "easy"


# ── GET /teaching/learner ─────────────────────────────────────────────────────

def test_get_learner_concepts():
    client = _make_client()
    mock_db = MagicMock()
    mock_db._safe_list.return_value = [
        {"Id": 1, "concept": "variables", "mastery": "practiced", "last_seen": None, "session_count": 3,
         "misconceptions": "confuses scope with lifetime", "preferred_style": "visual"},
    ]

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        resp = client.get("/teaching/learner?org_id=5&topic=Python")

    assert resp.status_code == 200
    concepts = resp.json()["concepts"]
    assert len(concepts) == 1
    assert concepts[0]["concept"] == "variables"
    assert concepts[0]["mastery"] == "practiced"
    assert concepts[0]["misconceptions"] == "confuses scope with lifetime"
    assert concepts[0]["preferred_style"] == "visual"
