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
  otherwise                                → project_human_review (blocked, revision_count+1)
"""
from __future__ import annotations

import asyncio
import json
import logging

from tools.project import resolve_agent_model
from tools.project.gitea_sync import push_files_to_gitea
from workers.kanban import TaskHandler
from workers.project_autonomy import check_autonomy
from infra.nocodb_client import NocodbClient
from infra.config import resolve_model_entry
from shared.model_client import build_model_client

_log = logging.getLogger(__name__)

_MAX_REVISE_FILES = 5
_MAX_FILE_CHARS = 3_000
_MAX_REVISE_CYCLES = 2

_REVISE_PROMPT = """\
You are a code editor. The human reviewer found issues in these files.
Apply the feedback precisely. Make only the changes needed to address the feedback.

Human feedback:
<human_feedback>
{feedback}
</human_feedback>

Current file contents:
{files}

Respond with a JSON array (no markdown, no commentary):
[{{"path": "path/to/file.py", "content": "<full corrected file content>"}}]
"""


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, task, payload)


def _run(task: dict, payload: dict) -> dict:
    import workers.kanban as _kanban

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
    file_snippets: list[str] = []
    for p in target_paths:
        if p not in all_files:
            continue
        content = str(all_files[p].get("content") or "")
        if len(content) > _MAX_FILE_CHARS:
            _log.warning("revise: truncating file for prompt  path=%s  chars=%d", p, len(content))
        file_snippets.append(f"### {p}\n```\n{content[:_MAX_FILE_CHARS]}\n```")
    if not file_snippets:
        return {
            "status": "failed",
            "error": f"none of the changed_paths exist in project files: {target_paths}",
        }
    files_block = "\n\n".join(file_snippets)

    prompt = _REVISE_PROMPT.format(feedback=human_feedback, files=files_block)
    mc = build_model_client()
    try:
        completion = mc.complete_sync(
            messages=[{"role": "user", "content": prompt}],
            model=f"local:{model_id}",
            max_tokens=4000,
            temperature=0.2,
        )
    except Exception as exc:
        _log.error("revise: model call failed  project=%d  err=%s", project_id, exc)
        return {"status": "failed", "error": f"model call failed: {exc}"}
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
    try:
        from workers.task_handlers.project_feature import _get_gitea_client, _get_repo_coords
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
