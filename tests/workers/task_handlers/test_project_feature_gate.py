# tests/workers/task_handlers/test_project_feature_gate.py
"""Tests for the human-review gate added to project_feature._run."""
from unittest.mock import MagicMock, patch


def _make_task(task_id=1, model=""):
    return {"Id": task_id, "input_payload": {
        "project_id": 10,
        "feature_description": "Add search",
        "branch_name": "feature/search",
        "architect_context": "",
    }, "model": model, "agent": "project:10"}


def test_gate_disabled_when_threshold_zero():
    """threshold=0 → skip human review entirely, go direct to project_review."""
    import workers.task_handlers.project_feature as mod
    gitea = MagicMock()
    gitea.create_branch.return_value = {}
    gitea.create_pr.return_value = {"number": 5}

    submitted_types: list[str] = []

    def fake_submit(db, task_type, payload, **kw):
        submitted_types.append(task_type)
        return 1

    with patch.object(mod, "check_autonomy"), \
         patch.object(mod, "NocodbClient", return_value=MagicMock()), \
         patch.object(mod, "read_repo_summary", return_value="s"), \
         patch.object(mod, "resolve_model_entry", return_value={"model_id": "m"}), \
         patch.object(mod, "resolve_agent_model", return_value="t2_coder"), \
         patch.object(mod, "_get_gitea_client", return_value=gitea), \
         patch.object(mod, "_get_repo_coords", return_value=("o", "r")), \
         patch.object(mod, "_run_code_agent", return_value={"tokens_used": 0, "changed_paths": ["f.py"]}), \
         patch.object(mod, "push_files_to_gitea", return_value=["f.py"]), \
         patch.object(mod, "get_agent_setting", side_effect=lambda agent, key: 0 if key == "human_review_threshold_files" else None), \
         patch("workers.kanban.submit", side_effect=fake_submit):
        result = mod._run(_make_task(), _make_task()["input_payload"])

    assert result["status"] == "done"
    assert "project_human_review" not in submitted_types


def test_meets_threshold_enqueues_human_review():
    """threshold=2, 3 changed files → enqueue project_human_review blocked."""
    import workers.task_handlers.project_feature as mod
    gitea = MagicMock()
    gitea.create_branch.return_value = {}
    gitea.create_pr.return_value = {"number": 5}

    submitted: list[tuple] = []

    def fake_submit(db, task_type, payload, **kw):
        submitted.append((task_type, kw.get("status", "ready")))
        return 1

    with patch.object(mod, "check_autonomy"), \
         patch.object(mod, "NocodbClient", return_value=MagicMock()), \
         patch.object(mod, "read_repo_summary", return_value="s"), \
         patch.object(mod, "resolve_model_entry", return_value={"model_id": "m"}), \
         patch.object(mod, "resolve_agent_model", return_value="t2_coder"), \
         patch.object(mod, "_get_gitea_client", return_value=gitea), \
         patch.object(mod, "_get_repo_coords", return_value=("o", "r")), \
         patch.object(mod, "_run_code_agent", return_value={"tokens_used": 0, "changed_paths": ["a.py", "b.py", "c.py"]}), \
         patch.object(mod, "push_files_to_gitea", return_value=["a.py", "b.py", "c.py"]), \
         patch.object(mod, "get_agent_setting", side_effect=lambda agent, key: 2 if key == "human_review_threshold_files" else None), \
         patch("workers.kanban.submit", side_effect=fake_submit):
        result = mod._run(_make_task(), _make_task()["input_payload"])

    assert result["status"] == "done"
    assert any(t == "project_human_review" and s == "blocked" for t, s in submitted)
    assert "project_review" not in [t for t, s in submitted]


def test_below_threshold_enqueues_project_review():
    """threshold=5, 2 changed files → project_review directly."""
    import workers.task_handlers.project_feature as mod
    gitea = MagicMock()
    gitea.create_branch.return_value = {}
    gitea.create_pr.return_value = {"number": 5}

    submitted_types: list[str] = []

    def fake_submit(db, task_type, payload, **kw):
        submitted_types.append(task_type)
        return 1

    with patch.object(mod, "check_autonomy"), \
         patch.object(mod, "NocodbClient", return_value=MagicMock()), \
         patch.object(mod, "read_repo_summary", return_value="s"), \
         patch.object(mod, "resolve_model_entry", return_value={"model_id": "m"}), \
         patch.object(mod, "resolve_agent_model", return_value="t2_coder"), \
         patch.object(mod, "_get_gitea_client", return_value=gitea), \
         patch.object(mod, "_get_repo_coords", return_value=("o", "r")), \
         patch.object(mod, "_run_code_agent", return_value={"tokens_used": 0, "changed_paths": ["a.py", "b.py"]}), \
         patch.object(mod, "push_files_to_gitea", return_value=["a.py", "b.py"]), \
         patch.object(mod, "get_agent_setting", side_effect=lambda agent, key: None if key == "human_review_threshold_files" else None), \
         patch("workers.kanban.submit", side_effect=fake_submit):
        result = mod._run(_make_task(), _make_task()["input_payload"])

    assert result["status"] == "done"
    assert "project_review" in submitted_types
    assert "project_human_review" not in submitted_types
