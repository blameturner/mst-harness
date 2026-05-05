"""Kanban handler for project_propose tasks — the PO role.

Reads the repo summary, recent commits, and open issues, then prompts
the PO model for a list of proposed features.

In proposal-only mode: enqueues project_feature tasks with status='blocked'.
In full mode: enqueues directly with status='ready'.

Input payload:
  project_id: int
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from tools.project import resolve_agent_model
from workers.kanban import TaskHandler

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
    from workers.project_autonomy import check_autonomy, _get_setting, _check_queue_depth
    from infra.nocodb_client import NocodbClient
    from tools.project.knowledge import read_repo_summary
    from infra.config import resolve_model_entry
    from workers import kanban as _kanban

    project_id = int(payload.get("project_id") or 0)
    if not project_id:
        return {"status": "failed", "error": "input_payload.project_id required"}

    db = NocodbClient()
    check_autonomy(db, task)

    model_role = resolve_agent_model(task, "project_po")

    repo_summary = read_repo_summary(db, project_id)
    if not repo_summary.strip():
        _kanban.submit(
            db, "project_index", {"project_id": project_id, "trigger": "pre_propose"},
            created_by=f"project:{project_id}", agent=f"project:{project_id}",
        )
        return {
            "status": "blocked",
            "error": "no repo summary available; project_index enqueued — retry after indexing",
            "proposals": [],
            "queued": 0,
            "tokens_used": 0,
        }

    try:
        _check_queue_depth(db, project_id)
    except Exception:
        return {"status": "done", "proposals": [], "queued": 0, "tokens_used": 0,
                "note": "proposal queue full"}

    context_parts = [f"## Repo Summary\n{repo_summary}"]
    context_parts.extend(_gather_extra_context(db, project_id))
    context = "\n\n".join(context_parts)

    entry = resolve_model_entry(model_role)
    if not entry:
        return {"status": "failed", "error": f"model role not in catalog: {model_role!r}"}

    model_id = str(entry.get("model_id") or model_role)
    prompt = _PROPOSE_RUBRIC + "\n\n" + context

    from shared.model_client import build_model_client
    mc = build_model_client()
    completion = mc.complete_sync(
        messages=[{"role": "user", "content": prompt}],
        model=f"local:{model_id}",
        max_tokens=1500,
        temperature=0.3,
    )
    raw = completion.text or ""
    approx_tokens = (len(prompt) + len(raw)) // 4

    try:
        proposals = json.loads(raw.strip())
        if not isinstance(proposals, list):
            raise ValueError("not a list")
    except (json.JSONDecodeError, ValueError) as exc:
        _log.warning("propose parse failed  err=%s  raw=%s", exc, raw[:200])
        return {"status": "failed", "error": f"model returned non-JSON: {raw[:200]}"}

    autonomy_mode = str(_get_setting(project_id, "autonomy_mode") or "proposal-only")
    task_status = "ready" if autonomy_mode == "full" else "blocked"

    queued_proposals = []
    for prop in proposals[:5]:
        title = str(prop.get("title") or "untitled")[:72]
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
            try:
                db._patch("task_list", task_id, {"error": "awaiting user approval"})
            except Exception:
                pass
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
