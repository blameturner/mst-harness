"""Unit tests for teaching kanban task handlers.

teaching_lesson is excluded — its research pipeline dependency chain
requires integration-level setup; the boundary contracts (missing fields,
bad parent) are covered by the other handlers which share the same pattern.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch


# ── teaching_curriculum ───────────────────────────────────────────────────────

def _curriculum_task(topic="Python", org_id=5, curriculum_id=None, root_goal=None, learner_note=None):
    return {
        "Id": 1,
        "input_payload": {
            "topic": topic,
            "org_id": org_id,
            "curriculum_id": curriculum_id,
            "root_goal": root_goal,
            "learner_note": learner_note,
        },
    }


_sample_modules = [
    {"id": "m1", "title": "Basics", "objectives": ["vars"], "prerequisites": [], "depth": "introductory", "status": "pending", "amended_reason": None},
    {"id": "m2", "title": "Functions", "objectives": ["def"], "prerequisites": ["m1"], "depth": "working", "status": "pending", "amended_reason": None},
]


def test_curriculum_valid_payload_returns_completed():
    from workers.task_handlers import teaching_curriculum as mod

    mock_db = MagicMock()
    mock_row = {"Id": 7}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db), \
         patch("tools.teaching.db.get_learner_concepts", return_value=[]), \
         patch("tools.teaching.db.get_curriculum", return_value=None), \
         patch("tools.teaching.db.upsert_curriculum", return_value=mock_row), \
         patch("tools.teaching.llm.generate_curriculum_modules", return_value=_sample_modules):
        result = asyncio.run(mod.handle(_curriculum_task()))

    assert result["status"] == "completed"
    assert result["curriculum_id"] == 7
    assert result["module_count"] == 2
    assert result["topic"] == "Python"


def test_curriculum_with_existing_curriculum_passes_existing_modules():
    from workers.task_handlers import teaching_curriculum as mod

    mock_db = MagicMock()
    existing_row = {"Id": 3, "modules": json.dumps(_sample_modules[:1])}
    captured: list[dict] = []

    def fake_generate(**kwargs):
        captured.append(kwargs)
        return _sample_modules

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db), \
         patch("tools.teaching.db.get_learner_concepts", return_value=[]), \
         patch("tools.teaching.db.get_curriculum", return_value=existing_row), \
         patch("tools.teaching.db.upsert_curriculum", return_value={"Id": 3}), \
         patch("tools.teaching.llm.generate_curriculum_modules", side_effect=fake_generate):
        asyncio.run(mod.handle(_curriculum_task(curriculum_id=3)))

    assert captured[0]["existing_modules"] == _sample_modules[:1]


def test_curriculum_missing_topic_returns_failed():
    from workers.task_handlers import teaching_curriculum as mod
    result = asyncio.run(mod.handle(_curriculum_task(topic="")))
    assert result["status"] == "failed"
    assert "topic" in result["error"]


def test_curriculum_missing_org_id_returns_failed():
    from workers.task_handlers import teaching_curriculum as mod
    result = asyncio.run(mod.handle(_curriculum_task(org_id=0)))
    assert result["status"] == "failed"
    assert "org_id" in result["error"]


def test_curriculum_upsert_returns_no_id_returns_failed():
    from workers.task_handlers import teaching_curriculum as mod

    mock_db = MagicMock()
    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db), \
         patch("tools.teaching.db.get_learner_concepts", return_value=[]), \
         patch("tools.teaching.db.get_curriculum", return_value=None), \
         patch("tools.teaching.db.upsert_curriculum", return_value={"Id": 0}), \
         patch("tools.teaching.llm.generate_curriculum_modules", return_value=_sample_modules):
        result = asyncio.run(mod.handle(_curriculum_task()))

    assert result["status"] == "failed"
    assert "Id" in result["error"] or "upsert" in result["error"]


# ── teaching_revision ─────────────────────────────────────────────────────────

def _revision_task(parent_task_id=99, revision_instructions="Fix examples"):
    return {
        "Id": 20,
        "input_payload": {
            "parent_task_id": parent_task_id,
            "revision_instructions": revision_instructions,
        },
    }


def _parent_output(lesson_id=10, curriculum_id=1, module_id="m1"):
    return {
        "lesson_id": lesson_id,
        "curriculum_id": curriculum_id,
        "module_id": module_id,
        "lesson_markdown": "# Old lesson",
        "anki_cards": "",
        "session_summary": "",
    }


def test_revision_valid_payload_returns_completed():
    from workers.task_handlers import teaching_revision as mod

    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [{"Id": 99, "output_payload": json.dumps(_parent_output())}]}
    lesson_row = {"Id": 10, "lesson_markdown": "# Old", "sources": "[]"}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db), \
         patch("tools.teaching.db.get_lesson_row", return_value=lesson_row), \
         patch("tools.teaching.db.update_lesson_row"), \
         patch("tools.teaching.llm.generate_revision", return_value=("# Revised", "summary", "cards", [])), \
         patch("tools.teaching.output.write_lesson_files", return_value=("/tmp/a.md", "/tmp/b.txt")):
        result = asyncio.run(mod.handle(_revision_task()))

    assert result["status"] == "completed"
    assert result["lesson_id"] == 10
    assert result["lesson_markdown"] == "# Revised"
    assert result["revised_from_task_id"] == 99


def test_revision_missing_parent_task_id_returns_failed():
    from workers.task_handlers import teaching_revision as mod
    result = asyncio.run(mod.handle(_revision_task(parent_task_id=0)))
    assert result["status"] == "failed"
    assert "parent_task_id" in result["error"]


def test_revision_missing_instructions_returns_failed():
    from workers.task_handlers import teaching_revision as mod
    result = asyncio.run(mod.handle(_revision_task(revision_instructions="")))
    assert result["status"] == "failed"
    assert "revision_instructions" in result["error"]


def test_revision_parent_task_not_found_returns_failed():
    from workers.task_handlers import teaching_revision as mod

    mock_db = MagicMock()
    mock_db._get.return_value = {"list": []}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        result = asyncio.run(mod.handle(_revision_task()))

    assert result["status"] == "failed"
    assert "not found" in result["error"]


def test_revision_parent_has_no_output_payload_raises_task_not_ready():
    """Parent exists but output_payload is absent → TaskNotReady so kanban
    re-queues with a delay instead of burning a retry slot."""
    import pytest
    from workers.task_handlers import teaching_revision as mod
    from workers.kanban import TaskNotReady

    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [{"Id": 99, "output_payload": None}]}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        with pytest.raises(TaskNotReady):
            asyncio.run(mod.handle(_revision_task()))


def test_revision_parent_output_missing_lesson_id_returns_failed():
    from workers.task_handlers import teaching_revision as mod

    mock_db = MagicMock()
    bad_output = json.dumps({"curriculum_id": 1})  # no lesson_id
    mock_db._get.return_value = {"list": [{"Id": 99, "output_payload": bad_output}]}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        result = asyncio.run(mod.handle(_revision_task()))

    assert result["status"] == "failed"
    assert "lesson_id" in result["error"]


def test_revision_lesson_row_not_found_returns_failed():
    from workers.task_handlers import teaching_revision as mod

    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [{"Id": 99, "output_payload": json.dumps(_parent_output())}]}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db), \
         patch("tools.teaching.db.get_lesson_row", return_value=None):
        result = asyncio.run(mod.handle(_revision_task()))

    assert result["status"] == "failed"
    assert "not found" in result["error"]


# ── teaching_check ────────────────────────────────────────────────────────────

def _check_task(parent_task_id=99, concept_focus=None, difficulty="mixed", count=5):
    return {
        "Id": 30,
        "input_payload": {
            "parent_task_id": parent_task_id,
            "concept_focus": concept_focus or [],
            "difficulty": difficulty,
            "count": count,
        },
    }


_sample_checks = [
    {"question": "What is a variable?", "expected_answer": "A named value", "concept": "variables", "difficulty": "introductory"},
]


def test_check_valid_payload_returns_completed():
    from workers.task_handlers import teaching_check as mod

    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [{"Id": 99, "output_payload": json.dumps({"lesson_id": 10})}]}
    lesson_row = {"Id": 10, "lesson_markdown": "# Lesson"}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db), \
         patch("tools.teaching.db.get_lesson_row", return_value=lesson_row), \
         patch("tools.teaching.llm.generate_checks", return_value=_sample_checks), \
         patch("tools.teaching.output.write_checks_file", return_value="/tmp/checks.json"):
        result = asyncio.run(mod.handle(_check_task()))

    assert result["status"] == "completed"
    assert result["lesson_id"] == 10
    assert result["checks"] == _sample_checks


def test_check_mixed_difficulty_expands_to_all_levels():
    """'mixed' difficulty should expand to a multi-level description before hitting generate_checks."""
    from workers.task_handlers import teaching_check as mod

    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [{"Id": 99, "output_payload": json.dumps({"lesson_id": 10})}]}
    captured: list[str] = []

    def fake_generate(lesson_markdown, concept_focus, difficulty, count):
        captured.append(difficulty)
        return _sample_checks

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db), \
         patch("tools.teaching.db.get_lesson_row", return_value={"Id": 10, "lesson_markdown": "x"}), \
         patch("tools.teaching.llm.generate_checks", side_effect=fake_generate), \
         patch("tools.teaching.output.write_checks_file", return_value="/tmp/c.json"):
        asyncio.run(mod.handle(_check_task(difficulty="mixed")))

    assert captured[0] != "mixed"
    assert "introductory" in captured[0]


def test_check_missing_parent_task_id_returns_failed():
    from workers.task_handlers import teaching_check as mod
    result = asyncio.run(mod.handle(_check_task(parent_task_id=0)))
    assert result["status"] == "failed"
    assert "parent_task_id" in result["error"]


def test_check_parent_task_not_found_returns_failed():
    from workers.task_handlers import teaching_check as mod

    mock_db = MagicMock()
    mock_db._get.return_value = {"list": []}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        result = asyncio.run(mod.handle(_check_task()))

    assert result["status"] == "failed"
    assert "not found" in result["error"]


def test_check_parent_output_missing_lesson_id_returns_failed():
    from workers.task_handlers import teaching_check as mod

    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [{"Id": 99, "output_payload": json.dumps({"curriculum_id": 1})}]}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        result = asyncio.run(mod.handle(_check_task()))

    assert result["status"] == "failed"
    assert "lesson_id" in result["error"]


def test_revision_parent_not_yet_complete_raises_task_not_ready():
    """When the parent lesson task exists but has no output_payload yet
    (still running), teaching_revision must raise TaskNotReady so kanban
    re-queues it with a delay instead of burning a retry slot."""
    from workers.task_handlers import teaching_revision as mod
    from workers.kanban import TaskNotReady

    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [{"Id": 99, "output_payload": None}]}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        import pytest
        with pytest.raises(TaskNotReady):
            asyncio.run(mod.handle(_revision_task()))


def test_check_parent_not_yet_complete_raises_task_not_ready():
    """Same contract for teaching_check: empty parent output_payload raises
    TaskNotReady (re-queue with delay) instead of burning a retry slot."""
    from workers.task_handlers import teaching_check as mod
    from workers.kanban import TaskNotReady

    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [{"Id": 99, "output_payload": None}]}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db):
        import pytest
        with pytest.raises(TaskNotReady):
            asyncio.run(mod.handle(_check_task()))


def test_check_lesson_row_not_found_returns_failed():
    from workers.task_handlers import teaching_check as mod

    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [{"Id": 99, "output_payload": json.dumps({"lesson_id": 10})}]}

    with patch("infra.nocodb_client.NocodbClient", return_value=mock_db), \
         patch("tools.teaching.db.get_lesson_row", return_value=None):
        result = asyncio.run(mod.handle(_check_task()))

    assert result["status"] == "failed"
    assert "not found" in result["error"]


# ── upsert_curriculum (tools/teaching/db.py) ─────────────────────────────────

def test_upsert_curriculum_refetches_row_when_patch_returns_none():
    """Some NocoDB versions return None on a successful PATCH. upsert_curriculum
    must not return the stale pre-patch row in that case — it must re-fetch."""
    from tools.teaching import db as teaching_db

    _sample_modules = [{"id": "m1", "title": "Intro", "objectives": [], "status": "pending"}]
    fresh_row = {"Id": 3, "modules": json.dumps(_sample_modules), "current_module_index": 0}

    mock_db = MagicMock()
    # _safe_get on the existing row returns old data
    mock_db._safe_get.side_effect = [
        {"Id": 3, "modules": "[]"},  # first call: existing row lookup
        fresh_row,                   # second call: re-fetch after patch
    ]
    mock_db._patch.return_value = None  # NocoDB returned nothing

    result = teaching_db.upsert_curriculum(mock_db, org_id=1, topic="Python",
                                           root_goal=None, modules=_sample_modules,
                                           curriculum_id=3)

    assert result == fresh_row, "Must return re-fetched row, not stale pre-patch data"
