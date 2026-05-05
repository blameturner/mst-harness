# Project Autonomy Guardrails — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-repo autonomy guardrails to the kanban system so that `project_*` handlers can be rate-limited, token-capped, and failure-backoffed without manual intervention.

**Architecture:** A `check_autonomy(db, task)` function raises typed exceptions (`AutonomyBlock` / `AutonomyBackoff`) when limits are exceeded. `kanban._execute` catches these before the retry path — `AutonomyBlock` sets `status='blocked'` (terminal, no retry), `AutonomyBackoff` re-queues with a `not_before` delay without consuming retry count. Per-repo settings live in the NocoDB `settings` table keyed `"project:{project_id}"`; `config.json features.project` provides system defaults.

**Tech Stack:** Python, FastAPI, NocoDB (`infra/nocodb_client.py`, `infra/settings.py`), existing kanban (`workers/kanban.py`)

---

## Design

### Per-Repo Settings

Lookup order in `_get_setting(project_id, key)`:
1. NocoDB `settings` table, `agent = "project:{project_id}"` — editable from frontend
2. `config.json` `features.project.{key}` — system defaults

### Config Defaults (add to `config.json`)

```json
"project": {
  "autonomy_mode": "proposal-only",
  "max_tasks_per_hour": 4,
  "max_queued_proposals": 10,
  "max_daily_tokens": 100000,
  "consecutive_failure_backoff": true
}
```

`autonomy_mode` is a submission-time setting (not enforced by `check_autonomy`): `proposal-only` means `project_propose` tasks are submitted with `status='pending'` requiring user approval; `full` submits them as `status='ready'`. This is enforced inside the future `project_propose` handler.

### Exception Types

```python
class AutonomyBlock(Exception):
    """Terminal — kanban sets status='blocked', no retry."""

class AutonomyBackoff(Exception):
    """Temporary — kanban re-queues with not_before delay, retry_count unchanged."""
    def __init__(self, reason: str, delay_seconds: int): ...
```

### Check Function

```python
def check_autonomy(db: NocodbClient, task: dict) -> None:
    project_id = int((task.get("input_payload") or {}).get("project_id") or 0)
    _check_queue_depth(db, project_id)       # raises AutonomyBlock if >= max_queued_proposals
    _check_hourly_rate(db, project_id)        # raises AutonomyBlock if >= max_tasks_per_hour
    _check_daily_tokens(db, project_id)       # raises AutonomyBlock if >= max_daily_tokens
    _check_consecutive_failures(db, project_id)
    # streak >= 2 → AutonomyBackoff(5 min)
    # streak >= 4 → AutonomyBackoff(30 min)
    # streak >= 6 → sets _halted flag + raises AutonomyBlock (manual resume required)
    # _halted=True → AutonomyBlock on every call until resumed
```

### Kanban Integration

`kanban._execute` gains two new catch branches (before the existing `except Exception`):

```python
except AutonomyBackoff as exc:
    not_before = (datetime.now(timezone.utc) + timedelta(seconds=exc.delay_seconds)).isoformat()
    await asyncio.to_thread(_requeue_with_delay, db, row_id, not_before, str(exc))
except AutonomyBlock as exc:
    await asyncio.to_thread(_mark_blocked, db, row_id, str(exc))
```

### Runtime State Queries

All state is read from `task_list`. Project tasks MUST be submitted with `created_by = f"project:{project_id}"`.

| Guard | Query |
|---|---|
| Queue depth | `task_list` where `created_by=project:{id}`, `task_type=project_propose`, `status in (ready,pending,claimed)` |
| Hourly rate | `task_list` where `created_by=project:{id}`, `started_at > now-1h` |
| Daily tokens | `task_list` where `created_by=project:{id}`, `status=done`, `completed_at > today_utc_midnight`; sum `output_payload.tokens_used` |
| Consecutive failures | last 6 `task_list` rows where `created_by=project:{id}`, `status in (done,failed,blocked)` sorted by `completed_at desc`; count leading `failed` rows |

Project handlers MUST emit `{"tokens_used": <int>}` in their `output_payload` for the daily token check.

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Create | `workers/project_autonomy.py` | `AutonomyBlock`, `AutonomyBackoff`, `check_autonomy`, 4 guard helpers, `_get_setting` |
| Modify | `workers/kanban.py` | `_mark_blocked`, `_requeue_with_delay`, catch both exceptions in `_execute` |
| Modify | `config.json` | Add `project` defaults section |
| Modify | `app/routers/tasks.py` | `GET/PUT /projects/{id}/autonomy`, `POST /projects/{id}/autonomy/resume` |
| Create | `tests/workers/test_project_autonomy.py` | Unit tests for all 4 guards + halt + backoff |

---

## Task 1: Config defaults + kanban extensions

**Files:**
- Modify: `config.json` (add `project` section)
- Modify: `workers/kanban.py` (add `_mark_blocked`, `_requeue_with_delay`, catch new exceptions in `_execute`)

- [ ] **Step 1: Add `project` section to `config.json`**

In `config.json`, inside `"features"`, add (after the last feature section, before the closing `}`):

```json
"project": {
  "autonomy_mode": "proposal-only",
  "max_tasks_per_hour": 4,
  "max_queued_proposals": 10,
  "max_daily_tokens": 100000,
  "consecutive_failure_backoff": true
}
```

- [ ] **Step 2: Add `_mark_blocked` and `_requeue_with_delay` to `workers/kanban.py`**

After `_mark_failed` (around line 211), add:

```python
def _mark_blocked(db: NocodbClient, row_id: int, reason: str) -> None:
    db._patch(TASK_TABLE, row_id, {
        "status": "blocked", "error": reason, "completed_at": _iso_now(),
    })
    _log.warning("kanban blocked  row_id=%s  reason=%s", row_id, reason)


def _requeue_with_delay(db: NocodbClient, row_id: int, not_before: str, reason: str) -> None:
    db._patch(TASK_TABLE, row_id, {"status": "ready", "not_before": not_before})
    _log.info("kanban autonomy-requeue  row_id=%s  not_before=%s  reason=%s", row_id, not_before, reason)
```

- [ ] **Step 3: Catch `AutonomyBlock` and `AutonomyBackoff` in `_execute`**

In `_execute`, between the `try: output = await entry.handler(task)` block and the existing `except Exception as exc:`, insert:

```python
    except AutonomyBackoff as exc:
        from workers.project_autonomy import AutonomyBackoff as _AB  # noqa: F811
        not_before = (datetime.now(timezone.utc) + timedelta(seconds=exc.delay_seconds)).isoformat()
        await asyncio.to_thread(_requeue_with_delay, db, row_id, not_before, str(exc))
        _log.info("kanban autonomy-backoff  row_id=%s  delay=%ds", row_id, exc.delay_seconds)
        return
    except AutonomyBlock as exc:
        await asyncio.to_thread(_mark_blocked, db, row_id, str(exc))
        _log.info("kanban autonomy-blocked  row_id=%s  reason=%s", row_id, exc)
        return
```

Add the lazy imports at the top of `_execute` (to avoid circular imports, import inside the function body):

```python
    from workers.project_autonomy import AutonomyBlock, AutonomyBackoff
```

This import goes at the start of the `try` block in `_execute`.

- [ ] **Step 4: Verify `datetime` and `timedelta` are imported in `kanban.py`**

Check the imports at the top of `workers/kanban.py` — `datetime` and `timedelta` are already imported from `datetime`. `timezone` is also already imported. No change needed.

- [ ] **Step 5: Commit**

```bash
git add config.json workers/kanban.py
git commit -m "feat: add blocked status and autonomy exception handling to kanban"
```

---

## Task 2: Write failing tests

**Files:**
- Create: `tests/workers/test_project_autonomy.py`

- [ ] **Step 1: Create `tests/workers/` directory if needed**

```bash
mkdir -p tests/workers && touch tests/workers/__init__.py
```

- [ ] **Step 2: Write the test file**

```python
"""Tests for workers.project_autonomy guardrails."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from workers.project_autonomy import AutonomyBackoff, AutonomyBlock, check_autonomy

PROJECT_ID = 42
TASK_TABLE = "task_list"


def _task(project_id: int = PROJECT_ID) -> dict:
    return {"Id": 1, "task_type": "project_propose", "input_payload": {"project_id": project_id}}


def _db(*call_results: list) -> MagicMock:
    """Mock db where each _get call returns the next result in call_results."""
    db = MagicMock()
    db._get.side_effect = [{"list": rows} for rows in call_results]
    return db


def _no_setting(project_id: int, key: str) -> object:
    return None


# ── queue depth ───────────────────────────────────────────────────────────────

def test_queue_depth_under_limit_proceeds():
    db = _db([])  # 0 queued proposals
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        check_autonomy(db, _task())


def test_queue_depth_at_limit_blocks():
    rows = [{"Id": i} for i in range(10)]  # 10 = default max_queued_proposals
    db = _db(rows)
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        with pytest.raises(AutonomyBlock, match="proposal queue"):
            check_autonomy(db, _task())


# ── hourly rate ───────────────────────────────────────────────────────────────

def test_hourly_rate_under_limit_proceeds():
    db = _db([], [])  # queue=0, hourly=0
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        check_autonomy(db, _task())


def test_hourly_rate_at_limit_blocks():
    rows = [{"Id": i} for i in range(4)]  # 4 = default max_tasks_per_hour
    db = _db([], rows)  # queue=0, hourly=4
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        with pytest.raises(AutonomyBlock, match="hourly rate"):
            check_autonomy(db, _task())


# ── daily tokens ──────────────────────────────────────────────────────────────

def test_daily_tokens_under_cap_proceeds():
    rows = [{"output_payload": json.dumps({"tokens_used": 10_000})} for _ in range(3)]
    db = _db([], [], rows)  # queue=0, hourly=0, daily=30k
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        check_autonomy(db, _task())


def test_daily_tokens_over_cap_blocks():
    rows = [{"output_payload": json.dumps({"tokens_used": 50_000})} for _ in range(3)]
    db = _db([], [], rows)  # 150k > 100k cap
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        with pytest.raises(AutonomyBlock, match="daily token"):
            check_autonomy(db, _task())


def test_daily_tokens_missing_field_counts_zero():
    rows = [{"output_payload": json.dumps({})} for _ in range(3)]
    db = _db([], [], rows)
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        check_autonomy(db, _task())


# ── consecutive failures ──────────────────────────────────────────────────────

def test_no_failures_proceeds():
    db = _db([], [], [], [])  # queue, hourly, daily, failures=[]
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        check_autonomy(db, _task())


def test_two_consecutive_failures_backoff_5min():
    db = _db([], [], [], [{"status": "failed"}, {"status": "failed"}])
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        with pytest.raises(AutonomyBackoff) as exc_info:
            check_autonomy(db, _task())
    assert exc_info.value.delay_seconds == 300


def test_four_consecutive_failures_backoff_30min():
    db = _db([], [], [], [{"status": "failed"}] * 4)
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        with pytest.raises(AutonomyBackoff) as exc_info:
            check_autonomy(db, _task())
    assert exc_info.value.delay_seconds == 1800


def test_six_consecutive_failures_halts():
    db = _db([], [], [], [{"status": "failed"}] * 6)
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        with patch("workers.project_autonomy._set_halted") as mock_halt:
            with pytest.raises(AutonomyBlock, match="halted"):
                check_autonomy(db, _task())
            mock_halt.assert_called_once_with(PROJECT_ID, True)


def test_mixed_failures_not_consecutive_proceeds():
    rows = [{"status": "failed"}, {"status": "done"}, {"status": "failed"}]
    db = _db([], [], [], rows)
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        check_autonomy(db, _task())


def test_halted_flag_blocks_regardless_of_streak():
    db = _db([], [], [], [])
    def _halted_setting(project_id: int, key: str) -> object:
        return True if key == "_halted" else None
    with patch("workers.project_autonomy._get_setting", side_effect=_halted_setting):
        with pytest.raises(AutonomyBlock, match="halted"):
            check_autonomy(db, _task())


def test_backoff_disabled_skips_failure_check():
    db = _db([], [], [])  # only 3 calls: queue, hourly, daily (no failures call)
    def _no_backoff(project_id: int, key: str) -> object:
        return False if key == "consecutive_failure_backoff" else None
    with patch("workers.project_autonomy._get_setting", side_effect=_no_backoff):
        check_autonomy(db, _task())
    assert db._get.call_count == 3
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/workers/test_project_autonomy.py -v
```

Expected: `ModuleNotFoundError` — `workers.project_autonomy` does not exist yet.

- [ ] **Step 4: Commit**

```bash
git add tests/workers/__init__.py tests/workers/test_project_autonomy.py
git commit -m "test: add failing tests for project autonomy guardrails"
```

---

## Task 3: Implement `workers/project_autonomy.py`

**Files:**
- Create: `workers/project_autonomy.py`

- [ ] **Step 1: Write the implementation**

```python
"""Pre-execution guardrails for project_* kanban tasks.

Call check_autonomy(db, task) at the top of every project_* handler.
Raises AutonomyBlock(reason) or AutonomyBackoff(reason, delay_seconds).

kanban._execute catches these before the retry path:
  AutonomyBlock  → status='blocked', no retry
  AutonomyBackoff → re-queued with not_before delay, retry_count unchanged
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from infra.nocodb_client import NocodbClient

_log = logging.getLogger("project_autonomy")

TASK_TABLE = "task_list"

_DEFAULTS: dict[str, object] = {
    "autonomy_mode": "proposal-only",
    "max_tasks_per_hour": 4,
    "max_queued_proposals": 10,
    "max_daily_tokens": 100_000,
    "consecutive_failure_backoff": True,
}


class AutonomyBlock(Exception):
    """Terminal block — kanban sets status='blocked'. No retry."""


class AutonomyBackoff(Exception):
    """Temporary backoff — kanban re-queues with not_before delay."""

    def __init__(self, reason: str, delay_seconds: int) -> None:
        super().__init__(reason)
        self.delay_seconds = delay_seconds


def check_autonomy(db: NocodbClient, task: dict) -> None:
    """Raises AutonomyBlock or AutonomyBackoff if any guardrail trips."""
    project_id = int((task.get("input_payload") or {}).get("project_id") or 0)
    _check_queue_depth(db, project_id)
    _check_hourly_rate(db, project_id)
    _check_daily_tokens(db, project_id)
    _check_consecutive_failures(db, project_id)


# ── settings reader ───────────────────────────────────────────────────────────

def _get_setting(project_id: int, key: str) -> object:
    try:
        from infra.settings import get_agent_setting
        val = get_agent_setting(f"project:{project_id}", key)
        if val is not None:
            return val
    except Exception:
        pass
    from infra.config import get_feature
    return get_feature("project", key, _DEFAULTS.get(key))


def _set_halted(project_id: int, halted: bool) -> None:
    try:
        from infra.settings import set_agent_setting
        set_agent_setting(f"project:{project_id}", "_halted", halted)
    except Exception:
        _log.error("failed to write _halted flag  project_id=%d", project_id, exc_info=True)


# ── guards ────────────────────────────────────────────────────────────────────

def _check_queue_depth(db: NocodbClient, project_id: int) -> None:
    limit = int(_get_setting(project_id, "max_queued_proposals") or _DEFAULTS["max_queued_proposals"])
    rows = db._get(TASK_TABLE, params={
        "where": (
            f"(created_by,eq,project:{project_id})"
            "~and(task_type,eq,project_propose)"
            "~and((status,eq,ready)~or(status,eq,pending)~or(status,eq,claimed))"
        ),
        "limit": limit + 1,
    }).get("list", [])
    if len(rows) >= limit:
        raise AutonomyBlock(
            f"proposal queue at limit ({limit}); no new proposals until the queue drains"
        )


def _check_hourly_rate(db: NocodbClient, project_id: int) -> None:
    limit = int(_get_setting(project_id, "max_tasks_per_hour") or _DEFAULTS["max_tasks_per_hour"])
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rows = db._get(TASK_TABLE, params={
        "where": (
            f"(created_by,eq,project:{project_id})"
            f"~and(started_at,gt,{one_hour_ago})"
        ),
        "limit": limit + 1,
    }).get("list", [])
    if len(rows) >= limit:
        raise AutonomyBlock(f"hourly rate limit reached ({limit}/hr); try again later")


def _check_daily_tokens(db: NocodbClient, project_id: int) -> None:
    cap = int(_get_setting(project_id, "max_daily_tokens") or _DEFAULTS["max_daily_tokens"])
    today_start = (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )
    rows = db._get(TASK_TABLE, params={
        "where": (
            f"(created_by,eq,project:{project_id})"
            f"~and(completed_at,gt,{today_start})"
            "~and(status,eq,done)"
        ),
        "limit": 500,
    }).get("list", [])
    total = 0
    for row in rows:
        raw = row.get("output_payload")
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        total += int(payload.get("tokens_used") or 0)
    if total >= cap:
        raise AutonomyBlock(
            f"daily token cap reached ({total:,}/{cap:,}); halted until midnight UTC"
        )


def _check_consecutive_failures(db: NocodbClient, project_id: int) -> None:
    enabled = _get_setting(project_id, "consecutive_failure_backoff")
    if not enabled:
        return

    if _get_setting(project_id, "_halted"):
        raise AutonomyBlock(
            "halted: manual resume required via POST /projects/{id}/autonomy/resume"
        )

    rows = db._get(TASK_TABLE, params={
        "where": (
            f"(created_by,eq,project:{project_id})"
            "~and((status,eq,done)~or(status,eq,failed)~or(status,eq,blocked))"
        ),
        "sort": "-completed_at",
        "limit": 6,
    }).get("list", [])

    streak = 0
    for row in rows:
        if row.get("status") == "failed":
            streak += 1
        else:
            break

    if streak >= 6:
        _set_halted(project_id, True)
        raise AutonomyBlock(
            f"halted: {streak} consecutive failures — manual resume required "
            "via POST /projects/{id}/autonomy/resume"
        )
    if streak >= 4:
        raise AutonomyBackoff(
            f"backoff: {streak} consecutive failures — pausing 30 minutes", 1800
        )
    if streak >= 2:
        raise AutonomyBackoff(
            f"backoff: {streak} consecutive failures — pausing 5 minutes", 300
        )
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/workers/test_project_autonomy.py -v
```

Expected: all 13 tests pass.

- [ ] **Step 3: Run linter**

```bash
ruff check workers/project_autonomy.py
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add workers/project_autonomy.py
git commit -m "feat: implement project autonomy guardrails"
```

---

## Task 4: Frontend routes

**Files:**
- Modify: `app/routers/tasks.py`

- [ ] **Step 1: Add `AutonomySettings` model and routes to `app/routers/tasks.py`**

After the closing brace of `create_task`, append:

```python
class AutonomySettings(BaseModel):
    autonomy_mode: str | None = None
    max_tasks_per_hour: int | None = None
    max_queued_proposals: int | None = None
    max_daily_tokens: int | None = None
    consecutive_failure_backoff: bool | None = None


@router.get("/projects/{project_id}/autonomy")
def get_autonomy(project_id: int):
    from infra.settings import get_agent_setting
    from infra.config import get_feature
    from workers.project_autonomy import _DEFAULTS

    agent = f"project:{project_id}"
    result: dict = {}
    for key, default in _DEFAULTS.items():
        db_val = get_agent_setting(agent, key)
        result[key] = db_val if db_val is not None else get_feature("project", key, default)
    halted = get_agent_setting(agent, "_halted")
    result["_halted"] = bool(halted)
    return result


@router.put("/projects/{project_id}/autonomy")
def put_autonomy(project_id: int, body: AutonomySettings):
    from infra.settings import set_agent_setting

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "no fields provided")
    agent = f"project:{project_id}"
    for key, value in updates.items():
        set_agent_setting(agent, key, value)
    return {"updated": list(updates.keys())}


@router.post("/projects/{project_id}/autonomy/resume")
def resume_autonomy(project_id: int):
    """Clear a project halt so its tasks can run again."""
    from infra.settings import set_agent_setting

    set_agent_setting(f"project:{project_id}", "_halted", False)
    return {"resumed": True}
```

- [ ] **Step 2: Run linter**

```bash
ruff check app/routers/tasks.py
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add app/routers/tasks.py
git commit -m "feat: add autonomy GET/PUT/resume routes to tasks router"
```

---

## Self-Review

**Spec coverage:**
- [x] `autonomy_mode` — in config defaults + NocoDB; `proposal-only` vs `full` enforced at submission time in future `project_propose` handler (noted in design, not in this plan's scope)
- [x] `max_tasks_per_hour` (default 4) — `_check_hourly_rate` raises `AutonomyBlock`
- [x] `max_queued_proposals` (default 10, no-op when >= limit) — `_check_queue_depth` raises `AutonomyBlock`
- [x] `max_daily_tokens` — `_check_daily_tokens` raises `AutonomyBlock`; handlers must emit `tokens_used` in `output_payload`
- [x] `consecutive_failure_backoff`: 2→5min, 4→30min, 6→halt — `_check_consecutive_failures` raises `AutonomyBackoff` or `AutonomyBlock`
- [x] Pre-execution check called by every `project_*` handler — `check_autonomy(db, task)`
- [x] Returns proceed/block — raises typed exceptions
- [x] Blocked tasks: `status='blocked'`, reason in `error` — `_mark_blocked` in kanban
- [x] No auto-retry for blocked — catch branch returns early, no retry path
- [x] Changeable in frontend — `GET/PUT /projects/{id}/autonomy`; resume via `POST .../resume`
- [x] Config.json as defaults — `_get_setting` falls back via `get_feature("project", key, default)`

**Placeholder scan:** None found — all code blocks are complete.

**Type consistency:** `AutonomyBackoff` defined in Task 3, referenced in Task 2 tests and Task 1 kanban catch. `_set_halted` defined and tested via mock. `_get_setting` defined and patched in tests.

**Note for future `project_propose` handler:** read `_get_setting(project_id, "autonomy_mode")` at submission time — if `"proposal-only"`, submit with `status="pending"` instead of `status="ready"`.