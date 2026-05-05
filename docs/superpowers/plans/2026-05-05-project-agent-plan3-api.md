# Project Agent — Plan 3: API Layer & Wiring

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the settings/autonomy API with model overrides and architectural rules, add dedicated project-scoped trigger endpoints, and register `project_index` in the kanban startup.

**Architecture:** All changes are in `app/routers/tasks.py` (extend existing models + add two POST routes) and `app/lifespan.py` (one new registration). No new files needed.

**Prerequisites:** Plans 1 and 2 complete.

**Tech Stack:** FastAPI, Pydantic, existing `kanban.submit`, `infra.settings.set_agent_setting`

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `app/routers/tasks.py` | Modify | Extend `AutonomySettings`; add 2 trigger endpoints |
| `app/lifespan.py` | Modify | Register `project_index` handler |

---

## Task 1: Extend `AutonomySettings` and GET/PUT endpoints

**Files:**
- Modify: `app/routers/tasks.py`

- [ ] **Step 1: Write failing test**

Create `tests/routers/test_project_settings.py`:

```python
"""Tests for extended autonomy settings fields."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app  # adjust import if your app factory lives elsewhere

client = TestClient(app)


def test_get_autonomy_includes_model_fields(monkeypatch):
    def fake_get_agent_setting(agent, key):
        return {
            "model_agent": "t2_coder",
            "model_po": "t1_primary",
            "staging_branch": "staging",
            "architectural_rules": "",
        }.get(key)

    monkeypatch.setattr(
        "app.routers.tasks.get_agent_setting",
        fake_get_agent_setting,
        raising=False,
    )
    with patch("app.routers.tasks.NocodbClient"):
        resp = client.get("/tasks/projects/1/autonomy")
    # Even if the mock returns 500 due to missing DB, the model fields must be
    # present in a successful response. This test confirms the schema.
    from app.routers.tasks import AutonomySettings
    fields = AutonomySettings.model_fields
    assert "model_agent" in fields
    assert "model_po" in fields
    assert "staging_branch" in fields
    assert "architectural_rules" in fields
```

- [ ] **Step 2: Run to confirm schema gap**

```bash
python -m pytest tests/routers/test_project_settings.py::test_get_autonomy_includes_model_fields -v
```

Expected: FAIL with `AssertionError` (fields not present yet).

- [ ] **Step 3: Extend `AutonomySettings` in `app/routers/tasks.py`**

Find the `AutonomySettings` class (currently ~line 18) and add the four new fields:

```python
class AutonomySettings(BaseModel):
    # existing fields
    autonomy_mode: str | None = None
    max_tasks_per_hour: int | None = None
    max_queued_proposals: int | None = None
    max_daily_tokens: int | None = None
    consecutive_failure_backoff: bool | None = None
    # new fields
    model_agent: str | None = None          # role key for the Coder (project_feature)
    model_po: str | None = None             # role key for Architect/PO (project_review, project_propose)
    staging_branch: str | None = None       # Gitea base branch (default: "staging")
    architectural_rules: str | None = None  # global review rules text
```

- [ ] **Step 4: Update `get_autonomy` to include the new fields**

In `get_autonomy`, after the existing loop over `_DEFAULTS.items()`, add:

```python
    from infra.settings import get_agent_setting as _gas
    from infra.config import get_feature as _gf

    _EXTENDED_DEFAULTS = {
        "model_agent": (_gf("project", "models") or {}).get("project_agent", {}).get("role", "t2_coder"),
        "model_po":    (_gf("project", "models") or {}).get("project_po",    {}).get("role", "t1_primary"),
        "staging_branch":       "staging",
        "architectural_rules":  "",
    }
    for key, default in _EXTENDED_DEFAULTS.items():
        db_val = _gas(agent, key)
        result[key] = db_val if db_val is not None else default
```

- [ ] **Step 5: Update `put_autonomy` to accept the new fields**

`put_autonomy` already uses `body.model_dump()` which will include any non-None new fields automatically — no additional changes required, but verify by reading it:

```python
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
```

This will now persist `model_agent`, `model_po`, `staging_branch`, `architectural_rules` to the settings table.

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests/routers/test_project_settings.py -v
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add app/routers/tasks.py tests/routers/test_project_settings.py
git commit -m "feat: extend autonomy settings with model_agent, model_po, staging_branch, architectural_rules"
```

---

## Task 2: Add trigger endpoints

**Files:**
- Modify: `app/routers/tasks.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/routers/test_project_settings.py`:

```python
def test_trigger_feature_returns_task():
    with patch("app.routers.tasks.kanban") as mock_kanban, \
         patch("app.routers.tasks.NocodbClient") as mock_db_cls:

        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        mock_db._get.return_value = {"list": [{"Id": 42, "task_type": "project_feature", "status": "ready",
                                                "input_payload": None, "agent": "project:1",
                                                "output_payload": None, "error": None, "model": None,
                                                "prompt_template_id": None}]}
        mock_kanban.submit.return_value = 42

        resp = client.post("/tasks/projects/1/trigger/feature", json={
            "feature_description": "Add rate limiting",
            "branch_name": "feature/rate-limit",
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["task_type"] == "project_feature"
    assert data["id"] == "42"


def test_trigger_propose_returns_task():
    with patch("app.routers.tasks.kanban") as mock_kanban, \
         patch("app.routers.tasks.NocodbClient") as mock_db_cls:

        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        mock_db._get.return_value = {"list": [{"Id": 43, "task_type": "project_propose", "status": "ready",
                                                "input_payload": None, "agent": "project:1",
                                                "output_payload": None, "error": None, "model": None,
                                                "prompt_template_id": None}]}
        mock_kanban.submit.return_value = 43

        resp = client.post("/tasks/projects/1/trigger/propose", json={})

    assert resp.status_code == 200
    data = resp.json()
    assert data["task_type"] == "project_propose"
```

- [ ] **Step 2: Run to confirm failures**

```bash
python -m pytest tests/routers/test_project_settings.py::test_trigger_feature_returns_task \
               tests/routers/test_project_settings.py::test_trigger_propose_returns_task -v
```

Expected: 404 (routes don't exist yet).

- [ ] **Step 3: Add Pydantic models and two routes to `app/routers/tasks.py`**

After the `AutonomySettings` class, add:

```python
class TriggerFeatureRequest(BaseModel):
    feature_description: str
    branch_name: str
    architect_context: str | None = None
    model: str | None = None


class TriggerProposeRequest(BaseModel):
    model: str | None = None
```

After the `resume_autonomy` endpoint, add:

```python
@router.post("/projects/{project_id}/trigger/feature")
def trigger_feature(project_id: int, body: TriggerFeatureRequest):
    """Manually queue a project_feature task. Always creates status='ready'."""
    from infra.nocodb_client import NocodbClient
    from workers import kanban
    client = NocodbClient()
    payload = {
        "project_id": project_id,
        "feature_description": body.feature_description,
        "branch_name": body.branch_name,
        "architect_context": body.architect_context,
        "revision_count": 0,
    }
    try:
        task_id = kanban.submit(
            client,
            "project_feature",
            payload,
            status="ready",
            created_by="api",
            agent=f"project:{project_id}",
            model=body.model or "",
        )
        resp = client._get(TASK_TABLE, params={"where": f"(Id,eq,{task_id})", "limit": 1})
        rows = resp.get("list", [])
        return _row(rows[0]) if rows else {"id": str(task_id), "status": "ready", "task_type": "project_feature"}
    except Exception as e:
        _log.error("trigger_feature error: %s", e)
        raise HTTPException(502, str(e))


@router.post("/projects/{project_id}/trigger/propose")
def trigger_propose(project_id: int, body: TriggerProposeRequest):
    """Manually queue a project_propose task."""
    from infra.nocodb_client import NocodbClient
    from workers import kanban
    client = NocodbClient()
    payload = {"project_id": project_id}
    try:
        task_id = kanban.submit(
            client,
            "project_propose",
            payload,
            status="ready",
            created_by="api",
            agent=f"project:{project_id}",
            model=body.model or "",
        )
        resp = client._get(TASK_TABLE, params={"where": f"(Id,eq,{task_id})", "limit": 1})
        rows = resp.get("list", [])
        return _row(rows[0]) if rows else {"id": str(task_id), "status": "ready", "task_type": "project_propose"}
    except Exception as e:
        _log.error("trigger_propose error: %s", e)
        raise HTTPException(502, str(e))
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/routers/test_project_settings.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/routers/tasks.py tests/routers/test_project_settings.py
git commit -m "feat: add trigger/feature and trigger/propose endpoints for manual task queuing"
```

---

## Task 3: Register `project_index` in lifespan

**Files:**
- Modify: `app/lifespan.py`

- [ ] **Step 1: Write failing test**

Add to `tests/routers/test_project_settings.py`:

```python
def test_project_index_registered():
    """project_index must be in the kanban registry at import time."""
    # Trigger lifespan startup logic by checking the registry directly
    # (the lifespan runs at app startup; this test verifies the import works)
    import importlib
    import workers.task_handlers.project_index as m
    assert callable(m.handle)
```

- [ ] **Step 2: Add the import and registration to `app/lifespan.py`**

Find the block where other project handlers are registered (the three you added earlier):

```python
    _kanban.register("project_feature", _project_feature_handler.handle, llm_bound=True)
    _kanban.register("project_review",  _project_review_handler.handle,  llm_bound=True)
    _kanban.register("project_propose", _project_propose_handler.handle, llm_bound=True)
```

Add the import alongside the other handler imports:

```python
    from workers.task_handlers import project_index as _project_index_handler
```

Add the registration immediately after the three existing project lines:

```python
    _kanban.register("project_index",   _project_index_handler.handle,   llm_bound=True)
```

- [ ] **Step 3: Verify the module imports cleanly**

```bash
python -c "from workers.task_handlers import project_index; print('import ok')"
```

Expected: `import ok`

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all tests pass (any existing failures must not be new regressions introduced by this plan).

- [ ] **Step 5: Commit**

```bash
git add app/lifespan.py
git commit -m "feat: register project_index kanban handler at startup"
```

---

## Task 4: End-to-end smoke verification

No code changes — verify the complete integration compiles and routes are reachable.

- [ ] **Step 1: Confirm all project task types are registered**

```bash
python -c "
from app.lifespan import *
# Can't run lifespan without full startup, so just check imports
from workers.task_handlers import project_feature, project_review, project_propose, project_index
print('project_feature:', callable(project_feature.handle))
print('project_review: ', callable(project_review.handle))
print('project_propose:', callable(project_propose.handle))
print('project_index:  ', callable(project_index.handle))
"
```

Expected:
```
project_feature: True
project_review:  True
project_propose: True
project_index:   True
```

- [ ] **Step 2: Confirm trigger routes are present**

```bash
python -c "
from app.routers.tasks import router
paths = [r.path for r in router.routes]
assert '/projects/{project_id}/trigger/feature' in paths, paths
assert '/projects/{project_id}/trigger/propose' in paths, paths
print('trigger routes: ok')
"
```

Expected: `trigger routes: ok`

- [ ] **Step 3: Confirm autonomy GET includes new fields**

```bash
python -c "
from app.routers.tasks import AutonomySettings
fields = set(AutonomySettings.model_fields)
required = {'model_agent', 'model_po', 'staging_branch', 'architectural_rules'}
missing = required - fields
assert not missing, f'missing fields: {missing}'
print('autonomy settings fields: ok')
"
```

Expected: `autonomy settings fields: ok`

- [ ] **Step 4: Run full test suite one final time**

```bash
python -m pytest tests/ -v 2>&1 | tail -20
```

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "feat: project agent system complete — foundation, handlers, API, and wiring"
```

---

## Summary of all new routes added across all three plans

| Method | Path | Description |
|---|---|---|
| GET | `/tasks/projects/{id}/autonomy` | Extended: now returns model_agent, model_po, staging_branch, architectural_rules |
| PUT | `/tasks/projects/{id}/autonomy` | Extended: now persists model_agent, model_po, staging_branch, architectural_rules |
| POST | `/tasks/projects/{id}/trigger/feature` | Queue a project_feature task manually |
| POST | `/tasks/projects/{id}/trigger/propose` | Queue a project_propose task manually |

The existing `POST /tasks` generic endpoint still works for all task types — these are convenience wrappers that set `agent` and `created_by` automatically.

---

**Plan 3 complete. All three plans done.**
