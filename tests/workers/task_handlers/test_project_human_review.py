import asyncio
from unittest.mock import MagicMock, patch


def _make_task(human_feedback=None, pr_id=5, revision_count=0):
    return {
        "Id": 99,
        "agent": "project:10",
        "input_payload": {
            "project_id": 10,
            "pr_id": pr_id,
            "branch_name": "feature/search",
            "feature_description": "Add search",
            "architect_context": "",
            "revision_count": revision_count,
            "changed_paths": ["a.py", "b.py"],
            "human_feedback": human_feedback,
        },
    }


def test_no_feedback_enqueues_project_review():
    """Approved without feedback → project_review ready."""
    from workers.task_handlers import project_human_review as mod
    submitted: list[tuple] = []

    def fake_submit(db, task_type, payload, **kw):
        submitted.append((task_type, kw.get("status", "ready")))
        return 1

    with patch("workers.project_autonomy.check_autonomy"), \
         patch("infra.nocodb_client.NocodbClient", return_value=MagicMock()), \
         patch("workers.kanban.submit", side_effect=fake_submit):
        result = asyncio.run(mod.handle(_make_task(human_feedback=None)))

    assert result["status"] == "done"
    assert result["action"] == "review"
    assert any(t == "project_review" for t, _ in submitted)
    assert all(t != "project_revise" for t, _ in submitted)


def test_with_feedback_enqueues_project_revise():
    """Feedback present → project_revise ready."""
    from workers.task_handlers import project_human_review as mod
    submitted: list[tuple] = []

    def fake_submit(db, task_type, payload, **kw):
        submitted.append((task_type, payload))
        return 1

    with patch("workers.project_autonomy.check_autonomy"), \
         patch("infra.nocodb_client.NocodbClient", return_value=MagicMock()), \
         patch("workers.kanban.submit", side_effect=fake_submit):
        result = asyncio.run(mod.handle(_make_task(human_feedback="Fix the logging in a.py")))

    assert result["status"] == "done"
    assert result["action"] == "revise"
    assert any(t == "project_revise" for t, _ in submitted)
    revise_payload = next(p for t, p in submitted if t == "project_revise")
    assert revise_payload["human_feedback"] == "Fix the logging in a.py"


def test_missing_project_id_fails():
    from workers.task_handlers import project_human_review as mod
    task = {"Id": 1, "input_payload": {}}
    result = asyncio.run(mod.handle(task))
    assert result["status"] == "failed"
    assert "project_id" in result["error"]
