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

    from infra.settings import get_agent_setting
    staging_branch = str(get_agent_setting(f"project:{project_id}", "staging_branch") or "staging")

    from workers.task_handlers.project_feature import _get_gitea_client, _get_repo_coords
    gitea = _get_gitea_client(db, project_id)
    if gitea is None:
        return {"status": "failed", "error": "no Gitea connection configured"}
    owner, repo_name = _get_repo_coords(db, project_id)

    repo_summary = read_repo_summary(db, project_id)
    arch_rules = _get_architectural_rules(db, project_id)

    try:
        diff = gitea.get_pr_diff(owner, repo_name, pr_id)
    except Exception as exc:
        return {"status": "failed", "error": f"failed to fetch PR diff: {exc}"}

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
        return _do_approve(gitea, owner, repo_name, pr_id, staging_branch, db, project_id, result, revision_count)
    if verdict == "reject" or revision_count >= _MAX_REVISIONS:
        return _do_reject(gitea, owner, repo_name, pr_id, rationale, revision_count)
    return _do_revise(gitea, owner, repo_name, pr_id, db, task, payload, result, revision_count)


def _do_approve(gitea, owner, repo_name, pr_id, staging_branch, db, project_id, result, revision_count) -> dict:
    try:
        gitea.merge_pr(owner, repo_name, pr_id, merge_method="merge")
        merge_sha = gitea.get_pr(owner, repo_name, pr_id).get("merge_commit_sha") or ""
    except Exception as exc:
        return {"status": "failed", "error": f"merge failed: {exc}"}

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
    original_context = str(payload.get("architect_context") or "")
    new_context = "\n\n".join(filter(None, [original_context, f"[Revision {revision_count + 1} feedback]\n{feedback}"]))

    project_id = int(payload.get("project_id") or 0)
    orig_branch = str(payload.get("branch_name") or "feature/revision")
    new_branch = f"{orig_branch}-rev{revision_count + 1}"

    new_payload = {
        **payload,
        "architect_context": new_context,
        "revision_count": revision_count + 1,
        "parent_task_id": task.get("Id"),
        "branch_name": new_branch,
    }

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
    try:
        return read_file(db, project_id, "ARCHITECTURE.md")
    except KeyError:
        pass
    return str(get_agent_setting(f"project:{project_id}", "architectural_rules") or "")


_type_check: TaskHandler = handle
