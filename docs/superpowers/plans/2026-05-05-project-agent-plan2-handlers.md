# Project Agent — Plan 2: Handlers

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the four kanban handlers (project_feature, project_review, project_propose, project_index) and the shared review logic they depend on.

**Architecture:** Each handler is a thin orchestration layer: validate inputs → call autonomy guards → call domain tools → write output. Shared review logic lives in `tools/project/review.py` and is called by both the kanban handler and the existing `ai_review` API route. No new AI model wiring — all calls go through the existing `resolve_model_entry` / `BaseAgent` pattern already in the codebase.

**Prerequisites:** Plan 1 complete (path_guard, fs_tools, knowledge, gitea PR methods, NocoDB accessors all merged).

**Tech Stack:** Python 3.11+, existing `workers.code.agent.CodeAgent`, `infra.config.resolve_model_entry`, Gitea REST, NocoDB

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `tools/project/review.py` | Create | Shared review logic: build prompt, call model, parse verdict |
| `workers/task_handlers/project_index.py` | Create | Indexes repo into NocoDB knowledge layer |
| `workers/task_handlers/project_feature.py` | Replace stub | Coder: branch → CodeAgent → PR |
| `workers/task_handlers/project_review.py` | Replace stub | Architect: diff → review → merge/reject/revise |
| `workers/task_handlers/project_propose.py` | Replace stub | PO: summary → propose → queue feature tasks |
| `app/routers/projects_ai.py` | Modify | Extract review call into `tools/project/review.py` |

---

## Helper: model resolver for project handlers

Every handler uses a `_model(task, config_key)` helper. Since all three existing stubs already define this function identically, **do not duplicate it**. Instead, add it to `tools/project/__init__.py` and import it:

```python
# tools/project/__init__.py
"""Project agent tooling."""
from __future__ import annotations


def resolve_agent_model(task: dict, config_key: str) -> str:
    """Resolve model role for a project task.

    Precedence: task.model column → features.project.models.<config_key>.role → hardcoded fallback.
    """
    override = str(task.get("model") or "").strip()
    if override:
        return override
    from infra.config import get_feature
    entry = (get_feature("project", "models") or {}).get(config_key) or {}
    defaults = {"project_agent": "t2_coder", "project_po": "t1_primary"}
    return str(entry.get("role") or defaults.get(config_key, "t2_coder"))
```

Update all three existing stub files (`project_feature.py`, `project_review.py`, `project_propose.py`) to import from `tools.project` instead of defining `_model` locally. Do this as the first commit in this plan.

---

## Task 1: Consolidate model resolver + shared review logic

**Files:**
- Modify: `tools/project/__init__.py`
- Create: `tools/project/review.py`
- Modify: `workers/task_handlers/project_feature.py` (remove local `_model`)
- Modify: `workers/task_handlers/project_review.py` (remove local `_model`)
- Modify: `workers/task_handlers/project_propose.py` (remove local `_model`)

- [ ] **Step 1: Add `resolve_agent_model` to `tools/project/__init__.py`**

Replace the file with:

```python
"""Project agent tooling."""
from __future__ import annotations


def resolve_agent_model(task: dict, config_key: str) -> str:
    """Resolve model role for a project task.

    Precedence: task.model column → features.project.models.<config_key>.role → hardcoded fallback.
    """
    override = str(task.get("model") or "").strip()
    if override:
        return override
    from infra.config import get_feature
    entry = (get_feature("project", "models") or {}).get(config_key) or {}
    defaults = {"project_agent": "t2_coder", "project_po": "t1_primary"}
    return str(entry.get("role") or defaults.get(config_key, "t2_coder"))
```

- [ ] **Step 2: Update each stub to import instead of redefine**

In `workers/task_handlers/project_feature.py`, replace the local `_model` function with:

```python
from tools.project import resolve_agent_model as _model_for
```

And update the call site from `_model(task)` to `_model_for(task, "project_agent")`.

Apply the same change to `project_review.py` (config_key `"project_po"`) and `project_propose.py` (config_key `"project_po"`).

- [ ] **Step 3: Write `tools/project/review.py`**

```python
"""Shared review logic used by both the AI review API route and the project_review handler.

The API route calls build_review_context + call_reviewer_model and renders markdown.
The handler calls the same two functions and then acts on parse_verdict.
"""
from __future__ import annotations

import logging
from typing import Literal

_log = logging.getLogger("project.review")

REVIEW_RUBRIC = """
You are a senior software architect reviewing a pull request. Evaluate the diff against:
1. Does it implement the stated feature description correctly and completely?
2. Does it follow the architectural rules and conventions?
3. Are there security issues, N+1 queries, or obvious bugs?
4. Is the code consistent with the existing codebase style?

Respond with valid JSON only — no markdown, no explanation outside the JSON:
{
  "verdict": "approve" | "reject" | "revise",
  "rationale": "<one paragraph>",
  "concerns": ["<specific concern>", ...],
  "suggestions": ["<actionable suggestion if verdict is revise>", ...]
}

verdict meanings:
- approve: merge as-is
- reject: fundamental problem; close the PR, do not retry
- revise: fixable issues; provide specific feedback so the Coder can iterate
"""


def build_review_context(
    diff: str,
    feature_description: str,
    repo_summary: str,
    architectural_rules: str,
) -> str:
    parts = [
        "## Feature Description\n" + feature_description.strip(),
    ]
    if repo_summary.strip():
        parts.append("## Repo Summary\n" + repo_summary.strip())
    if architectural_rules.strip():
        parts.append("## Architectural Rules\n" + architectural_rules.strip())
    parts.append("## Diff\n```diff\n" + diff.strip() + "\n```")
    return "\n\n".join(parts)


def call_reviewer_model(
    context: str,
    model_role: str,
    org_id: int = 0,
) -> dict:
    """Call the reviewer model and return the parsed JSON verdict dict.

    Raises ValueError if the model response cannot be parsed as the expected schema.
    """
    import json
    from infra.config import resolve_model_entry
    from shared.llm import call_llm_sync  # existing sync LLM helper

    entry = resolve_model_entry(model_role)
    if not entry:
        raise ValueError(f"model role not found in catalog: {model_role!r}")

    prompt = REVIEW_RUBRIC + "\n\n" + context
    raw = call_llm_sync(
        url=entry["url"],
        model=entry.get("model_id", ""),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
        temperature=0.1,
    )
    return _parse_verdict_json(raw)


def parse_verdict(result: dict) -> Literal["approve", "reject", "revise"]:
    v = str(result.get("verdict") or "").lower().strip()
    if v not in ("approve", "reject", "revise"):
        raise ValueError(f"unexpected verdict value: {v!r}")
    return v  # type: ignore[return-value]


def _parse_verdict_json(raw: str) -> dict:
    import json, re
    # Strip any accidental markdown fencing
    cleaned = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\n?```$", "", cleaned.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"reviewer model returned non-JSON: {raw[:300]!r}") from exc
    if "verdict" not in data:
        raise ValueError(f"reviewer response missing 'verdict' key: {data}")
    return data
```

> **Note on `call_llm_sync`:** This assumes a shared synchronous LLM call utility exists in `shared/llm.py`. Before implementing, check: `grep -rn "def call_llm_sync\|def call_llm" shared/ infra/ workers/ --include="*.py"`. If the utility doesn't exist or has a different name, use the pattern from `tools/research/agent.py` or `workers/code/agent.py` and adapt accordingly.

- [ ] **Step 4: Write tests for review logic**

Create `tests/tools/project/test_review.py`:

```python
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.project.review import (
    build_review_context,
    parse_verdict,
    _parse_verdict_json,
)


def test_build_review_context_includes_all_sections():
    ctx = build_review_context("+ new line", "Add auth", "Python FastAPI app", "No global state")
    assert "## Feature Description" in ctx
    assert "## Repo Summary" in ctx
    assert "## Architectural Rules" in ctx
    assert "## Diff" in ctx
    assert "Add auth" in ctx


def test_build_review_context_omits_empty_summary():
    ctx = build_review_context("+ new line", "Add auth", "", "")
    assert "## Repo Summary" not in ctx
    assert "## Architectural Rules" not in ctx


def test_parse_verdict_approve():
    assert parse_verdict({"verdict": "approve"}) == "approve"


def test_parse_verdict_reject():
    assert parse_verdict({"verdict": "reject"}) == "reject"


def test_parse_verdict_revise():
    assert parse_verdict({"verdict": "revise"}) == "revise"


def test_parse_verdict_invalid_raises():
    with pytest.raises(ValueError, match="unexpected verdict"):
        parse_verdict({"verdict": "maybe"})


def test_parse_verdict_json_strips_markdown_fencing():
    raw = '```json\n{"verdict": "approve", "rationale": "looks good"}\n```'
    result = _parse_verdict_json(raw)
    assert result["verdict"] == "approve"


def test_parse_verdict_json_raises_on_non_json():
    with pytest.raises(ValueError, match="non-JSON"):
        _parse_verdict_json("sorry, I cannot review this")
```

- [ ] **Step 5: Run**

```bash
python -m pytest tests/tools/project/test_review.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add tools/project/__init__.py tools/project/review.py \
        workers/task_handlers/project_feature.py \
        workers/task_handlers/project_review.py \
        workers/task_handlers/project_propose.py \
        tests/tools/project/test_review.py
git commit -m "feat: add shared review logic and consolidate model resolver"
```

---

## Task 2: Refactor `ai_review` route to use shared review logic

**Files:**
- Modify: `app/routers/projects_ai.py`

- [ ] **Step 1: Find the review prompt logic in `projects_ai.py`**

```bash
grep -n "review\|rubric\|verdict\|diff" app/routers/projects_ai.py | head -30
```

Identify where the diff is read, the prompt is assembled, and the model is called.

- [ ] **Step 2: Extract inline prompt assembly into `build_review_context`**

In `projects_ai.py`, replace the inline diff + prompt assembly with:

```python
from tools.project.review import build_review_context, call_reviewer_model

# Where the review prompt was previously assembled:
context = build_review_context(
    diff=diff_text,
    feature_description=feature_description or "",
    repo_summary=repo_summary or "",
    architectural_rules="",
)
result = call_reviewer_model(context, model_role=model_key, org_id=org_id)
```

The route still renders `result` as markdown for the client — that part doesn't change.

- [ ] **Step 3: Verify the route still runs (smoke test)**

```bash
python -c "from app.routers.projects_ai import router; print('import ok')"
```

Expected: `import ok`

- [ ] **Step 4: Commit**

```bash
git add app/routers/projects_ai.py
git commit -m "refactor: extract ai_review prompt assembly into tools/project/review"
```

---

## Task 3: `project_index` handler

**Files:**
- Create: `workers/task_handlers/project_index.py`

- [ ] **Step 1: Write `workers/task_handlers/project_index.py`**

```python
"""Kanban handler for project_index tasks.

Reads the project's file tree, generates per-file purpose+exports+deps
using the reviewer model (larger = better quality), then writes the
structured index and a prose summary back to NocoDB.

llm_bound=True. Registered in app/lifespan.py (Plan 3).
"""
from __future__ import annotations

import asyncio
import json
import logging

from workers.kanban import TaskHandler
from tools.project import resolve_agent_model

_log = logging.getLogger("project_index.handler")

# Files larger than this are summarised as "[binary or oversized]"
_MAX_FILE_CHARS = 8_000
# Batch size: files per LLM call
_BATCH_SIZE = 10


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, task, payload)


def _run(task: dict, payload: dict) -> dict:
    project_id = int(payload.get("project_id") or 0)
    trigger = str(payload.get("trigger") or "manual")
    if not project_id:
        return {"status": "failed", "error": "input_payload.project_id required"}

    from infra.nocodb_client import NocodbClient
    from tools.project.fs_tools import read_repo_tree, read_file
    from tools.project.knowledge import write_repo_summary, write_repo_index

    db = NocodbClient()
    model_role = resolve_agent_model(task, "project_po")

    try:
        all_files = db.list_project_files(project_id)
        if not all_files:
            return {"status": "done", "files_indexed": 0, "summary_chars": 0, "tokens_used": 0}

        entries: list[dict] = []
        total_tokens = 0

        for batch_start in range(0, len(all_files), _BATCH_SIZE):
            batch = all_files[batch_start:batch_start + _BATCH_SIZE]
            batch_entries, tokens = _index_batch(db, project_id, batch, model_role)
            entries.extend(batch_entries)
            total_tokens += tokens

        write_repo_index(db, project_id, entries)

        summary = _generate_summary(entries, model_role)
        write_repo_summary(db, project_id, summary, model_used=model_role)

        _log.info(
            "project_index done  project=%d  trigger=%s  files=%d  tokens=%d",
            project_id, trigger, len(entries), total_tokens,
        )
        return {
            "status": "done",
            "files_indexed": len(entries),
            "summary_chars": len(summary),
            "tokens_used": total_tokens,
        }
    except Exception as exc:
        _log.error("project_index failed  project=%d  err=%s", project_id, exc, exc_info=True)
        return {"status": "failed", "error": str(exc)[:400]}


def _index_batch(
    db,
    project_id: int,
    file_rows: list[dict],
    model_role: str,
) -> tuple[list[dict], int]:
    """Generate index entries for a batch of files via one LLM call."""
    from infra.config import resolve_model_entry
    from shared.llm import call_llm_sync

    file_excerpts = []
    for f in file_rows:
        path = f.get("path", "")
        vid = f.get("current_version_id")
        content = ""
        if vid:
            v = db.get_project_file_version(int(vid))
            content = str(v.get("content") or "")[:_MAX_FILE_CHARS] if v else ""
        file_excerpts.append(f"### {path}\n{content or '[empty]'}")

    prompt = (
        "For each file below, produce a JSON array where each element has:\n"
        '{"path": "...", "purpose": "one sentence", "key_exports": ["name", ...], "dependencies": ["path", ...]}\n\n'
        "Respond with only the JSON array, no markdown.\n\n"
        + "\n\n".join(file_excerpts)
    )

    entry = resolve_model_entry(model_role)
    if not entry:
        raise ValueError(f"model role not in catalog: {model_role!r}")

    raw = call_llm_sync(
        url=entry["url"],
        model=entry.get("model_id", ""),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
        temperature=0.1,
    )

    try:
        parsed = json.loads(raw.strip())
        if not isinstance(parsed, list):
            raise ValueError("expected JSON array")
    except (json.JSONDecodeError, ValueError):
        _log.warning("index batch parse failed; using stubs  raw=%s", raw[:200])
        parsed = [{"path": f.get("path", ""), "purpose": "", "key_exports": [], "dependencies": []} for f in file_rows]

    # token count is approximate — 4 chars ≈ 1 token
    approx_tokens = (len(prompt) + len(raw)) // 4
    return parsed, approx_tokens


def _generate_summary(entries: list[dict], model_role: str) -> str:
    """Generate a prose summary of the repo from the index entries."""
    from infra.config import resolve_model_entry
    from shared.llm import call_llm_sync

    index_text = "\n".join(
        f"- {e.get('path', '')}: {e.get('purpose', '')}" for e in entries
    )
    prompt = (
        "You are generating a repo orientation document for an AI coding agent.\n"
        "Based on the file index below, write 3-5 paragraphs covering:\n"
        "1. What this codebase does (product/service)\n"
        "2. Technical stack and architecture\n"
        "3. Key modules and their responsibilities\n"
        "4. Conventions or patterns to follow when adding code\n\n"
        "File index:\n" + index_text
    )

    entry = resolve_model_entry(model_role)
    if not entry:
        return ""

    return call_llm_sync(
        url=entry["url"],
        model=entry.get("model_id", ""),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1500,
        temperature=0.2,
    )


_type_check: TaskHandler = handle
```

- [ ] **Step 2: Write a unit test**

Create `tests/workers/task_handlers/test_project_index.py`:

```python
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_handle_returns_failed_without_project_id():
    from workers.task_handlers.project_index import handle
    result = await handle({"input_payload": {}})
    assert result["status"] == "failed"
    assert "project_id" in result["error"]


@pytest.mark.asyncio
async def test_handle_returns_done_for_empty_project():
    from workers.task_handlers.project_index import handle
    with patch("workers.task_handlers.project_index._run") as mock_run:
        mock_run.return_value = {"status": "done", "files_indexed": 0, "summary_chars": 0, "tokens_used": 0}
        result = await handle({"input_payload": {"project_id": 1}})
    assert result["status"] == "done"
```

- [ ] **Step 3: Run**

```bash
python -m pytest tests/workers/task_handlers/test_project_index.py -v
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add workers/task_handlers/project_index.py tests/workers/task_handlers/test_project_index.py
git commit -m "feat: implement project_index handler (repo summary + index generation)"
```

---

## Task 4: `project_feature` handler

**Files:**
- Replace stub: `workers/task_handlers/project_feature.py`

- [ ] **Step 1: Write the full handler**

```python
"""Kanban handler for project_feature tasks — the Coder role.

Reads repo summary → creates feature branch → runs CodeAgent in execute mode
→ opens PR against the configured staging branch.

Input payload:
  project_id: int
  feature_description: str
  branch_name: str
  architect_context: str | None   — extra system context injected from PO or Architect
  revision_count: int             — incremented on each revise loop (default 0)
  parent_task_id: int | None      — set by project_review on a revise spawn
"""
from __future__ import annotations

import asyncio
import logging

from workers.kanban import TaskHandler
from tools.project import resolve_agent_model

_log = logging.getLogger("project_feature.handler")


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, task, payload)


def _run(task: dict, payload: dict) -> dict:
    from workers.project_autonomy import check_autonomy
    from infra.nocodb_client import NocodbClient
    from infra.settings import get_agent_setting
    from tools.project.knowledge import read_repo_summary

    project_id = int(payload.get("project_id") or 0)
    feature_description = str(payload.get("feature_description") or "").strip()
    branch_name = str(payload.get("branch_name") or "").strip()
    architect_context = str(payload.get("architect_context") or "").strip()
    revision_count = int(payload.get("revision_count") or 0)

    if not project_id or not feature_description or not branch_name:
        return {"status": "failed", "error": "project_id, feature_description, and branch_name are required"}

    db = NocodbClient()

    check_autonomy(db, task)

    model_role = resolve_agent_model(task, "project_agent")
    staging_branch = str(get_agent_setting(f"project:{project_id}", "staging_branch") or "staging")

    # Fetch Gitea connection for this project's org
    gitea = _get_gitea_client(db, project_id)
    if gitea is None:
        return {"status": "failed", "error": "no Gitea connection configured"}
    owner, repo_name = _get_repo_coords(db, project_id)

    # 1. Inject repo summary into system context
    repo_summary = read_repo_summary(db, project_id)

    # 2. Resolve model URL
    from infra.config import resolve_model_entry
    model_entry = resolve_model_entry(model_role)
    if not model_entry:
        return {"status": "failed", "error": f"model role not in catalog: {model_role!r}"}
    model_url_key = model_entry.get("model_id") or model_role

    # 3. Create feature branch
    try:
        gitea.create_branch(owner, repo_name, branch_name, from_branch=staging_branch)
    except Exception as exc:
        return {"status": "failed", "error": f"branch creation failed: {exc}"}

    # 4. Run CodeAgent in execute mode
    try:
        result = _run_code_agent(
            db=db,
            project_id=project_id,
            model=model_url_key,
            feature_description=feature_description,
            architect_context=architect_context,
            repo_summary=repo_summary,
            task_id=str(task.get("Id") or ""),
        )
    except Exception as exc:
        _log.error("CodeAgent failed  project=%d  err=%s", project_id, exc, exc_info=True)
        return {"status": "failed", "error": f"CodeAgent failed: {exc}"}

    # 5. Open PR
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
        "project_feature done  project=%d  branch=%s  pr=%d  tokens=%d",
        project_id, branch_name, pr_id, tokens_used,
    )

    # 6. Optionally queue a knowledge refresh (fire-and-forget)
    try:
        from workers import kanban as _kanban
        _kanban.submit(db, "project_index", {"project_id": project_id, "trigger": "post_feature"},
                       created_by=f"project:{project_id}", agent=f"project:{project_id}")
    except Exception:
        pass  # non-fatal

    return {
        "status": "done",
        "pr_id": pr_id,
        "branch_name": branch_name,
        "commit_shas": result.get("commit_shas") or [],
        "change_summary": result.get("change_summary") or "",
        "tokens_used": tokens_used,
    }


def _run_code_agent(
    db,
    project_id: int,
    model: str,
    feature_description: str,
    architect_context: str,
    repo_summary: str,
    task_id: str,
) -> dict:
    """Run CodeAgent synchronously in execute mode. Returns summary dict."""
    from workers.code.agent import CodeAgent

    extra_context = "\n\n".join(filter(None, [repo_summary, architect_context]))

    # CodeAgent loads system_note from the project row; we prepend extra context
    # to the user message since there is no dedicated system-context injection param.
    user_message = feature_description
    if extra_context:
        user_message = f"[Repo Context]\n{extra_context}\n\n[Task]\n{feature_description}"

    agent = CodeAgent(
        model=model,
        org_id=0,  # background task — no org context needed for FS writes
        mode="execute",
        project_id=project_id,
        interactive_fs=True,
    )

    # CodeAgent.run_job requires a job object for SSE streaming.
    # For background tasks we use a no-op job collector.
    from workers.code.agent import _NullJob  # import or define below
    job = _NullJob()
    agent.run_job(job, user_message)
    return job.summary()


def _get_gitea_client(db, project_id: int):
    from infra.gitea_client import GiteaClient
    project = db.get_project(project_id)
    if not project:
        return None
    org_id = int(project.get("org_id") or 0)
    rows = db._safe_list("gitea_connections", f"(org_id,eq,{org_id})", limit=1)
    if not rows:
        return None
    conn = rows[0]
    return GiteaClient(
        base_url=str(conn.get("base_url") or ""),
        token=str(conn.get("access_token") or ""),
        username=str(conn.get("username") or ""),
    )


def _get_repo_coords(db, project_id: int) -> tuple[str, str]:
    """Return (owner, repo_name) from the project's gitea_origin field."""
    project = db.get_project(project_id)
    origin = str(project.get("gitea_origin") or "")
    # origin format: "owner/repo" or full URL
    if "/" in origin:
        parts = origin.rstrip("/").split("/")
        return parts[-2], parts[-1]
    return "", origin


_type_check: TaskHandler = handle
```

> **Note on `_NullJob`:** Before running, verify whether `workers/code/agent.py` already has a null/mock job class, or whether jobs are always SSE streaming objects. Search: `grep -n "_NullJob\|NullJob\|DummyJob\|MockJob\|BackgroundJob" workers/code/agent.py`. If absent, define a minimal one in the handler:
>
> ```python
> class _NullJob:
>     """Absorbs SSE events from CodeAgent without streaming them."""
>     def __init__(self): self._events = []; self._tokens = 0
>     def emit(self, event: dict): self._events.append(event)
>     def summary(self) -> dict:
>         tokens = sum(e.get("tokens_used", 0) for e in self._events if isinstance(e, dict))
>         changes = [e for e in self._events if isinstance(e, dict) and e.get("type") == "file_change"]
>         return {"tokens_used": tokens, "commit_shas": [], "change_summary": f"{len(changes)} file(s) changed"}
> ```

- [ ] **Step 2: Write unit test**

Create `tests/workers/task_handlers/test_project_feature.py`:

```python
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_handle_missing_required_fields():
    from workers.task_handlers.project_feature import handle
    result = await handle({"input_payload": {"project_id": 1}})
    assert result["status"] == "failed"
    assert "required" in result["error"]


@pytest.mark.asyncio
async def test_handle_no_gitea_connection():
    from workers.task_handlers.project_feature import handle
    with patch("workers.task_handlers.project_feature._run") as mock_run:
        mock_run.return_value = {"status": "failed", "error": "no Gitea connection configured"}
        result = await handle({
            "input_payload": {
                "project_id": 1,
                "feature_description": "Add auth",
                "branch_name": "feature/auth",
            }
        })
    assert result["status"] == "failed"
```

- [ ] **Step 3: Run**

```bash
python -m pytest tests/workers/task_handlers/test_project_feature.py -v
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add workers/task_handlers/project_feature.py tests/workers/task_handlers/test_project_feature.py
git commit -m "feat: implement project_feature handler (Coder role)"
```

---

## Task 5: `project_review` handler

**Files:**
- Replace stub: `workers/task_handlers/project_review.py`

- [ ] **Step 1: Write the full handler**

```python
"""Kanban handler for project_review tasks — the Architect role.

Input payload:
  project_id: int
  pr_id: int
  feature_description: str
  revision_count: int   — default 0; capped at 2 before treating as reject

Verdict actions:
  approve → merge PR to staging_branch, enqueue project_index
  reject  → close PR, record reason
  revise  → close PR, spawn new project_feature with incremented revision_count
"""
from __future__ import annotations

import asyncio
import logging

from workers.kanban import TaskHandler
from tools.project import resolve_agent_model

_log = logging.getLogger("project_review.handler")

_MAX_REVISIONS = 2


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, task, payload)


def _run(task: dict, payload: dict) -> dict:
    from workers.project_autonomy import check_autonomy
    from infra.nocodb_client import NocodbClient
    from infra.settings import get_agent_setting
    from tools.project.knowledge import read_repo_summary
    from tools.project.review import build_review_context, call_reviewer_model, parse_verdict

    project_id = int(payload.get("project_id") or 0)
    pr_id = int(payload.get("pr_id") or 0)
    feature_description = str(payload.get("feature_description") or "").strip()
    revision_count = int(payload.get("revision_count") or 0)

    if not project_id or not pr_id or not feature_description:
        return {"status": "failed", "error": "project_id, pr_id, and feature_description are required"}

    db = NocodbClient()
    check_autonomy(db, task)

    model_role = resolve_agent_model(task, "project_po")
    staging_branch = str(get_agent_setting(f"project:{project_id}", "staging_branch") or "staging")

    from workers.task_handlers.project_feature import _get_gitea_client, _get_repo_coords
    gitea = _get_gitea_client(db, project_id)
    if gitea is None:
        return {"status": "failed", "error": "no Gitea connection configured"}
    owner, repo_name = _get_repo_coords(db, project_id)

    # 1. Gather review inputs
    repo_summary = read_repo_summary(db, project_id)
    arch_rules = _get_architectural_rules(db, project_id)

    try:
        diff = gitea.get_pr_diff(owner, repo_name, pr_id)
    except Exception as exc:
        return {"status": "failed", "error": f"failed to fetch PR diff: {exc}"}

    # 2. Call reviewer model
    context = build_review_context(diff, feature_description, repo_summary, arch_rules)
    try:
        result = call_reviewer_model(context, model_role=model_role)
    except Exception as exc:
        return {"status": "failed", "error": f"reviewer model failed: {exc}"}

    verdict = parse_verdict(result)
    rationale = str(result.get("rationale") or "")

    _log.info(
        "project_review verdict=%s  project=%d  pr=%d  revision=%d",
        verdict, project_id, pr_id, revision_count,
    )

    if verdict == "approve":
        return _do_approve(gitea, owner, repo_name, pr_id, staging_branch, db, project_id, task, result, revision_count)
    if verdict == "reject" or revision_count >= _MAX_REVISIONS:
        return _do_reject(gitea, owner, repo_name, pr_id, rationale, revision_count)
    # revise
    return _do_revise(gitea, owner, repo_name, pr_id, db, task, payload, result, revision_count)


def _do_approve(gitea, owner, repo_name, pr_id, staging_branch, db, project_id, task, result, revision_count) -> dict:
    try:
        gitea.merge_pr(owner, repo_name, pr_id, merge_method="merge")
        merge_sha = gitea.get_pr(owner, repo_name, pr_id).get("merge_commit_sha") or ""
    except Exception as exc:
        return {"status": "failed", "error": f"merge failed: {exc}"}

    # Enqueue knowledge refresh
    try:
        from workers import kanban as _kanban
        _kanban.submit(db, "project_index", {"project_id": project_id, "trigger": "post_merge"},
                       created_by=f"project:{project_id}", agent=f"project:{project_id}")
    except Exception:
        pass

    return {
        "status": "done",
        "verdict": "approve",
        "rationale": str(result.get("rationale") or ""),
        "merge_sha": merge_sha,
        "child_task_id": None,
        "revision_count": revision_count,
        "tokens_used": 0,
    }


def _do_reject(gitea, owner, repo_name, pr_id, rationale, revision_count) -> dict:
    try:
        gitea.close_pr(owner, repo_name, pr_id)
    except Exception as exc:
        _log.warning("close_pr failed  pr=%d  err=%s", pr_id, exc)
    return {
        "status": "done",
        "verdict": "reject",
        "rationale": rationale,
        "merge_sha": None,
        "child_task_id": None,
        "revision_count": revision_count,
        "tokens_used": 0,
    }


def _do_revise(gitea, owner, repo_name, pr_id, db, task, payload, result, revision_count) -> dict:
    from workers import kanban as _kanban

    feedback = "\n".join(result.get("suggestions") or result.get("concerns") or [])
    original_context = str((payload.get("input_payload") or payload).get("architect_context") or "")
    new_context = "\n\n".join(filter(None, [original_context, f"[Revision {revision_count + 1} feedback]\n{feedback}"]))

    project_id = int(payload.get("project_id") or 0)
    new_payload = {
        **payload,
        "architect_context": new_context,
        "revision_count": revision_count + 1,
        "parent_task_id": task.get("Id"),
    }
    # Generate a new branch name for the revision
    orig_branch = str(payload.get("branch_name") or "feature/revision")
    new_branch = f"{orig_branch}-rev{revision_count + 1}"
    new_payload["branch_name"] = new_branch

    try:
        gitea.close_pr(owner, repo_name, pr_id)
    except Exception:
        pass

    child_id = _kanban.submit(
        db, "project_feature", new_payload,
        created_by=f"project:{project_id}",
        agent=f"project:{project_id}",
    )

    return {
        "status": "done",
        "verdict": "revise",
        "rationale": str(result.get("rationale") or ""),
        "merge_sha": None,
        "child_task_id": child_id,
        "revision_count": revision_count,
        "tokens_used": 0,
    }


def _get_architectural_rules(db, project_id: int) -> str:
    from infra.settings import get_agent_setting
    from tools.project.fs_tools import read_file

    # Repo-level ARCHITECTURE.md takes precedence over global settings
    try:
        return read_file(db, project_id, "ARCHITECTURE.md")
    except KeyError:
        pass
    return str(get_agent_setting(f"project:{project_id}", "architectural_rules") or "")


_type_check: TaskHandler = handle
```

- [ ] **Step 2: Write unit test**

Create `tests/workers/task_handlers/test_project_review.py`:

```python
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_handle_missing_fields():
    from workers.task_handlers.project_review import handle
    result = await handle({"input_payload": {"project_id": 1}})
    assert result["status"] == "failed"
    assert "required" in result["error"]


def test_max_revisions_falls_back_to_reject():
    from workers.task_handlers.project_review import _MAX_REVISIONS, _do_reject
    # If revision_count >= _MAX_REVISIONS, the handler routes to reject
    assert _MAX_REVISIONS == 2
```

- [ ] **Step 3: Run**

```bash
python -m pytest tests/workers/task_handlers/test_project_review.py -v
```

- [ ] **Step 4: Commit**

```bash
git add workers/task_handlers/project_review.py tests/workers/task_handlers/test_project_review.py
git commit -m "feat: implement project_review handler (Architect role)"
```

---

## Task 6: `project_propose` handler

**Files:**
- Replace stub: `workers/task_handlers/project_propose.py`

- [ ] **Step 1: Write the full handler**

```python
"""Kanban handler for project_propose tasks — the PO role.

Reads the repo summary, recent commits, and open issues, then prompts
the PO model for a list of proposed features.

In proposal-only mode: enqueues project_feature tasks with status='blocked'
  (pending user approval via the task API).
In full mode: enqueues directly with status='ready'.

Input payload:
  project_id: int

Output:
  proposals: [{title, description, rationale, scope, task_id}]
  queued: int
"""
from __future__ import annotations

import asyncio
import json
import logging

from workers.kanban import TaskHandler
from tools.project import resolve_agent_model

_log = logging.getLogger("project_propose.handler")

_PROPOSE_RUBRIC = """
You are a Product Owner reviewing a software repository. Based on the context below,
identify the next 1-3 most valuable improvements or features.

For each proposal respond with a JSON array element:
{
  "title": "Short imperative title (max 60 chars)",
  "description": "2-3 sentences describing what to build",
  "rationale": "Why this is valuable now",
  "scope": "small" | "medium" | "large"
}

Respond with only a JSON array. No markdown. No commentary.
"""


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, task, payload)


def _run(task: dict, payload: dict) -> dict:
    from workers.project_autonomy import check_autonomy, _get_setting
    from infra.nocodb_client import NocodbClient
    from infra.settings import get_agent_setting
    from tools.project.knowledge import read_repo_summary
    from infra.config import resolve_model_entry
    from shared.llm import call_llm_sync
    from workers import kanban as _kanban

    project_id = int(payload.get("project_id") or 0)
    if not project_id:
        return {"status": "failed", "error": "input_payload.project_id required"}

    db = NocodbClient()
    check_autonomy(db, task)

    model_role = resolve_agent_model(task, "project_po")

    # 1. Guard: if no summary, enqueue index first and block
    repo_summary = read_repo_summary(db, project_id)
    if not repo_summary.strip():
        _kanban.submit(db, "project_index", {"project_id": project_id, "trigger": "pre_propose"},
                       created_by=f"project:{project_id}", agent=f"project:{project_id}")
        return {
            "status": "blocked",
            "error": "no repo summary available; project_index enqueued — retry after indexing",
            "proposals": [],
            "queued": 0,
            "tokens_used": 0,
        }

    # 2. Check queue depth
    from workers.project_autonomy import _check_queue_depth
    try:
        _check_queue_depth(db, project_id)
    except Exception:
        return {"status": "done", "proposals": [], "queued": 0, "tokens_used": 0,
                "note": "proposal queue full"}

    # 3. Gather context
    context_parts = [f"## Repo Summary\n{repo_summary}"]
    context_parts.extend(_gather_extra_context(db, project_id))
    context = "\n\n".join(context_parts)

    # 4. Call PO model
    entry = resolve_model_entry(model_role)
    if not entry:
        return {"status": "failed", "error": f"model role not in catalog: {model_role!r}"}

    prompt = _PROPOSE_RUBRIC + "\n\n" + context
    raw = call_llm_sync(
        url=entry["url"],
        model=entry.get("model_id", ""),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1500,
        temperature=0.3,
    )
    approx_tokens = (len(prompt) + len(raw)) // 4

    try:
        proposals = json.loads(raw.strip())
        if not isinstance(proposals, list):
            raise ValueError("not a list")
    except (json.JSONDecodeError, ValueError) as exc:
        _log.warning("propose parse failed  err=%s  raw=%s", exc, raw[:200])
        return {"status": "failed", "error": f"model returned non-JSON: {raw[:200]}"}

    # 5. Enqueue feature tasks
    autonomy_mode = str(_get_setting(project_id, "autonomy_mode") or "proposal-only")
    task_status = "ready" if autonomy_mode == "full" else "blocked"

    queued_proposals = []
    for prop in proposals[:5]:  # cap at 5 per run
        title = str(prop.get("title") or "untitled")[:72]
        import re
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
        branch_name = f"feature/{slug}"

        feature_payload = {
            "project_id": project_id,
            "feature_description": f"{title}\n\n{prop.get('description', '')}",
            "branch_name": branch_name,
            "architect_context": None,
            "revision_count": 0,
        }
        task_id = _kanban.submit(
            db, "project_feature", feature_payload,
            status=task_status,
            created_by=f"project:{project_id}",
            agent=f"project:{project_id}",
        )
        if task_status == "blocked":
            # Write approval note into the task row
            db._patch("task_list", task_id, {"error": "awaiting user approval"})
        queued_proposals.append({**prop, "task_id": task_id})

    _log.info(
        "project_propose done  project=%d  proposals=%d  mode=%s",
        project_id, len(queued_proposals), autonomy_mode,
    )
    return {
        "status": "done",
        "proposals": queued_proposals,
        "queued": len(queued_proposals),
        "tokens_used": approx_tokens,
    }


def _gather_extra_context(db, project_id: int) -> list[str]:
    """Gather recent commits and open issues for extra PO context."""
    parts: list[str] = []
    try:
        from workers.task_handlers.project_feature import _get_gitea_client, _get_repo_coords
        gitea = _get_gitea_client(db, project_id)
        if gitea:
            owner, repo_name = _get_repo_coords(db, project_id)
            commits = gitea.list_commits(owner, repo_name, limit=10)
            if commits:
                lines = [f"- {c.get('commit', {}).get('message', '')[:80]}" for c in commits]
                parts.append("## Recent Commits\n" + "\n".join(lines))
            issues = gitea.list_issues(owner, repo_name, state="open")
            if issues:
                lines = [f"- #{i.get('number')}: {i.get('title', '')}" for i in issues[:10]]
                parts.append("## Open Issues\n" + "\n".join(lines))
    except Exception as exc:
        _log.debug("extra context gather failed  err=%s", exc)
    return parts


_type_check: TaskHandler = handle
```

- [ ] **Step 2: Write unit test**

Create `tests/workers/task_handlers/test_project_propose.py`:

```python
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock


@pytest.mark.asyncio
async def test_handle_missing_project_id():
    from workers.task_handlers.project_propose import handle
    result = await handle({"input_payload": {}})
    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_handle_blocks_when_no_summary():
    from workers.task_handlers.project_propose import handle
    with patch("workers.task_handlers.project_propose._run") as mock_run:
        mock_run.return_value = {
            "status": "blocked",
            "error": "no repo summary available; project_index enqueued",
            "proposals": [],
            "queued": 0,
            "tokens_used": 0,
        }
        result = await handle({"input_payload": {"project_id": 1}})
    assert result["status"] == "blocked"
    assert result["queued"] == 0
```

- [ ] **Step 3: Run**

```bash
python -m pytest tests/workers/task_handlers/test_project_propose.py -v
```

- [ ] **Step 4: Commit**

```bash
git add workers/task_handlers/project_propose.py tests/workers/task_handlers/test_project_propose.py
git commit -m "feat: implement project_propose handler (PO role)"
```

---

**Plan 2 complete.** All four handlers implemented and committed. Proceed to Plan 3 (API layer and wiring).
