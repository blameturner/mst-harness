# Project Agent Kanban Task Types — Design Spec

**Date:** 2026-05-05

## Overview

Three new `llm_bound=True` task types for the Project agent. All state flows through `task_list.input_payload` / `output_payload`. Project identity resolves via existing `projects` and `gitea_connections` tables. Repo content for proposals uses `project_snapshots` / `project_snapshot_files`.

---

## 1. `project_feature`

Coding agent implements a described feature on a Gitea branch and opens a PR.

**Input payload**
```json
{
  "project_id": 42,
  "feature_description": "Add OAuth2 login via GitHub",
  "branch_name": "feat/oauth2-github",
  "parent_task_id": null,
  "feedback": ""
}
```
`parent_task_id` and `feedback` are optional — set by `project_review` when re-queuing after rejection.

**Output payload**
```json
{
  "pr_id": 7,
  "pr_url": "http://gitea/owner/repo/pulls/7",
  "branch_name": "feat/oauth2-github"
}
```

**Handler:** `workers/task_handlers/project_feature.py`

Steps:
1. Fetch project row + Gitea connection via `NocodbClient`.
2. Ensure branch exists (create if absent) via `GiteaClient`.
3. Build feature prompt (feature_description + optional feedback) → run `CodeAgent` in `execute` mode against the project.
4. Open PR via `GiteaClient.create_pull_request(head=branch_name, base=default_branch)`.

---

## 2. `project_review`

PO/Architect reviews a feature branch PR against the original feature description. Approves + merges to base branch, or rejects + spawns a new `project_feature` child task with feedback.

**Input payload**
```json
{
  "project_id": 42,
  "pr_id": 7,
  "feature_description": "Add OAuth2 login via GitHub"
}
```

**Output payload — approved**
```json
{
  "decision": "approved",
  "feedback": "Looks good."
}
```

**Output payload — rejected**
```json
{
  "decision": "rejected",
  "feedback": "Missing error handling on token exchange.",
  "child_task_id": 88
}
```

**Handler:** `workers/task_handlers/project_review.py`

Steps:
1. Fetch PR diff via `GiteaClient.get_pull_request_diff`.
2. LLM review prompt: diff + feature_description → structured JSON `{decision, feedback}`.
3. If approved: `GiteaClient.merge_pull_request(pr_id, merge_style="merge")` (merges to PR base branch).
4. If rejected: `kanban.submit("project_feature", {project_id, feature_description, branch_name, feedback, parent_task_id=task["Id"]})`.

---

## 3. `project_propose`

PO/Architect scans the latest project snapshot and proposes the next feature. Spawns a `project_feature` task: `status=ready` if `autonomy=full`, `status=blocked` if `autonomy=proposal-only`.

**Input payload**
```json
{
  "project_id": 42,
  "autonomy": "full"
}
```
`autonomy` is `"full"` or `"proposal-only"`.

**Output payload**
```json
{
  "feature_description": "Extract auth into its own module",
  "branch_name": "feat/auth-extraction",
  "child_task_id": 91,
  "child_status": "ready"
}
```

**Handler:** `workers/task_handlers/project_propose.py`

Steps:
1. Fetch project row via `NocodbClient`.
2. Get latest snapshot: `list_project_snapshots(project_id, limit=1)`.
3. Build file manifest: `list_project_snapshot_files(snapshot_id)` → `get_project_file_version` per file.
4. LLM prompt: file manifest + project description → structured JSON `{feature_description, branch_name}`.
5. `kanban.submit("project_feature", payload, status="ready"|"blocked")`.

Uses `kanban.submit(..., status="blocked")` for `proposal-only` — `submit` accepts an optional `status` kwarg (default `"ready"`).

---

## GiteaClient additions

Two new methods on `infra/gitea_client.py`:

```python
def create_pull_request(self, owner, repo, title, head, base, body="") -> dict:
    # POST /repos/{owner}/{repo}/pulls

def merge_pull_request(self, owner, repo, pr_id, merge_style="merge") -> None:
    # POST /repos/{owner}/{repo}/pulls/{index}/merge

def get_pull_request_diff(self, owner, repo, pr_id) -> str:
    # GET /repos/{owner}/{repo}/pulls/{index}.diff
```

---

## `app/lifespan.py` additions

```python
from workers.task_handlers import project_feature as _project_feature_handler
from workers.task_handlers import project_review as _project_review_handler
from workers.task_handlers import project_propose as _project_propose_handler

_kanban.register("project_feature", _project_feature_handler.handle, llm_bound=True)
_kanban.register("project_review",  _project_review_handler.handle,  llm_bound=True)
_kanban.register("project_propose", _project_propose_handler.handle, llm_bound=True)
```

---

## Files touched

| File | Change |
|---|---|
| `workers/task_handlers/project_feature.py` | new |
| `workers/task_handlers/project_review.py` | new |
| `workers/task_handlers/project_propose.py` | new |
| `infra/gitea_client.py` | add 3 methods |
| `app/lifespan.py` | 6 lines (3 imports + 3 registers) |