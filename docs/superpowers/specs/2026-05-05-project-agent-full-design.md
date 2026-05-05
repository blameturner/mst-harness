# Project Agent — Full System Design

_Date: 2026-05-05. Status: pending implementation review._

---

## Scope

This document covers the full implementation of the three project agent task handlers (`project_feature`, `project_review`, `project_propose`), plus the supporting infrastructure they depend on:

- Filesystem tools with path-validation security boundary
- Repo knowledge layer (persisted summary + structured index in NocoDB)
- `project_index` task type
- Gitea PR/branch methods
- Per-repo settings extensions (base branch, model overrides, architectural rules)
- Trigger endpoints (`POST /tasks/projects/{id}/trigger/feature|propose`)
- Shared review logic extracted for reuse by both the API route and the background handler

---

## 1. NocoDB Schema Changes

### New table: `project_repo_summaries`

| Column | Type | Notes |
|---|---|---|
| `Id` | int PK | auto |
| `project_id` | int | FK → projects |
| `content` | long text | AI-generated prose summary of the repo |
| `last_indexed_at` | datetime | Updated after each `project_index` run |
| `model_used` | varchar | Model that produced this summary |

One row per project. Upserted on `(project_id)`. Follows the optional-table pattern — accessed via `_safe_post`/`_safe_list` with `_has_table` guards so startup never crashes if the table is absent.

### New table: `project_repo_index`

| Column | Type | Notes |
|---|---|---|
| `Id` | int PK | auto |
| `project_id` | int | FK → projects |
| `path` | varchar | Relative file path |
| `purpose` | text | One-sentence AI description of the file |
| `key_exports` | text | JSON array of function/class names |
| `dependencies` | text | JSON array of paths this file imports |
| `last_indexed_at` | datetime | Per-entry freshness |

One row per file per project. Upserted on `(project_id, path)`. Same optional-table pattern.

### Why new tables rather than existing ones

`projects.system_note` and `projects.description` are user-set; conflating AI-generated content with user instructions breaks update-cadence semantics. `project_file_versions` (storing summary as a magic file path) would pollute the user-visible file tree and expose it to accidental deletion by the Coder. `project_symbols` is static-analysis output (AST names/lines), not AI-generated per-file purpose descriptions — different query shape, different update trigger.

---

## 2. Per-Repo Settings Extensions

Stored in the `settings` table under agent key `project:{project_id}`. These join the existing autonomy fields (`autonomy_mode`, `max_tasks_per_hour`, etc.).

| Key | Type | Default | Description |
|---|---|---|---|
| `model_agent` | str | `t2_coder` | Model role for `project_feature` (Coder) |
| `model_po` | str | `t1_primary` | Model role for `project_review` + `project_propose` (Architect/PO) |
| `staging_branch` | str | `staging` | Gitea base branch for PRs and merges |
| `architectural_rules` | str | `""` | Global rules text; repo-level `ARCHITECTURE.md` takes precedence if present |

`GET /tasks/projects/{id}/autonomy` and `PUT /tasks/projects/{id}/autonomy` extended to include these four fields. Existing `AutonomySettings` Pydantic model gains the new optional fields — backward-compatible.

---

## 3. Path Validator

**Module:** `tools/project/path_guard.py`

```python
def validate_path(
    repo_root: str | Path,
    requested: str,
    denylist: list[str] | None = None,
) -> Path
```

Rules enforced in order — fails fast, never falls back:

1. Reject absolute `requested` paths.
2. Join `repo_root / requested` and resolve all symlinks.
3. Confirm the resolved path starts with the resolved `repo_root`. Rejects any `..` traversal or symlink that escapes the root.
4. Match against default denylist: `.git/**`, `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, plus any caller-supplied patterns.

On failure: `ValueError` naming the path and the violated rule. Never partial execution.

**Test module:** `tests/tools/project/test_path_guard.py`

Eight required cases:
1. Absolute path input → `ValueError("absolute path rejected")`
2. `../escape` traversal → `ValueError("path escapes repo root")`
3. Symlink that resolves outside root → `ValueError("symlink escapes repo root")`
4. `.git/config` → `ValueError("path matches denylist: .git/**")`
5. `secrets.key` → `ValueError("path matches denylist: *.key")`
6. Custom per-repo denylist addition blocks matching path
7. Valid nested path → returns resolved `Path`
8. Valid non-existent path → returns resolved `Path` (existence not required for validation)

---

## 4. Tool Modules

### `tools/project/fs_tools.py`

All tools call `validate_path()` before any filesystem action. All write tools log at INFO: `task_id`, repo, operation, path.

**Read tools — available to all three roles (Coder, Architect, PO):**

| Function | Signature | Returns |
|---|---|---|
| `read_file` | `(db, project_id, path) → str` | Content of current version |
| `read_directory` | `(db, project_id, path, depth=1) → list[dict]` | `[{path, kind, size_bytes}]` |
| `read_repo_tree` | `(db, project_id, max_depth=3) → dict` | Nested structure, no content, with sizes |
| `search_repo` | `(db, project_id, query, glob_pattern=None) → list[dict]` | `[{path, line, snippet}]` |

**Write tools — Coder only:**

| Function | Signature | Returns |
|---|---|---|
| `create_file` | `(db, project_id, path, content, task_id) → dict` | Error if path exists |
| `edit_file` | `(db, project_id, path, mode, content, content_hash=None, task_id) → dict` | Wraps `apply_file_fences()`; modes: `replace`, `append`, `patch` |
| `delete_file` | `(db, project_id, path, task_id) → dict` | Archives via `db.archive_project_file()` |
| `rename_file` | `(db, project_id, old_path, new_path, task_id) → dict` | Write new + archive old |
| `move_file` | `(db, project_id, src, dst, task_id) → dict` | Same as rename; separate name for directory-move semantics |
| `create_directory` | `(db, project_id, path) → dict` | Writes `.gitkeep` placeholder at `path/.gitkeep` |
| `delete_directory` | `(db, project_id, path, recursive=False, task_id) → dict` | Archives all files under path; rejects non-empty unless `recursive=True` |

### `tools/project/knowledge.py`

| Function | Signature | Notes |
|---|---|---|
| `read_repo_summary` | `(db, project_id) → str` | From `project_repo_summaries`; returns `""` if table absent or no row |
| `write_repo_summary` | `(db, project_id, content, section=None, model_used="") → dict` | Full replace or named-section replace |
| `read_repo_index` | `(db, project_id, path_filter=None) → list[dict]` | From `project_repo_index`; optional glob on path |
| `write_repo_index` | `(db, project_id, entries: list[dict]) → dict` | Upsert on `(project_id, path)` |

### `tools/project/review.py`

Shared logic called by both `app/routers/projects_ai.py` (`ai_review` route) and `workers/task_handlers/project_review.py`.

| Function | Signature | Notes |
|---|---|---|
| `build_review_context` | `(diff, feature_description, repo_summary, architecture_rules) → str` | Assembles the review prompt |
| `call_reviewer_model` | `(context, model, db, org_id) → dict` | Returns `{verdict, rationale, concerns, suggestions}` |
| `parse_structured_verdict` | `(raw: dict) → Literal["approve","reject","revise"]` | Validates and extracts verdict |

The `ai_review` route calls `build_review_context` + `call_reviewer_model`, renders result as markdown, returns to client. The `project_review` handler calls the same two functions, then acts on `parse_structured_verdict`.

---

## 5. Gitea Client Extensions

**File:** `infra/gitea_client.py`

New methods added to `GiteaClient`:

| Method | Signature | Gitea API |
|---|---|---|
| `create_branch` | `(repo, branch_name, from_branch) → dict` | `POST /repos/{owner}/{repo}/branches` |
| `list_prs` | `(repo, state="open") → list[dict]` | `GET /repos/{owner}/{repo}/pulls` |
| `create_pr` | `(repo, title, head, base, body="") → dict` | `POST /repos/{owner}/{repo}/pulls` |
| `get_pr` | `(repo, pr_id) → dict` | `GET /repos/{owner}/{repo}/pulls/{index}` |
| `get_pr_diff` | `(repo, pr_id) → str` | `GET /repos/{owner}/{repo}/pulls/{index}.diff` |
| `merge_pr` | `(repo, pr_id, merge_method="merge", message="") → dict` | `POST /repos/{owner}/{repo}/pulls/{index}/merge` |
| `close_pr` | `(repo, pr_id) → dict` | `PATCH /repos/{owner}/{repo}/pulls/{index}` with `state=closed` |
| `list_issues` | `(repo, state="open") → list[dict]` | `GET /repos/{owner}/{repo}/issues` |

All methods follow the existing `GiteaClient` pattern: `self._get(path)` / `self._post(path, body)` / `self._patch(path, body)`, raise on non-2xx.

---

## 6. Handler Specifications

### `project_feature` — Coder

**File:** `workers/task_handlers/project_feature.py`

```python
class ProjectFeatureInput(BaseModel):
    project_id: int
    feature_description: str
    branch_name: str
    architect_context: str | None = None   # injected by propose if PO-spawned
    revision_count: int = 0                # incremented on each revise loop
    parent_task_id: int | None = None      # set by project_review on revise spawn
```

Output written to `output_payload`:
```json
{
    "status": "done|failed",
    "pr_id": 123,
    "branch_name": "feature/my-branch",
    "commit_shas": ["abc123"],
    "change_summary": "Added X, modified Y",
    "tokens_used": 4200
}
```

Execution sequence:
1. `check_autonomy(db, task)` — autonomy guardrails.
2. `read_repo_summary(db, project_id)` — inject into CodeAgent system context.
3. Resolve `staging_branch` from agent settings (default `"staging"`).
4. `GiteaClient.create_branch(branch_name, from_branch=staging_branch)`.
5. `CodeAgent(model=_model(task, "project_agent"), mode="execute", project_id=project_id, interactive_fs=True)`.
6. Call `agent.run_job(user_message=feature_description)` with `architect_context` prepended to the system prompt if non-empty (newline-separated, not concatenated into the user message).
7. `GiteaClient.create_pr(head=branch_name, base=staging_branch)`.
8. Return output with `tokens_used` from agent result.
9. Optionally enqueue `project_index` with `trigger="post_feature"` (non-blocking; fire-and-forget).

### `project_review` — Architect

**File:** `workers/task_handlers/project_review.py`

```python
class ProjectReviewInput(BaseModel):
    project_id: int
    pr_id: int
    feature_description: str
    revision_count: int = 0
```

Output:
```json
{
    "status": "done|failed",
    "verdict": "approve|reject|revise",
    "rationale": "...",
    "merge_sha": "abc123",
    "child_task_id": 456,
    "revision_count": 0,
    "tokens_used": 3100
}
```

Execution sequence:
1. `check_autonomy(db, task)`.
2. `read_repo_summary(db, project_id)`.
3. Read `ARCHITECTURE.md` via `read_file` if present; fall back to `get_agent_setting("project:{project_id}", "architectural_rules")`.
4. `GiteaClient.get_pr_diff(pr_id)` to fetch unified diff.
5. `build_review_context(diff, feature_description, repo_summary, architectural_rules)`.
6. `call_reviewer_model(context, model=_model(task, "project_po"))`.
7. `parse_structured_verdict(result)`.
8. **approve** → `GiteaClient.merge_pr(pr_id, base=staging_branch)`, enqueue `project_index(trigger="post_merge")`.
9. **reject** → `GiteaClient.close_pr(pr_id)`.
10. **revise** → if `revision_count >= 2`, treat as reject. Else: `GiteaClient.close_pr(pr_id)`, enqueue new `project_feature` with `revision_count + 1`, reviewer feedback appended to `architect_context`.

### `project_propose` — PO

**File:** `workers/task_handlers/project_propose.py`

```python
class ProjectProposeInput(BaseModel):
    project_id: int
```

Output:
```json
{
    "status": "done|failed|blocked",
    "proposals": [
        {"title": "...", "description": "...", "rationale": "...", "scope": "small|medium|large", "task_id": 789}
    ],
    "queued": 2,
    "tokens_used": 1800
}
```

Execution sequence:
1. `check_autonomy(db, task)`.
2. `read_repo_summary(db, project_id)`. If empty → enqueue `project_index(trigger="pre_propose")`, return `status="blocked", error="no summary available, indexing first"`.
3. Read recent commits via `GiteaClient.list_commits()`, open issues via `GiteaClient.list_issues()`, README/ARCHITECTURE/TODO files.
4. Check queue depth — if at `max_queued_proposals` limit, return `status="done", queued=0, proposals=[]`.
5. Prompt PO model (role from `_model(task, "project_po")`) with all context + "next valuable change" rubric. Model returns list of proposals.
6. Based on `autonomy_mode`:
   - `"proposal-only"` → insert `project_feature` tasks with `status="blocked"`, `error="awaiting user approval"`.
   - `"full"` → insert with `status="ready"`.
7. Return proposal list with created task IDs.

---

## 7. `project_index` Task Type

**File:** `workers/task_handlers/project_index.py`

```python
class ProjectIndexInput(BaseModel):
    project_id: int
    trigger: Literal["manual", "post_merge", "post_feature", "pre_propose", "cron"]
```

Output:
```json
{
    "status": "done|failed",
    "files_indexed": 42,
    "summary_chars": 3200,
    "tokens_used": 8500
}
```

Execution sequence:
1. `read_repo_tree(db, project_id, max_depth=5)` — file list with sizes.
2. Batch files (by token budget); for each batch: read content, generate `{purpose, key_exports, dependencies}` per file using `_model(task, "project_po")` (larger model for quality).
3. Accumulate per-file data; generate full repo prose summary.
4. `write_repo_index(db, project_id, entries)`.
5. `write_repo_summary(db, project_id, content, model_used=model_key)`.

**Registration** (`app/lifespan.py`):
```python
from workers.task_handlers import project_index as _project_index_handler
_kanban.register("project_index", _project_index_handler.handle, llm_bound=True)
```

Loop A is appropriate. `project_index` is low-priority background work; serialisation is desirable. Revisit if real data shows runtime > 5 min for common repos.

---

## 8. API Extensions

### Extended autonomy settings — `app/routers/tasks.py`

`AutonomySettings` gains four new optional fields:

```python
class AutonomySettings(BaseModel):
    # existing
    autonomy_mode: str | None = None
    max_tasks_per_hour: int | None = None
    max_queued_proposals: int | None = None
    max_daily_tokens: int | None = None
    consecutive_failure_backoff: bool | None = None
    # new
    model_agent: str | None = None          # role key for Coder
    model_po: str | None = None             # role key for Architect/PO
    staging_branch: str | None = None       # Gitea base branch
    architectural_rules: str | None = None  # global review rules text
```

`GET /tasks/projects/{id}/autonomy` merges these from the settings table with config defaults and returns the full picture. `PUT /tasks/projects/{id}/autonomy` persists any non-null fields.

### Trigger endpoints — `app/routers/tasks.py`

```
POST /tasks/projects/{project_id}/trigger/feature
```
Body:
```json
{
    "feature_description": "Add rate-limit middleware to all write endpoints",
    "branch_name": "feature/rate-limit-writes",
    "architect_context": null,
    "model": null
}
```
Creates a `project_feature` task with `agent="project:{project_id}"`, `created_by="api"`, status always `"ready"`. Manual triggers bypass the `autonomy_mode` gate (that gate controls autonomous proposal behaviour, not human-initiated work). Rate limits from `check_autonomy` still apply at execution time. Returns the created task row.

```
POST /tasks/projects/{project_id}/trigger/propose
```
Body: `{}` (no fields required).
Creates a `project_propose` task. Returns the created task row.

Both endpoints auto-set `agent`, `created_by`, and (for feature) enforce the proposal-only gating.

---

## 9. File Map

```
New files:
  tools/project/__init__.py
  tools/project/path_guard.py
  tools/project/fs_tools.py
  tools/project/knowledge.py
  tools/project/review.py
  workers/task_handlers/project_index.py
  tests/tools/project/__init__.py
  tests/tools/project/test_path_guard.py

Edited files:
  infra/gitea_client.py          add 8 PR/branch methods
  infra/nocodb_client.py         add 4 knowledge-layer accessors
  workers/task_handlers/project_feature.py   implement stub
  workers/task_handlers/project_review.py    implement stub
  workers/task_handlers/project_propose.py   implement stub
  app/lifespan.py                add project_index registration
  app/routers/tasks.py           extend AutonomySettings + add trigger endpoints
  app/routers/projects_ai.py     extract review logic into tools/project/review.py
```

Total: 8 new files, 8 edited files.

---

## 10. What is NOT in scope

- Frontend UI components (no frontend lives in this repo; the trigger and settings endpoints are the contract)
- Huey migration for large-repo indexing (register on Loop A; revisit with data)
- Webhook-driven auto-trigger on Gitea push (listed as broken/stub in the existing audit)
- Conflict resolution on Gitea pull (separate work)