"""Shared review logic used by both the AI review API route and the project_review handler."""
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
    """Call the reviewer model and return the parsed JSON verdict dict."""
    from infra.config import resolve_model_entry
    from shared.model_client import build_model_client

    entry = resolve_model_entry(model_role)
    if not entry:
        raise ValueError(f"model role not found in catalog: {model_role!r}")

    model_id = entry.get("model_id") or entry.get("model") or ""
    prompt = REVIEW_RUBRIC + "\n\n" + context
    mc = build_model_client()
    result = mc.complete_sync(
        messages=[{"role": "user", "content": prompt}],
        model=f"local:{model_id}",
        max_tokens=2000,
        temperature=0.1,
    )
    if result.error:
        raise RuntimeError(f"reviewer model error: {result.error}")
    return _parse_verdict_json(result.text)


def parse_verdict(result: dict) -> Literal["approve", "reject", "revise"]:
    v = str(result.get("verdict") or "").lower().strip()
    if v not in ("approve", "reject", "revise"):
        raise ValueError(f"unexpected verdict value: {v!r}")
    return v  # type: ignore[return-value]  # reason: narrowed by the membership check above


def _parse_verdict_json(raw: str) -> dict:
    import json
    import re
    cleaned = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\n?```$", "", cleaned.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"reviewer model returned non-JSON: {raw[:300]!r}") from exc
    if "verdict" not in data:
        raise ValueError(f"reviewer response missing 'verdict' key: {data}")
    return data
