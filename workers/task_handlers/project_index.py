"""Kanban handler for project_index tasks.

Reads the project's file tree, generates per-file purpose+exports+deps
using the reviewer model, then writes the structured index and a prose
summary back to NocoDB.
"""
from __future__ import annotations

import asyncio
import json
import logging

from workers.kanban import TaskHandler
from workers.project_autonomy import check_autonomy
from tools.project import resolve_agent_model

_log = logging.getLogger("project_index.handler")

_MAX_FILE_CHARS = 8_000
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
    from tools.project.knowledge import write_repo_summary, write_repo_index

    db = NocodbClient()
    check_autonomy(db, task)
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
    db,  # reason: NocodbClient imported lazily to avoid circular imports at module load
    project_id: int,
    file_rows: list[dict],
    model_role: str,
) -> tuple[list[dict], int]:
    """Generate index entries for a batch of files via one LLM call."""
    from infra.config import resolve_model_entry

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

    raw = _call_model_sync(entry, prompt)

    try:
        parsed = json.loads(raw.strip())
        if not isinstance(parsed, list):
            raise ValueError("expected JSON array")
    except (json.JSONDecodeError, ValueError):
        _log.warning("index batch parse failed; using stubs  raw=%s", raw[:200])
        parsed = [
            {"path": f.get("path", ""), "purpose": "", "key_exports": [], "dependencies": []}
            for f in file_rows
        ]

    approx_tokens = (len(prompt) + len(raw)) // 4
    return parsed, approx_tokens


def _generate_summary(entries: list[dict], model_role: str) -> str:
    """Generate a prose summary of the repo from the index entries."""
    from infra.config import resolve_model_entry

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

    return _call_model_sync(entry, prompt)


def _call_model_sync(entry: dict, prompt: str) -> str:
    """Call model synchronously using the shared model client."""
    from shared.model_client import build_model_client

    model_id = entry.get("model_id") or entry.get("model") or ""
    mc = build_model_client()
    result = mc.complete_sync(
        messages=[{"role": "user", "content": prompt}],
        model=f"local:{model_id}",
        max_tokens=2000,
        temperature=0.1,
    )
    if result.error:
        raise RuntimeError(f"model error: {result.error}")
    return result.text


_type_check: TaskHandler = handle
