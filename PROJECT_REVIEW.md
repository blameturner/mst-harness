# Project Module Review

> **Principle under test:** Each task handler does one thing — get a task,
> invoke a model or perform one side-effect, return the result, and write to
> DB. Shared concerns belong in `tools/`, not in a handler that other handlers
> import from.

---

## Executive Summary

The project pipeline handlers (`project_propose → project_feature →
project_review → project_human_review / project_revise → project_index`) are
thin and follow the principle. Task flow is correct, kanban registration is
complete, and the autonomy guardrails are well-isolated.

Two structural problems undermine reliability in production:

1. `_get_gitea_client` and `_get_repo_coords` are defined in
   `project_feature.py` but imported as private helpers by three other
   handlers. The boundary is broken: one handler's internals are the API of
   the whole pipeline.

2. There was no global Gitea token in settings, so all `project_feature`,
   `project_review`, and `project_revise` tasks silently returned
   `status=failed` with "no Gitea connection configured" unless the
   `gitea_connections` table was pre-populated per-org via the `/gitea`
   router. This is now fixed (see §4 below).

---

## 1. Call Map

```
project_propose (PO role — picks features)
  └── reads repo_summary; reads gitea commits/issues
  └── calls model → JSON list of proposals
  └── kanban.submit("project_feature", ..., status="blocked"|"ready")

project_feature (Coder role — implements feature)
  └── reads repo_summary
  └── gitea.create_branch()
  └── CodeAgent.run_job()
  └── push_files_to_gitea()
  └── gitea.create_pr()
  └── kanban.submit("project_index", ...)    [post-feature index refresh]
  └── kanban.submit("project_review" | "project_human_review", ...)

project_review (Architect role — reviews PR diff)
  └── gitea.get_pr_diff()
  └── call_reviewer_model()
  └── verdict: approve → gitea.merge_pr() → kanban.submit("project_index")
             reject → gitea.close_pr()
             revise → gitea.close_pr() → kanban.submit("project_feature", revision_count+1)

project_human_review (zero LLM — routes on feedback presence)
  └── human_feedback present → kanban.submit("project_revise")
  └── no feedback           → kanban.submit("project_review")

project_revise (Coder role — applies human patch)
  └── reads changed files from NocoDB
  └── calls model → JSON patch list
  └── db.write_project_file_version() per patch
  └── push_files_to_gitea()
  └── kanban.submit("project_review" | "project_human_review", revision_count+1)

project_index (Architect role — regenerates repo summary)
  └── list_project_files()
  └── _index_batch() × N    [MODEL CALL per batch]
  └── write_repo_index()
  └── _generate_summary()   [MODEL CALL]
  └── write_repo_summary()
```

---

## 2. Problems

### 1. Handler boundary violation: private helpers shared across handlers

`project_feature.py` defines `_get_gitea_client()` and `_get_repo_coords()`
as module-private functions (prefixed `_`). Three other handlers import them:

```python
# project_review.py, project_revise.py, project_propose.py
from workers.task_handlers.project_feature import _get_gitea_client, _get_repo_coords
```

This is the wrong boundary. Private helpers in a handler are not a shared API.
Any change to `_get_gitea_client` now has hidden callers across the pipeline.

**Fix:** extract to `tools/project/gitea.py` (public, tested independently).
Currently surfaced as an issue; not fixed here to avoid touching 5+ files.

---

### 2. `project_propose` imports private functions from `project_autonomy`

```python
from workers.project_autonomy import check_autonomy, _get_setting, _check_queue_depth
```

`_get_setting` and `_check_queue_depth` are private to `project_autonomy`.
Importing them directly bypasses the module's interface. If the internals
change, `project_propose` breaks silently.

**Fix:** expose `_get_setting` as `get_project_setting` and `_check_queue_depth`
as a public guard, or inline the needed logic in the caller.

---

### 3. `_get_repo_coords` parsed `owner/repo@ref` incorrectly

```python
# old — returns ("myuser", "myrepo@main") instead of ("myuser", "myrepo")
parts = origin.rstrip("/").split("/")
return parts[-2], parts[-1]
```

The `@ref` suffix was included in the repo name, which would cause all Gitea
API calls (`create_branch`, `get_pr_diff`, `merge_pr`, etc.) to 404. The
gitea router already uses the correct regex pattern for the same field.

**Fixed:** now uses `re.match(r"^([^/]+)/([^@]+)@", origin)` consistent with
the rest of the codebase. Also adds a `None` guard for missing projects.

---

### 4. No global Gitea token in settings (now fixed)

`_get_gitea_client` previously returned `None` when no org-scoped
`gitea_connections` row existed, causing every `project_feature`,
`project_review`, and `project_revise` task to return:

```json
{"status": "failed", "error": "no Gitea connection configured"}
```

The `/gitea/connection` endpoint manages org-scoped connections, but required
knowing the `org_id` and was not visible in the settings router alongside
OpenRouter.

**Fixed:** `infra/settings.py` now exposes `get_gitea_default()` / `upsert_gitea_default()`.
`_get_gitea_client` falls back to the global token when no org-scoped row
exists. The settings router now has:

```
GET    /settings/connections/gitea
PUT    /settings/connections/gitea    { base_url, token, username }
DELETE /settings/connections/gitea
POST   /settings/connections/gitea/test
```

---

### 5. Silent `except: pass` on `project_index` enqueue in `_do_approve`

```python
# project_review.py::_do_approve
try:
    _kanban.submit(db, "project_index", ...)
except Exception:
    pass  # repo index silently never updates
```

Same pattern identified in the research pipeline. A failed `project_index`
enqueue means the repo summary becomes stale after every approved feature,
and the next `project_propose` will read an outdated summary.

**Fix:** log at `warning` level at minimum. Optionally include the `project_index`
task ID in the approve response so the caller can verify it was queued.

---

### 6. `revision_count` shared across two different revision loops

`_MAX_REVISIONS = 2` in `project_review.py` (CodeAgent cycles) and
`_MAX_REVISE_CYCLES = 2` in `project_revise.py` (human annotation cycles)
both increment the same `revision_count` in the payload. A human revision
at `revision_count=1` pushes it to `2`, which causes the next
`project_review` to immediately reject regardless of quality:

```python
# project_review.py
if verdict == "reject" or revision_count >= _MAX_REVISIONS:
    return _do_reject(...)
```

This is probably unintended. The two caps should use separate payload fields
(`agent_revision_count` vs `human_revision_count`) or one shared cap that
is clearly documented.

---

### 7. `project_propose._gather_extra_context` failure logged at DEBUG

```python
except Exception as exc:
    _log.debug("extra context gather failed  err=%s", exc)
```

A broken Gitea token (or unreachable server) is invisible in production logs
unless debug logging is enabled. This should be `_log.warning`.

---

## 3. Assessment vs. Principle

| Handler | Assessment |
|---|---|
| `project_propose` | Clean model call. Issues: private import from autonomy, debug-level gitea failure. |
| `project_feature` | Correct. Defines helpers that others import (boundary violation). |
| `project_review` | Correct routing. Silent exception on `project_index` enqueue. |
| `project_human_review` | Clean — zero LLM, pure routing. No issues. |
| `project_revise` | Correct. Shares `revision_count` field ambiguously with `project_review`. |
| `project_index` | Correct. Batch model calls. Raises on model error (good). |
| `project_autonomy` | Clean isolation. Leaks private helpers to `project_propose`. |

---

## 4. Recommended Changes (priority order)

**1. Move `_get_gitea_client` and `_get_repo_coords` to `tools/project/gitea.py`** (medium risk)
Break the cross-handler private import. All four callers import from `tools/project/gitea`.
5+ files, needs separate change.

**2. Fix silent `except: pass` in `_do_approve`** (low risk)
```python
except Exception as exc:
    _log.warning("project_review: project_index enqueue failed  err=%s", exc)
```

**3. Separate `agent_revision_count` from `human_revision_count`** (low risk)
Add a new `human_revision_count` field to the payload so the two caps don't interfere.

**4. Expose `_get_setting` and `_check_queue_depth` as public in `project_autonomy`** (low risk)
Rename to remove the `_` prefix; update `project_propose` to use the public names.

**5. Upgrade `_gather_extra_context` failure log to warning** (low risk, one line)
