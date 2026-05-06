"""LLM call functions for teaching pipeline.

Each public function maps to one logical step:
  generate_curriculum_modules  →  list[dict] (module array)
  generate_lesson              →  (lesson_markdown, session_summary, anki_cards, checks)
  generate_revision            →  (lesson_markdown, session_summary, anki_cards, checks)
  generate_checks              →  list[dict]

All calls use shared.models.model_call which handles slot acquisition, usage
logging, and the reasoner guard.
"""
from __future__ import annotations

import json
import logging
import re

from shared.models import model_call

_log = logging.getLogger("teaching.llm")

_MODULE_SCHEMA = """{
  "id": "m1",
  "title": "...",
  "objectives": ["..."],
  "prerequisites": [],
  "depth": "introductory|working|deep",
  "status": "pending",
  "amended_reason": null
}"""

_CHECK_SCHEMA = '{"question": "...", "expected_answer": "...", "concept": "...", "difficulty": "introductory|working|deep"}'


def generate_curriculum_modules(
    topic: str,
    root_goal: str | None,
    learner_note: str | None,
    known_concepts: list[dict],
    existing_modules: list[dict] | None,
) -> list[dict]:
    concepts_text = ", ".join(c.get("concept", "") for c in known_concepts[:30]) or "none yet"
    amend_section = ""
    if existing_modules:
        amend_section = f"\nExisting modules (amend rather than replace):\n{json.dumps(existing_modules, indent=2)}\n"
    prompt = (
        f'Build a learning curriculum for the topic: "{topic}".\n'
        f'Root goal: {root_goal or "general understanding"}\n'
        f'Learner notes: {learner_note or "none"}\n'
        f'Concepts the learner already knows: {concepts_text}\n'
        f'{amend_section}\n'
        f'Return a JSON object with a "modules" array. Each element:\n{_MODULE_SCHEMA}\n'
        f'depth must be "introductory", "working", or "deep".\n'
        f'Order modules from foundational to advanced. Return ONLY the JSON object.'
    )
    for attempt in range(2):
        text, _ = model_call("teaching_curriculum", prompt)
        if not text:
            continue
        try:
            parsed = _parse_json_object(text)
            modules = parsed.get("modules") or []
            if isinstance(modules, list) and modules:
                return modules
        except (ValueError, json.JSONDecodeError):
            _log.warning("teaching_curriculum parse failed (attempt %d/2), trying salvage", attempt + 1)
        modules = _salvage_list(text, "modules")
        if modules:
            return modules
    raise RuntimeError("teaching_curriculum LLM failed to return modules after 2 attempts")


def generate_lesson(
    topic: str,
    module_title: str,
    objectives: list[str],
    learner_level: str,
    known_concepts: list[dict],
    research_text: str,
) -> tuple[str, str, str, list[dict]]:
    known = ", ".join(c.get("concept", "") for c in known_concepts[:20]) or "none"
    obj_text = "\n".join(f"- {o}" for o in objectives)
    lesson_prompt = (
        f'Write a comprehensive lesson on "{module_title}" (topic: {topic}).\n'
        f'Learner level: {learner_level or "intermediate"}\n'
        f'Known concepts: {known}\n\n'
        f'Learning objectives:\n{obj_text}\n\n'
        f'Source material:\n{research_text[:18000]}\n\n'
        f'Write the full lesson in markdown. Include explanation, worked examples, '
        f'and connections to known concepts. Use ## headings for each major section.'
    )
    lesson_markdown, _ = model_call("teaching_lesson", lesson_prompt)
    if not lesson_markdown:
        raise RuntimeError("teaching_lesson LLM call returned empty text")

    session_summary, anki_cards, checks = _generate_lesson_meta(lesson_markdown)
    return lesson_markdown, session_summary, anki_cards, checks


def generate_revision(
    lesson_markdown: str,
    sources: list[dict],
    revision_instructions: str,
) -> tuple[str, str, str, list[dict]]:
    sources_text = "\n".join(
        f"- {s.get('title', '')} ({s.get('url', '')}): {s.get('excerpt', '')[:200]}"
        for s in sources[:10]
    )
    prompt = (
        f'Revise the following lesson based on these instructions: {revision_instructions}\n\n'
        f'Original lesson:\n{lesson_markdown[:14000]}\n\n'
        f'Sources available:\n{sources_text}\n\n'
        f'Return the full revised lesson in markdown. Keep all sections; deepen or clarify as instructed.'
    )
    revised_markdown, _ = model_call("teaching_revision", prompt)
    if not revised_markdown:
        raise RuntimeError("teaching_revision LLM call returned empty text")

    session_summary, anki_cards, checks = _generate_lesson_meta(revised_markdown)
    return revised_markdown, session_summary, anki_cards, checks


def generate_checks(
    lesson_markdown: str,
    concept_focus: list[str],
    difficulty: str,
    count: int,
) -> list[dict]:
    focus_text = f"Focus on these concepts: {', '.join(concept_focus)}.\n" if concept_focus else ""
    prompt = (
        f'Generate {count} comprehension checks for this lesson.\n'
        f'{focus_text}'
        f'Difficulty: {difficulty}.\n\n'
        f'Lesson:\n{lesson_markdown[:10000]}\n\n'
        f'Return a JSON object with a "checks" array. Each element:\n{_CHECK_SCHEMA}\n'
        f'Return ONLY the JSON object.'
    )
    for attempt in range(2):
        text, _ = model_call("teaching_check", prompt)
        if not text:
            continue
        try:
            parsed = _parse_json_object(text)
            checks = parsed.get("checks") or []
            if isinstance(checks, list) and checks:
                return checks
        except (ValueError, json.JSONDecodeError):
            _log.warning("teaching_check parse failed (attempt %d/2), trying salvage", attempt + 1)
        checks = _salvage_list(text, "checks")
        if checks:
            return checks
    raise RuntimeError("teaching_check LLM failed to return checks after 2 attempts")


# ── internal helpers ─────────────────────────────────────────────────────────

def _generate_lesson_meta(lesson_markdown: str) -> tuple[str, str, list[dict]]:
    """Second LLM pass: extract structured metadata from the prose lesson."""
    prompt = (
        f'Given this lesson:\n{lesson_markdown[:8000]}\n\n'
        f'Return a JSON object with:\n'
        f'{{"session_summary": "1-2 paragraph summary", '
        f'"anki_cards": "front\\tback\\ttags\\n... (one card per line, tab-separated)", '
        f'"checks": [{_CHECK_SCHEMA}]}}\n'
        f'Return ONLY the JSON object.'
    )
    text, _ = model_call("teaching_lesson_meta", prompt)
    if not text:
        _log.warning("teaching_lesson_meta returned empty — using fallback metadata")
        return "", "", []
    try:
        parsed = _parse_json_object(text)
        session_summary = str(parsed.get("session_summary") or "")
        anki_cards = str(parsed.get("anki_cards") or "")
        checks = parsed.get("checks") or []
        if not isinstance(checks, list):
            checks = []
        return session_summary, anki_cards, checks
    except (ValueError, json.JSONDecodeError) as exc:
        _log.warning("teaching_lesson_meta parse failed: %s", exc)
        return "", "", []


def _salvage_list(raw: str, key: str) -> list:
    """Extract a named JSON array from malformed model output."""
    s = _strip_fence(raw)
    s = s.replace("“", '"').replace("”", '"')
    s = re.sub(r",\s*([}\]])", r"\1", s)
    m = re.search(rf'"{key}"\s*:\s*\[', s, re.IGNORECASE)
    if not m:
        return []
    start = m.end()  # right after the opening [
    depth = 1
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start - 1:i + 1])
                except json.JSONDecodeError:
                    return []
    return []


def _strip_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = s.rstrip("`").strip()
    return s


def _parse_json_object(raw: str) -> dict:
    s = _strip_fence(raw)
    s = s.replace(""", '"').replace(""", '"')
    s = re.sub(r",\s*([}\]])", r"\1", s)
    start = s.find("{")
    if start < 0:
        raise ValueError("no JSON object found in LLM output")
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            escape = not escape and ch == "\\"
            if not escape and ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise ValueError("unbalanced JSON object in LLM output")
