# Project Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement fixes 2–5 from PROJECT_REVIEW.md (fix #1 is deferred — touches 5+ files).

**Architecture:** Four small targeted edits across the project pipeline handlers and the autonomy module. No new files, no new dependencies.

**Tech Stack:** Python, asyncio, structlog/logging

---

## File Map

| File | Change |
|---|---|
| `workers/task_handlers/project_review.py` | Fix #2: log swallowed exception in `_do_approve` |
| `workers/task_handlers/project_revise.py` | Fix #3: use `human_revision_count` to avoid cap collision |
| `workers/project_autonomy.py` | Fix #4: expose `_get_setting` and `_check_queue_depth` as public names |
| `workers/task_handlers/project_propose.py` | Fix #4: use public names; Fix #5: upgrade log level |

---

## Task 1: Fix silent swallowed exception in `_do_approve` (Fix #2)

**Files:** Modify `workers/task_handlers/project_review.py:97-101`

- [ ] **Step 1: Apply the fix**

In `_do_approve`, lines 97–101 currently read:

```python
    try:
        from workers import kanban as _kanban
        _kanban.submit(db, "project_index", {"project_id": project_id, "trigger": "post_merge"},
                       created_by=f"project:{project_id}", agent=f"project:{project_id}")
    except Exception:
        pass
```

Change to:

```python
    try:
        from workers import kanban as _kanban
        _kanban.submit(db, "project_index", {"project_id": project_id, "trigger": "post_merge"},
                       created_by=f"project:{project_id}", agent=f"project:{project_id}")
    except Exception as exc:
        _log.warning("project_review: project_index enqueue failed  err=%s", exc)
```

- [ ] **Step 2: Verify no lint errors**

```bash
cd /Users/michaelturner/PycharmProjects/JeffGPT-Harness
python -m py_compile workers/task_handlers/project_review.py && echo OK
```

Expected: `OK`

---

## Task 2: Separate `human_revision_count` from `revision_count` (Fix #3)

**Files:** Modify `workers/task_handlers/project_revise.py:74,115,162-178`

Context: `project_review.py` caps on `revision_count` (agent cycles only). `project_revise.py` currently also increments `revision_count`, which burns through the agent-cycle cap after human revisions. The fix: `project_revise` tracks its own `human_revision_count` field and never touches `revision_count`.

- [ ] **Step 1: Update `_run` to use `human_revision_count`**

Current line 74:
```python
    revision_count = int(payload.get("revision_count") or 0)
```

Change to:
```python
    revision_count = int(payload.get("revision_count") or 0)
    human_revision_count = int(payload.get("human_revision_count") or 0)
```

- [ ] **Step 2: Update cap check and base_payload**

Current lines 161–179:
```python
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
```

Change to:
```python
    base_payload = {k: payload[k] for k in payload if k != "human_feedback"}
    base_payload["human_revision_count"] = human_revision_count + 1

    if human_revision_count + 1 >= _MAX_REVISE_CYCLES:
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
```

- [ ] **Step 3: Update the return dict**

Current:
```python
    return {
        "status": "done",
        "patched_paths": patched_paths,
        "revision_count": revision_count + 1,
        "tokens_used": approx_tokens,
        "next_action": next_action,
    }
```

Change to:
```python
    return {
        "status": "done",
        "patched_paths": patched_paths,
        "revision_count": revision_count,
        "human_revision_count": human_revision_count + 1,
        "tokens_used": approx_tokens,
        "next_action": next_action,
    }
```

- [ ] **Step 4: Update the module docstring** (line 17)

Current:
```
  revision_count: int           — cycles completed so far
```

Change to:
```
  revision_count: int           — agent revision cycles completed so far
  human_revision_count: int     — human annotation cycles completed so far
```

- [ ] **Step 5: Verify compilation**

```bash
python -m py_compile workers/task_handlers/project_revise.py && echo OK
```

Expected: `OK`

---

## Task 3: Expose private helpers as public names in `project_autonomy` (Fix #4)

**Files:** Modify `workers/project_autonomy.py`

Currently `_get_setting` and `_check_queue_depth` are private (`_` prefix) but imported as such by `project_propose.py`. The fix: add public aliases.

- [ ] **Step 1: Add public aliases below the private definitions**

In `workers/project_autonomy.py`, after the `_get_setting` function definition (after line 66), add:

```python
get_project_setting = _get_setting
```

After the `_check_queue_depth` function definition (after line 92), add:

```python
check_queue_depth = _check_queue_depth
```

- [ ] **Step 2: Verify compilation**

```bash
python -m py_compile workers/project_autonomy.py && echo OK
```

Expected: `OK`

---

## Task 4: Update `project_propose` to use public names + warning log (Fixes #4 and #5)

**Files:** Modify `workers/task_handlers/project_propose.py:46,76,111,169`

- [ ] **Step 1: Update the import**

Current line 46:
```python
    from workers.project_autonomy import check_autonomy, _get_setting, _check_queue_depth
```

Change to:
```python
    from workers.project_autonomy import check_autonomy, get_project_setting, check_queue_depth
```

- [ ] **Step 2: Update the two call sites**

Line 76:
```python
        _check_queue_depth(db, project_id)
```
Change to:
```python
        check_queue_depth(db, project_id)
```

Line 111:
```python
    autonomy_mode = str(_get_setting(project_id, "autonomy_mode") or "proposal-only")
```
Change to:
```python
    autonomy_mode = str(get_project_setting(project_id, "autonomy_mode") or "proposal-only")
```

- [ ] **Step 3: Upgrade log level in `_gather_extra_context`**

Line 169:
```python
        _log.debug("extra context gather failed  err=%s", exc)
```
Change to:
```python
        _log.warning("extra context gather failed  err=%s", exc)
```

- [ ] **Step 4: Verify compilation**

```bash
python -m py_compile workers/task_handlers/project_propose.py && echo OK
```

Expected: `OK`

---

## Task 5: Final verification

- [ ] **Step 1: Compile all changed files**

```bash
python -m py_compile \
  workers/task_handlers/project_review.py \
  workers/task_handlers/project_revise.py \
  workers/project_autonomy.py \
  workers/task_handlers/project_propose.py && echo ALL OK
```

Expected: `ALL OK`

- [ ] **Step 2: Run existing tests if present**

```bash
cd /Users/michaelturner/PycharmProjects/JeffGPT-Harness
python -m pytest tests/ -x -q 2>&1 | tail -20
```

- [ ] **Step 3: Check for remaining private import violations**

```bash
grep -rn "from workers.project_autonomy import.*_get_setting\|from workers.project_autonomy import.*_check_queue" \
  workers/ && echo "FOUND violations" || echo "Clean"
```

Expected: `Clean`

---

> **Note — Fix #1 deferred:** Extracting `_get_gitea_client` / `_get_repo_coords` to `tools/project/gitea.py` touches 5+ files and requires a separate change per CLAUDE.md hard rules.
