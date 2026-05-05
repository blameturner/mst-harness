import asyncio
import json
from unittest.mock import MagicMock, patch


def _make_task(revision_count=0, changed_paths=None):
    return {
        "Id": 50,
        "agent": "project:10",
        "input_payload": {
            "project_id": 10,
            "pr_id": 5,
            "branch_name": "feature/search",
            "feature_description": "Add search",
            "architect_context": "",
            "revision_count": revision_count,
            "changed_paths": changed_paths or ["src/search.py"],
            "human_feedback": "Fix the error handling in src/search.py",
        },
    }


def _mock_db(file_content="def search(): pass"):
    db = MagicMock()
    db.list_project_files.return_value = [
        {"path": "src/search.py", "content": file_content, "Id": 1},
    ]
    return db


def _mock_model_response(patches_json: str):
    mc = MagicMock()
    mc.complete_sync.return_value = MagicMock(text=patches_json)
    return mc


def test_applies_llm_patches_to_nocodb():
    from workers.task_handlers import project_revise as mod

    patches_json = json.dumps([{"path": "src/search.py", "content": "def search(): raise NotImplementedError"}])
    db = _mock_db()
    mc = _mock_model_response(patches_json)

    with patch.object(mod, "check_autonomy"), \
         patch.object(mod, "NocodbClient", return_value=db), \
         patch.object(mod, "resolve_model_entry", return_value={"model_id": "m1"}), \
         patch.object(mod, "resolve_agent_model", return_value="t2_coder"), \
         patch.object(mod, "build_model_client", return_value=mc), \
         patch.object(mod, "push_files_to_gitea", return_value=["src/search.py"]), \
         patch("workers.task_handlers.project_feature._get_gitea_client", return_value=MagicMock()), \
         patch("workers.task_handlers.project_feature._get_repo_coords", return_value=("o", "r")), \
         patch("workers.kanban.submit", return_value=1):
        result = asyncio.run(mod.handle(_make_task()))

    assert result["status"] == "done"
    db.write_project_file_version.assert_called_once()
    call_args = db.write_project_file_version.call_args
    assert "raise NotImplementedError" in call_args.args[2]  # content is args[2]


def test_re_gates_when_below_max_revisions():
    from workers.task_handlers import project_revise as mod

    patches_json = json.dumps([{"path": "src/search.py", "content": "fixed"}])
    db = _mock_db()
    mc = _mock_model_response(patches_json)

    submitted: list[tuple] = []

    def fake_submit(db, task_type, payload, **kw):
        submitted.append((task_type, kw.get("status", "ready")))
        return 1

    with patch.object(mod, "check_autonomy"), \
         patch.object(mod, "NocodbClient", return_value=db), \
         patch.object(mod, "resolve_model_entry", return_value={"model_id": "m1"}), \
         patch.object(mod, "resolve_agent_model", return_value="t2_coder"), \
         patch.object(mod, "build_model_client", return_value=mc), \
         patch.object(mod, "push_files_to_gitea", return_value=["src/search.py"]), \
         patch("workers.task_handlers.project_feature._get_gitea_client", return_value=MagicMock()), \
         patch("workers.task_handlers.project_feature._get_repo_coords", return_value=("o", "r")), \
         patch("workers.kanban.submit", side_effect=fake_submit):
        result = asyncio.run(mod.handle(_make_task(revision_count=0)))

    assert any(t == "project_human_review" and s == "blocked" for t, s in submitted)
    assert "project_review" not in [t for t, s in submitted]


def test_skips_to_review_at_max_revisions():
    from workers.task_handlers import project_revise as mod

    patches_json = json.dumps([{"path": "src/search.py", "content": "final"}])
    db = _mock_db()
    mc = _mock_model_response(patches_json)

    submitted: list[str] = []

    def fake_submit(db, task_type, payload, **kw):
        submitted.append(task_type)
        return 1

    with patch.object(mod, "check_autonomy"), \
         patch.object(mod, "NocodbClient", return_value=db), \
         patch.object(mod, "resolve_model_entry", return_value={"model_id": "m1"}), \
         patch.object(mod, "resolve_agent_model", return_value="t2_coder"), \
         patch.object(mod, "build_model_client", return_value=mc), \
         patch.object(mod, "push_files_to_gitea", return_value=["src/search.py"]), \
         patch("workers.task_handlers.project_feature._get_gitea_client", return_value=MagicMock()), \
         patch("workers.task_handlers.project_feature._get_repo_coords", return_value=("o", "r")), \
         patch("workers.kanban.submit", side_effect=fake_submit):
        result = asyncio.run(mod.handle(_make_task(revision_count=2)))

        # revision_count=2 means 2+1=3 >= _MAX_REVISE_CYCLES=2 → review

    assert "project_review" in submitted
    assert "project_human_review" not in submitted


def test_fails_when_all_changed_paths_missing_from_project_files():
    """If the project has no matching files for the changed_paths, the handler
    must fail before calling the model — not silently advance revision_count."""
    from workers.task_handlers import project_revise as mod

    db = MagicMock()
    db.list_project_files.return_value = []  # no files at all

    mc = MagicMock()

    with patch.object(mod, "check_autonomy"), \
         patch.object(mod, "NocodbClient", return_value=db), \
         patch.object(mod, "resolve_model_entry", return_value={"model_id": "m1"}), \
         patch.object(mod, "resolve_agent_model", return_value="t2_coder"), \
         patch.object(mod, "build_model_client", return_value=mc):
        result = asyncio.run(mod.handle(_make_task()))

    assert result["status"] == "failed"
    mc.complete_sync.assert_not_called()


def test_fails_cleanly_on_bad_llm_json():
    from workers.task_handlers import project_revise as mod

    mc = _mock_model_response("not json at all")
    db = _mock_db()

    with patch.object(mod, "check_autonomy"), \
         patch.object(mod, "NocodbClient", return_value=db), \
         patch.object(mod, "resolve_model_entry", return_value={"model_id": "m1"}), \
         patch.object(mod, "resolve_agent_model", return_value="t2_coder"), \
         patch.object(mod, "build_model_client", return_value=mc):
        result = asyncio.run(mod.handle(_make_task()))

    assert result["status"] == "failed"
    assert "non-JSON" in result["error"]
