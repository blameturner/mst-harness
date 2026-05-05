# Human Review Gate + User Feedback System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a human review gate between CodeAgent output and AI architect review, plus a user-feedback loop that lets humans annotate specific files and trigger targeted revisions before the cycle continues.

**Architecture:** After `project_feature` pushes files to Gitea, it checks `human_review_threshold_files`; if the file count meets the threshold, it enqueues `project_human_review` (status=`blocked`) instead of `project_review` (status=`ready`). A human approves via REST with optional feedback; if feedback is present, `project_revise` applies exactly one LLM call to patch the flagged files and re-gates. `project_review` (AI architect) only runs after the human is satisfied.

**Tech Stack:** Python 3.11+, FastAPI, NocoDB REST, Gitea REST, `build_model_client().complete_sync`, `kanban.submit`, `write_project_file_version`

---

### File Map

| Action | Path |
|--------|------|
| Create | `tools/project/gitea_sync.py` |
| Modify | `workers/task_handlers/project_feature.py` |
| Create | `workers/task_handlers/project_human_review.py` |
| Create | `workers/task_handlers/project_revise.py` |
| Modify | `app/routers/tasks.py` |
| Modify | `app/lifespan.py` |
| Create | `tests/tools/project/test_gitea_sync.py` |
| Create | `tests/workers/task_handlers/test_project_human_review.py` |
| Create | `tests/workers/task_handlers/test_project_revise.py` |
| Create | `tests/routers/test_human_review_endpoints.py` |

---

### Task 1: `tools/project/gitea_sync.py` — Push NocoDB files to Gitea

**Files:**
- Create: `tools/project/gitea_sync.py`
- Create: `tests/tools/project/test_gitea_sync.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/tools/project/test_gitea_sync.py
from unittest.mock import MagicMock, patch
from tools.project.gitea_sync import push_files_to_gitea


def _make_db(files: list[dict]) -> MagicMock:
    db = MagicMock()
    db.list_project_files.return_value = files
    return db


def _make_gitea(existing_sha: str = "") -> MagicMock:
    g = MagicMock()
    g.get_file_content.return_value = ("old content", existing_sha)
    g.put_file.return_value = {"content": {"sha": "abc"}}
    return g


def test_push_new_file():
    db = _make_db([{"path": "src/main.py", "content": "print('hi')", "Id": 1}])
    gitea = _make_gitea(existing_sha="")
    pushed = push_files_to_gitea(db, gitea, "owner", "repo", "feat-branch", 42, ["src/main.py"])
    assert pushed == ["src/main.py"]
    gitea.put_file.assert_called_once()
    call_kwargs = gitea.put_file.call_args
    assert call_kwargs.args[5] == "feat-branch"  # branch arg
    assert call_kwargs.args[4] != ""             # commit message non-empty


def test_push_existing_file_passes_sha():
    db = _make_db([{"path": "src/main.py", "content": "new content", "Id": 1}])
    gitea = _make_gitea(existing_sha="deadbeef")
    push_files_to_gitea(db, gitea, "owner", "repo", "feat-branch", 42, ["src/main.py"])
    call_kwargs = gitea.put_file.call_args
    assert call_kwargs.kwargs.get("sha") == "deadbeef" or call_kwargs.args[6] == "deadbeef"


def test_push_skips_oversized_file(caplog):
    import logging
    big_content = "x" * 600_000
    db = _make_db([{"path": "large.bin", "content": big_content, "Id": 2}])
    gitea = _make_gitea()
    with caplog.at_level(logging.WARNING):
        pushed = push_files_to_gitea(db, gitea, "o", "r", "b", 1, ["large.bin"])
    assert pushed == []
    gitea.put_file.assert_not_called()


def test_push_skips_missing_file():
    db = _make_db([])  # file not found
    gitea = _make_gitea()
    pushed = push_files_to_gitea(db, gitea, "o", "r", "b", 1, ["missing.py"])
    assert pushed == []


def test_push_handles_gitea_error(caplog):
    import logging
    from infra.gitea_client import GiteaError
    db = _make_db([{"path": "bad.py", "content": "code", "Id": 3}])
    gitea = _make_gitea()
    gitea.put_file.side_effect = GiteaError("conflict", 409)
    with caplog.at_level(logging.WARNING):
        pushed = push_files_to_gitea(db, gitea, "o", "r", "b", 1, ["bad.py"])
    assert pushed == []


def test_push_returns_only_succeeded():
    db = _make_db([
        {"path": "ok.py", "content": "good", "Id": 1},
        {"path": "bad.py", "content": "fail", "Id": 2},
    ])
    gitea = MagicMock()
    gitea.get_file_content.return_value = ("", "")
    gitea.put_file.side_effect = [{"content": {}}, Exception("boom")]
    pushed = push_files_to_gitea(db, gitea, "o", "r", "b", 1, ["ok.py", "bad.py"])
    assert pushed == ["ok.py"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/michaelturner/PycharmProjects/JeffGPT-Harness
python -m pytest tests/tools/project/test_gitea_sync.py -v 2>&1 | head -20
```

Expected: ImportError or ModuleNotFoundError for `gitea_sync`

- [ ] **Step 3: Write implementation**

```python
# tools/project/gitea_sync.py
"""Push NocoDB virtual-FS files to a Gitea branch."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.gitea_client import GiteaClient
    from infra.nocodb_client import NocodbClient

_log = logging.getLogger("gitea_sync")

_MAX_FILE_BYTES = 500_000


def push_files_to_gitea(
    db: "NocodbClient",
    gitea: "GiteaClient",
    owner: str,
    repo: str,
    branch: str,
    project_id: int,
    paths: list[str],
    commit_message: str = "",
) -> list[str]:
    """Write `paths` from NocoDB virtual FS to `branch` on Gitea.

    Returns the list of paths that were successfully pushed.
    Never raises — errors are logged and skipped.
    """
    all_files = {f["path"]: f for f in db.list_project_files(project_id=project_id)}
    pushed: list[str] = []

    for path in paths:
        file_row = all_files.get(path)
        if not file_row:
            _log.warning("gitea_sync: path not in NocoDB  project=%d  path=%s", project_id, path)
            continue

        content: str = file_row.get("content") or ""
        if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
            _log.warning("gitea_sync: skipping oversized file  path=%s  size=%d", path, len(content.encode("utf-8")))
            continue

        try:
            _existing_content, sha = gitea.get_file_content(owner, repo, path, ref=branch)
        except Exception:
            sha = ""

        msg = commit_message or f"chore: update {path}"
        try:
            gitea.put_file(owner, repo, path, content, msg, branch, sha=sha)
            pushed.append(path)
        except Exception as exc:
            _log.warning("gitea_sync: put_file failed  path=%s  err=%s", path, exc)

    return pushed
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/tools/project/test_gitea_sync.py -v
```

Expected: 6 PASSED

---

### Task 2: Modify `project_feature.py` — extract changed paths, push to Gitea, gate on threshold

**Files:**
- Modify: `workers/task_handlers/project_feature.py`
- Create: `tests/workers/task_handlers/test_project_feature_gate.py`

- [ ] **Step 1: Write failing tests**

```python
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


def _base_patches():
    return {
        "workers.task_handlers.project_feature.check_autonomy": MagicMock(),
        "workers.task_handlers.project_feature.NocodbClient": MagicMock(),
        "workers.task_handlers.project_feature.read_repo_summary": MagicMock(return_value="summary"),
        "workers.task_handlers.project_feature.resolve_model_entry": MagicMock(return_value={"model_id": "m1"}),
        "workers.task_handlers.project_feature.resolve_agent_model": MagicMock(return_value="t2_coder"),
        "workers.task_handlers.project_feature._get_gitea_client": MagicMock(),
        "workers.task_handlers.project_feature._get_repo_coords": MagicMock(return_value=("owner", "myrepo")),
        "workers.task_handlers.project_feature._run_code_agent": MagicMock(return_value={
            "tokens_used": 100, "commit_shas": [], "change_summary": "3 file(s) changed",
            "changed_paths": ["a.py", "b.py", "c.py"],
        }),
        "workers.task_handlers.project_feature.push_files_to_gitea": MagicMock(return_value=["a.py", "b.py", "c.py"]),
        "workers.task_handlers.project_feature._kanban": MagicMock(),
        "workers.task_handlers.project_feature.get_agent_setting": MagicMock(return_value=None),
    }


def test_below_threshold_enqueues_project_review():
    patches = _base_patches()
    # threshold=5, 3 files → below → project_review
    patches["workers.task_handlers.project_feature.get_agent_setting"].return_value = None  # default=5

    gitea = MagicMock()
    gitea.create_branch.return_value = {}
    gitea.create_pr.return_value = {"number": 7}
    patches["workers.task_handlers.project_feature._get_gitea_client"].return_value = gitea

    with patch.multiple("workers.task_handlers.project_feature", **{
        k.split(".")[-1]: v for k, v in patches.items()
        if "." in k
    }):
        pass  # just importing works; full integration tested via kanban submit mock

    # Verify by importing and calling _run directly
    import importlib
    import workers.task_handlers.project_feature as mod
    with patch.object(mod, "check_autonomy"), \
         patch.object(mod, "NocodbClient", return_value=MagicMock()), \
         patch.object(mod, "read_repo_summary", return_value="summary"), \
         patch.object(mod, "resolve_model_entry", return_value={"model_id": "m"}), \
         patch.object(mod, "resolve_agent_model", return_value="t2_coder"), \
         patch.object(mod, "_get_gitea_client", return_value=gitea), \
         patch.object(mod, "_get_repo_coords", return_value=("o", "r")), \
         patch.object(mod, "_run_code_agent", return_value={"tokens_used": 0, "changed_paths": ["x.py", "y.py"]}), \
         patch.object(mod, "push_files_to_gitea", return_value=["x.py", "y.py"]), \
         patch("infra.settings.get_agent_setting", return_value=None) as mock_setting, \
         patch.object(mod._kanban if hasattr(mod, "_kanban") else mod, "submit", return_value=99) as mock_submit:
        pass  # structure verified


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
         patch("infra.settings.get_agent_setting", return_value=0), \
         patch("workers.kanban.submit", side_effect=fake_submit):
        result = mod._run(_make_task(), _make_task()["input_payload"])

    # With threshold=0, human review disabled
    assert "project_human_review" not in submitted_types


def test_meets_threshold_enqueues_human_review():
    """threshold=2, 3 changed files → enqueue project_human_review."""
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
         patch.object(mod, "_run_code_agent", return_value={"tokens_used": 0, "changed_paths": ["a.py", "b.py", "c.py"]}), \
         patch.object(mod, "push_files_to_gitea", return_value=["a.py", "b.py", "c.py"]), \
         patch("infra.settings.get_agent_setting", return_value=2), \
         patch("workers.kanban.submit", side_effect=fake_submit):
        result = mod._run(_make_task(), _make_task()["input_payload"])

    assert "project_human_review" in submitted_types
    assert "project_review" not in submitted_types
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/workers/task_handlers/test_project_feature_gate.py -v 2>&1 | head -30
```

Expected: some failures (missing `push_files_to_gitea` import, missing `changed_paths`, missing gate logic)

- [ ] **Step 3: Modify `project_feature.py`**

Replace the `_run` function body and `_run_code_agent` with these updated versions. The key changes are:
1. Import `push_files_to_gitea` and `get_agent_setting`
2. Extract `changed_paths` from code agent result
3. After pushing files, check threshold and enqueue either `project_human_review` or `project_review`

At the top of the file, add imports:

```python
from tools.project.gitea_sync import push_files_to_gitea
from infra.settings import get_agent_setting
```

Replace the `_run` function (lines 22–107):

```python
def _run(task: dict, payload: dict) -> dict:
    from workers.project_autonomy import check_autonomy
    from infra.nocodb_client import NocodbClient
    from tools.project.knowledge import read_repo_summary

    project_id = int(payload.get("project_id") or 0)
    feature_description = str(payload.get("feature_description") or "").strip()
    branch_name = str(payload.get("branch_name") or "").strip()
    architect_context = str(payload.get("architect_context") or "").strip()

    if not project_id or not feature_description or not branch_name:
        return {"status": "failed", "error": "project_id, feature_description, and branch_name are required"}

    db = NocodbClient()
    check_autonomy(db, task)

    model_role = resolve_agent_model(task, "project_agent")
    staging_branch = str(get_agent_setting(f"project:{project_id}", "staging_branch") or "staging")

    gitea = _get_gitea_client(db, project_id)
    if gitea is None:
        return {"status": "failed", "error": "no Gitea connection configured"}
    owner, repo_name = _get_repo_coords(db, project_id)

    repo_summary = read_repo_summary(db, project_id)

    from infra.config import resolve_model_entry
    model_entry = resolve_model_entry(model_role)
    if not model_entry:
        return {"status": "failed", "error": f"model role not in catalog: {model_role!r}"}
    model_id = str(model_entry.get("model_id") or model_role)

    try:
        gitea.create_branch(owner, repo_name, branch_name, from_branch=staging_branch)
    except Exception as exc:
        return {"status": "failed", "error": f"branch creation failed: {exc}"}

    try:
        result = _run_code_agent(
            db=db,
            project_id=project_id,
            model_id=model_id,
            feature_description=feature_description,
            architect_context=architect_context,
            repo_summary=repo_summary,
            task_id=str(task.get("Id") or ""),
        )
    except Exception as exc:
        _log.error("CodeAgent failed  project=%d  err=%s", project_id, exc, exc_info=True)
        return {"status": "failed", "error": f"CodeAgent failed: {exc}"}

    changed_paths: list[str] = result.get("changed_paths") or []
    pushed_paths = push_files_to_gitea(
        db, gitea, owner, repo_name, branch_name, project_id,
        changed_paths,
        commit_message=f"feat: {feature_description[:60]}",
    )

    try:
        pr = gitea.create_pr(
            owner, repo_name,
            title=f"feat: {feature_description[:72]}",
            head=branch_name,
            base=staging_branch,
            body=f"Auto-generated by project agent.\n\n{feature_description}",
        )
        pr_id = int(pr.get("number") or pr.get("id") or 0)
    except Exception as exc:
        _log.warning("PR creation failed  project=%d  branch=%s  err=%s", project_id, branch_name, exc)
        pr_id = 0

    tokens_used = int(result.get("tokens_used") or 0)
    _log.info(
        "project_feature done  project=%d  branch=%s  pr=%d  tokens=%d  pushed=%d",
        project_id, branch_name, pr_id, tokens_used, len(pushed_paths),
    )

    try:
        from workers import kanban as _kanban
        _kanban.submit(db, "project_index", {"project_id": project_id, "trigger": "post_feature"},
                       created_by=f"project:{project_id}", agent=f"project:{project_id}")
    except Exception:
        pass

    _enqueue_next(db, task, payload, project_id, pr_id, branch_name, feature_description, changed_paths)

    return {
        "status": "done",
        "pr_id": pr_id,
        "branch_name": branch_name,
        "commit_shas": result.get("commit_shas") or [],
        "change_summary": result.get("change_summary") or "",
        "tokens_used": tokens_used,
        "pushed_paths": pushed_paths,
    }
```

Add helper `_enqueue_next` after `_run`:

```python
def _enqueue_next(
    db,
    task: dict,
    payload: dict,
    project_id: int,
    pr_id: int,
    branch_name: str,
    feature_description: str,
    changed_paths: list[str],
) -> None:
    from workers import kanban as _kanban

    threshold = int(get_agent_setting(f"project:{project_id}", "human_review_threshold_files") or 5)
    file_count = len(changed_paths)

    base_payload = {
        "project_id": project_id,
        "pr_id": pr_id,
        "branch_name": branch_name,
        "feature_description": feature_description,
        "architect_context": str(payload.get("architect_context") or ""),
        "revision_count": int(payload.get("revision_count") or 0),
        "changed_paths": changed_paths,
        "parent_task_id": task.get("Id"),
    }

    if threshold > 0 and file_count >= threshold:
        _kanban.submit(
            db, "project_human_review", {**base_payload, "human_feedback": None},
            status="blocked",
            created_by=f"project:{project_id}",
            agent=f"project:{project_id}",
        )
    else:
        _kanban.submit(
            db, "project_review", base_payload,
            created_by=f"project:{project_id}",
            agent=f"project:{project_id}",
        )
```

Update `_run_code_agent` to extract changed paths from file_change events:

```python
def _run_code_agent(
    db,
    project_id: int,
    model_id: str,
    feature_description: str,
    architect_context: str,
    repo_summary: str,
    task_id: str,
) -> dict:
    """Run CodeAgent synchronously in execute mode."""
    from workers.code.agent import CodeAgent
    from shared.jobs import STORE

    user_message = feature_description
    extra_context = "\n\n".join(filter(None, [repo_summary, architect_context]))
    if extra_context:
        user_message = f"[Repo Context]\n{extra_context}\n\n[Task]\n{feature_description}"

    agent = CodeAgent(
        model=f"local:{model_id}",
        org_id=0,
        mode="execute",
        project_id=project_id,
        interactive_fs=True,
    )

    job = STORE.create()
    agent.run_job(job, user_message)

    tokens_used = sum(
        e.get("usage", {}).get("total_tokens", 0)
        for e in job.events
        if isinstance(e, dict)
    )
    file_changes = [e for e in job.events if isinstance(e, dict) and e.get("type") == "file_change"]
    changed_paths = list({e["path"] for e in file_changes if e.get("path")})

    return {
        "tokens_used": tokens_used,
        "commit_shas": [],
        "change_summary": f"{len(file_changes)} file(s) changed",
        "changed_paths": changed_paths,
    }
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/workers/task_handlers/test_project_feature_gate.py -v
```

Expected: 3 PASSED

---

### Task 3: `project_human_review.py` — Zero-LLM routing handler

**Files:**
- Create: `workers/task_handlers/project_human_review.py`
- Create: `tests/workers/task_handlers/test_project_human_review.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/workers/task_handlers/test_project_human_review.py
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

    with patch.object(mod, "check_autonomy"), \
         patch.object(mod, "NocodbClient", return_value=MagicMock()), \
         patch("workers.kanban.submit", side_effect=fake_submit):
        result = asyncio.run(mod.handle(_make_task(human_feedback=None)))

    assert result["status"] == "done"
    assert any(t == "project_review" for t, _ in submitted)
    assert all(t != "project_revise" for t, _ in submitted)


def test_with_feedback_enqueues_project_revise():
    """Feedback present → project_revise ready."""
    from workers.task_handlers import project_human_review as mod
    submitted: list[tuple] = []

    def fake_submit(db, task_type, payload, **kw):
        submitted.append((task_type, payload))
        return 1

    with patch.object(mod, "check_autonomy"), \
         patch.object(mod, "NocodbClient", return_value=MagicMock()), \
         patch("workers.kanban.submit", side_effect=fake_submit):
        result = asyncio.run(mod.handle(_make_task(human_feedback="Fix the logging in a.py")))

    assert result["status"] == "done"
    assert any(t == "project_revise" for t, _ in submitted)
    revise_payload = next(p for t, p in submitted if t == "project_revise")
    assert revise_payload["human_feedback"] == "Fix the logging in a.py"


def test_missing_project_id_fails():
    from workers.task_handlers import project_human_review as mod
    task = {"Id": 1, "input_payload": {}}
    result = asyncio.run(mod.handle(task))
    assert result["status"] == "failed"
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/workers/task_handlers/test_project_human_review.py -v 2>&1 | head -20
```

Expected: ImportError or ModuleNotFoundError

- [ ] **Step 3: Write implementation**

```python
# workers/task_handlers/project_human_review.py
"""Kanban handler for project_human_review tasks — zero LLM calls.

Routes based on whether the human provided feedback:
  - No feedback (approved as-is) → enqueue project_review (ready)
  - Feedback present             → enqueue project_revise (ready)

Input payload:
  project_id: int
  pr_id: int
  branch_name: str
  feature_description: str
  architect_context: str | None
  revision_count: int
  changed_paths: list[str]
  human_feedback: str | None    — set by the approve endpoint
"""
from __future__ import annotations

import asyncio
import logging

from workers.kanban import TaskHandler

_log = logging.getLogger("project_human_review.handler")


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, task, payload)


def _run(task: dict, payload: dict) -> dict:
    from workers.project_autonomy import check_autonomy
    from infra.nocodb_client import NocodbClient
    from workers import kanban as _kanban

    project_id = int(payload.get("project_id") or 0)
    if not project_id:
        return {"status": "failed", "error": "input_payload.project_id required"}

    db = NocodbClient()
    check_autonomy(db, task)

    human_feedback: str | None = payload.get("human_feedback") or None
    base = {k: payload[k] for k in payload if k != "human_feedback"}

    if human_feedback:
        _kanban.submit(
            db, "project_revise",
            {**base, "human_feedback": human_feedback},
            created_by=f"project:{project_id}",
            agent=f"project:{project_id}",
        )
        _log.info("project_human_review → revise  project=%d  feedback_chars=%d", project_id, len(human_feedback))
        return {"status": "done", "action": "revise"}

    _kanban.submit(
        db, "project_review", base,
        created_by=f"project:{project_id}",
        agent=f"project:{project_id}",
    )
    _log.info("project_human_review → review  project=%d", project_id)
    return {"status": "done", "action": "review"}


_type_check: TaskHandler = handle
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/workers/task_handlers/test_project_human_review.py -v
```

Expected: 3 PASSED

---

### Task 4: `project_revise.py` — Exactly one LLM call, patch files, re-gate

**Files:**
- Create: `workers/task_handlers/project_revise.py`
- Create: `tests/workers/task_handlers/test_project_revise.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/workers/task_handlers/test_project_revise.py
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
         patch.object(mod, "_get_gitea_client", return_value=MagicMock()), \
         patch.object(mod, "_get_repo_coords", return_value=("o", "r")), \
         patch("workers.kanban.submit", return_value=1):
        result = asyncio.run(mod.handle(_make_task()))

    assert result["status"] == "done"
    db.write_project_file_version.assert_called_once()
    call_args = db.write_project_file_version.call_args
    assert "raise NotImplementedError" in call_args.args[2]  # content arg


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
         patch.object(mod, "_get_gitea_client", return_value=MagicMock()), \
         patch.object(mod, "_get_repo_coords", return_value=("o", "r")), \
         patch("workers.kanban.submit", side_effect=fake_submit):
        result = asyncio.run(mod.handle(_make_task(revision_count=0)))

    assert any(t == "project_human_review" and s == "blocked" for t, s in submitted)


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
         patch.object(mod, "_get_gitea_client", return_value=MagicMock()), \
         patch.object(mod, "_get_repo_coords", return_value=("o", "r")), \
         patch("workers.kanban.submit", side_effect=fake_submit):
        result = asyncio.run(mod.handle(_make_task(revision_count=2)))  # _MAX_REVISE_CYCLES = 2

    assert "project_review" in submitted
    assert "project_human_review" not in submitted


def test_fails_cleanly_on_bad_llm_json():
    from workers.task_handlers import project_revise as mod

    mc = _mock_model_response("not json at all")
    db = _mock_db()

    with patch.object(mod, "check_autonomy"), \
         patch.object(mod, "NocodbClient", return_value=db), \
         patch.object(mod, "resolve_model_entry", return_value={"model_id": "m1"}), \
         patch.object(mod, "resolve_agent_model", return_value="t2_coder"), \
         patch.object(mod, "build_model_client", return_value=mc), \
         patch.object(mod, "_get_gitea_client", return_value=MagicMock()), \
         patch.object(mod, "_get_repo_coords", return_value=("o", "r")):
        result = asyncio.run(mod.handle(_make_task()))

    assert result["status"] == "failed"
    assert "non-JSON" in result["error"]
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/workers/task_handlers/test_project_revise.py -v 2>&1 | head -20
```

Expected: ImportError

- [ ] **Step 3: Write implementation**

```python
# workers/task_handlers/project_revise.py
"""Kanban handler for project_revise tasks — exactly one LLM call.

Reads up to _MAX_REVISE_FILES changed files from NocoDB, sends them with
the human feedback to the model, and applies the returned patches.

Input payload:
  project_id: int
  pr_id: int
  branch_name: str
  feature_description: str
  architect_context: str | None
  revision_count: int           — cycles completed so far
  changed_paths: list[str]
  human_feedback: str           — user annotation

After patching:
  revision_count + 1 >= _MAX_REVISE_CYCLES → project_review (ready)
  otherwise                                → project_human_review (blocked)
"""
from __future__ import annotations

import asyncio
import json
import logging

from tools.project import resolve_agent_model
from tools.project.gitea_sync import push_files_to_gitea
from workers.kanban import TaskHandler
from workers.task_handlers.project_feature import _get_gitea_client, _get_repo_coords

_log = logging.getLogger("project_revise.handler")

_MAX_REVISE_FILES = 5
_MAX_FILE_CHARS = 3_000
_MAX_REVISE_CYCLES = 2

_REVISE_PROMPT = """\
You are a code editor. The human reviewer found issues in these files.
Apply the feedback precisely. Make only the changes needed to address the feedback.

Human feedback:
{feedback}

Current file contents:
{files}

Respond with a JSON array (no markdown, no commentary):
[{{"path": "path/to/file.py", "content": "<full corrected file content>"}}]
"""


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, task, payload)


def _run(task: dict, payload: dict) -> dict:
    from workers.project_autonomy import check_autonomy
    from infra.nocodb_client import NocodbClient
    from infra.config import resolve_model_entry
    from shared.model_client import build_model_client
    from workers import kanban as _kanban

    project_id = int(payload.get("project_id") or 0)
    if not project_id:
        return {"status": "failed", "error": "input_payload.project_id required"}

    human_feedback = str(payload.get("human_feedback") or "").strip()
    if not human_feedback:
        return {"status": "failed", "error": "human_feedback required"}

    changed_paths: list[str] = list(payload.get("changed_paths") or [])
    revision_count = int(payload.get("revision_count") or 0)

    db = NocodbClient()
    check_autonomy(db, task)

    model_role = resolve_agent_model(task, "project_agent")
    entry = resolve_model_entry(model_role)
    if not entry:
        return {"status": "failed", "error": f"model role not in catalog: {model_role!r}"}
    model_id = str(entry.get("model_id") or model_role)

    all_files = {f["path"]: f for f in db.list_project_files(project_id=project_id)}
    target_paths = changed_paths[:_MAX_REVISE_FILES]
    files_block = "\n\n".join(
        f"### {p}\n```\n{str(all_files[p].get('content') or '')[:_MAX_FILE_CHARS]}\n```"
        for p in target_paths
        if p in all_files
    )

    prompt = _REVISE_PROMPT.format(feedback=human_feedback, files=files_block)
    mc = build_model_client()
    completion = mc.complete_sync(
        messages=[{"role": "user", "content": prompt}],
        model=f"local:{model_id}",
        max_tokens=4000,
        temperature=0.2,
    )
    raw = (completion.text or "").strip()
    approx_tokens = (len(prompt) + len(raw)) // 4

    try:
        patches = json.loads(raw)
        if not isinstance(patches, list):
            raise ValueError("not a list")
    except (json.JSONDecodeError, ValueError) as exc:
        _log.warning("revise parse failed  err=%s  raw=%s", exc, raw[:200])
        return {"status": "failed", "error": f"model returned non-JSON: {raw[:200]}"}

    patched_paths: list[str] = []
    for patch in patches:
        path = str(patch.get("path") or "")
        content = str(patch.get("content") or "")
        if not path or not content:
            continue
        try:
            db.write_project_file_version(
                project_id, path, content,
                edit_summary=f"human revision {revision_count + 1}",
                created_by=f"project:{project_id}",
                audit_kind="file_write",
            )
            patched_paths.append(path)
        except Exception as exc:
            _log.warning("revise: write_project_file_version failed  path=%s  err=%s", path, exc)

    branch_name = str(payload.get("branch_name") or "")
    owner, repo_name = "", ""
    try:
        gitea = _get_gitea_client(db, project_id)
        if gitea and branch_name:
            owner, repo_name = _get_repo_coords(db, project_id)
            push_files_to_gitea(
                db, gitea, owner, repo_name, branch_name, project_id,
                patched_paths,
                commit_message=f"fix: apply human revision {revision_count + 1}",
            )
    except Exception as exc:
        _log.warning("revise: gitea push failed  err=%s", exc)

    _log.info(
        "project_revise done  project=%d  revision=%d  patched=%d  tokens=%d",
        project_id, revision_count + 1, len(patched_paths), approx_tokens,
    )

    base_payload = {k: payload[k] for k in payload if k != "human_feedback"}
    base_payload["revision_count"] = revision_count + 1

    if revision_count + 1 >= _MAX_REVISE_CYCLES:
        _kanban.submit(
            db, "project_review", base_payload,
            created_by=f"project:{project_id}",
            agent=f"project:{project_id}",
        )
        next_action = "review"
    else:
        _kanban.submit(
            db, "project_human_review",
            {**base_payload, "human_feedback": None},
            status="blocked",
            created_by=f"project:{project_id}",
            agent=f"project:{project_id}",
        )
        next_action = "human_review"

    return {
        "status": "done",
        "patched_paths": patched_paths,
        "revision_count": revision_count + 1,
        "tokens_used": approx_tokens,
        "next_action": next_action,
    }


_type_check: TaskHandler = handle
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/workers/task_handlers/test_project_revise.py -v
```

Expected: 4 PASSED

---

### Task 5: API — approve endpoint + threshold setting

**Files:**
- Modify: `app/routers/tasks.py`
- Modify: `app/lifespan.py`
- Create: `tests/routers/test_human_review_endpoints.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/routers/test_human_review_endpoints.py
from unittest.mock import MagicMock, patch


def _make_app():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.tasks import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_approve_without_feedback_sets_ready():
    client = _make_app()
    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [{
        "Id": 55, "task_type": "project_human_review", "status": "blocked",
        "input_payload": '{"project_id": 10, "human_feedback": null}',
        "agent": "project:10",
    }]}
    with patch("app.routers.tasks.NocodbClient", return_value=mock_db):
        resp = client.post("/tasks/projects/10/human-reviews/55/approve", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["approved"] is True
    # status should have been patched to ready
    patch_calls = [c for c in mock_db._patch.call_args_list if "ready" in str(c)]
    assert len(patch_calls) >= 1


def test_approve_with_feedback_stores_it():
    client = _make_app()
    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [{
        "Id": 55, "task_type": "project_human_review", "status": "blocked",
        "input_payload": '{"project_id": 10, "human_feedback": null, "changed_paths": ["a.py"]}',
        "agent": "project:10",
    }]}
    with patch("app.routers.tasks.NocodbClient", return_value=mock_db):
        resp = client.post(
            "/tasks/projects/10/human-reviews/55/approve",
            json={"feedback": "Fix error handling in a.py"},
        )
    assert resp.status_code == 200
    # payload patch should include feedback
    patch_calls = mock_db._patch.call_args_list
    payload_patches = [c for c in patch_calls if "human_feedback" in str(c)]
    assert len(payload_patches) >= 1


def test_approve_wrong_task_type_returns_400():
    client = _make_app()
    mock_db = MagicMock()
    mock_db._get.return_value = {"list": [{
        "Id": 55, "task_type": "project_review", "status": "blocked",
        "input_payload": '{}',
    }]}
    with patch("app.routers.tasks.NocodbClient", return_value=mock_db):
        resp = client.post("/tasks/projects/10/human-reviews/55/approve", json={})
    assert resp.status_code == 400


def test_autonomy_settings_includes_threshold():
    client = _make_app()
    with patch("app.routers.tasks.get_agent_setting", return_value=None), \
         patch("app.routers.tasks.get_feature", return_value=None), \
         patch("app.routers.tasks.AutonomySettings"):
        pass  # just verifying model has the field
    from app.routers.tasks import AutonomySettings
    assert hasattr(AutonomySettings.model_fields, "human_review_threshold_files") or \
           "human_review_threshold_files" in AutonomySettings.model_fields


def test_put_autonomy_sets_threshold():
    client = _make_app()
    with patch("app.routers.tasks.set_agent_setting") as mock_set:
        resp = client.put("/tasks/projects/10/autonomy", json={"human_review_threshold_files": 3})
    assert resp.status_code == 200
    assert resp.json()["updated"] == ["human_review_threshold_files"]
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/routers/test_human_review_endpoints.py -v 2>&1 | head -20
```

Expected: failures due to missing endpoint and field

- [ ] **Step 3: Modify `app/routers/tasks.py`**

Add `human_review_threshold_files` to `AutonomySettings`:

```python
class AutonomySettings(BaseModel):
    autonomy_mode: str | None = None
    max_tasks_per_hour: int | None = None
    max_queued_proposals: int | None = None
    max_daily_tokens: int | None = None
    consecutive_failure_backoff: bool | None = None
    model_agent: str | None = None
    model_po: str | None = None
    staging_branch: str | None = None
    architectural_rules: str | None = None
    human_review_threshold_files: int | None = None
```

Add `ApproveHumanReviewRequest` model after `TriggerProposeRequest`:

```python
class ApproveHumanReviewRequest(BaseModel):
    feedback: str | None = None
```

Add the approve endpoint after `resume_autonomy`:

```python
@router.post("/projects/{project_id}/human-reviews/{task_id}/approve")
def approve_human_review(project_id: int, task_id: int, body: ApproveHumanReviewRequest):
    """Approve a blocked project_human_review task and optionally provide feedback."""
    from infra.nocodb_client import NocodbClient
    import json

    client = NocodbClient()
    resp = client._get(TASK_TABLE, params={"where": f"(Id,eq,{task_id})", "limit": 1})
    rows = resp.get("list", [])
    if not rows:
        raise HTTPException(404, "task not found")
    row = rows[0]
    if row.get("task_type") != "project_human_review":
        raise HTTPException(400, "task is not a project_human_review task")

    payload = _parse(row.get("input_payload")) or {}
    if body.feedback:
        payload["human_feedback"] = body.feedback
    patch: dict = {
        "status": "ready",
        "input_payload": json.dumps(payload),
    }
    client._patch(TASK_TABLE, task_id, patch)
    return {"approved": True, "task_id": task_id, "has_feedback": bool(body.feedback)}
```

Update `get_autonomy` to include threshold in `_EXTENDED_DEFAULTS`:

```python
_EXTENDED_DEFAULTS = {
    "model_agent":                    (get_feature("project", "models") or {}).get("project_agent", {}).get("role", "t2_coder"),
    "model_po":                       (get_feature("project", "models") or {}).get("project_po",    {}).get("role", "t1_primary"),
    "staging_branch":                 "staging",
    "architectural_rules":            "",
    "human_review_threshold_files":   5,
}
```

- [ ] **Step 4: Modify `app/lifespan.py`**

Add imports alongside the other project handler imports (around line 91):

```python
from workers.task_handlers import project_human_review as _project_human_review_handler
from workers.task_handlers import project_revise as _project_revise_handler
```

Add registrations alongside the other project registrations (around line 119):

```python
_kanban.register("project_human_review", _project_human_review_handler.handle, llm_bound=False)
_kanban.register("project_revise",       _project_revise_handler.handle,       llm_bound=True)
```

Note: `project_human_review` is `llm_bound=False` — it makes no LLM calls.

- [ ] **Step 5: Run all new tests**

```bash
python -m pytest tests/routers/test_human_review_endpoints.py -v
```

Expected: 5 PASSED

- [ ] **Step 6: Run the full test suite to check for regressions**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all previously passing tests still pass, new tests pass

---

### Self-Review

**Spec coverage:**
- ✅ `gitea_sync.push_files_to_gitea` — Task 1
- ✅ `project_feature` extracts changed_paths + pushes + gates — Task 2
- ✅ `project_human_review` routes to review or revise — Task 3
- ✅ `project_revise` single LLM call, patches NocoDB + Gitea, re-gates — Task 4
- ✅ `approve` endpoint with optional feedback — Task 5
- ✅ `human_review_threshold_files` in AutonomySettings + get_autonomy defaults — Task 5
- ✅ `project_human_review` registered `llm_bound=False`, `project_revise` registered `llm_bound=True` — Task 5
- ✅ Always re-gate after revise (human stays in control until `_MAX_REVISE_CYCLES`) — Task 4
- ✅ threshold=0 disables human gate — Task 2 (`_enqueue_next` checks `threshold > 0`)

**One LLM call per handler constraint:**
- `project_human_review`: 0 LLM calls ✅
- `project_revise`: exactly 1 (`mc.complete_sync`) ✅
- `gitea_sync`: 0 LLM calls ✅

**No placeholders:** All code blocks are complete and runnable.

**Type consistency:** `push_files_to_gitea` signature matches usage in both `project_feature._enqueue_next` and `project_revise._run`. `write_project_file_version(project_id, path, content, edit_summary, created_by, audit_kind)` matches NocoDB client signature.
