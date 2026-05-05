# Teaching Agent — Kanban Task Types and Handler Shapes

_Date: 2026-05-05. Derived from F.1 design (`2026-05-05-teaching-agent-design.md`)._

---

## Design notes before the schemas

Discussion mode is real-time chat. It does not require a Kanban task — it runs in the chat handler with access to the learner model and curriculum, drawing on lesson content already stored. Only lesson mode requires background work.

This gives four Kanban task types:

| Task type | What it does | LLM-bound | Search |
|---|---|---|---|
| `teaching_curriculum` | Builds or updates the curriculum for a topic | yes | no |
| `teaching_lesson` | Runs a deep search pass and produces the full lesson for one curriculum module | yes | yes (planned) |
| `teaching_revision` | Revises a previous lesson based on user feedback | yes | no |
| `teaching_check` | Generates standalone comprehension checks against a lesson | yes | no |

`teaching_curriculum` runs once per topic before the first lesson, and again whenever the learner model or user feedback warrants restructuring. `teaching_lesson` consumes a curriculum module and produces the content. `teaching_revision` and `teaching_check` are downstream of a `teaching_lesson` task and reference it via `parent_task_id`.

---

## NocoDB tables required

### `learner_curricula`

| Column | Type | Notes |
|---|---|---|
| `Id` | int PK | auto |
| `topic` | varchar | e.g. "Attention in LLMs" |
| `org_id` | int | FK → orgs |
| `modules` | long text | JSON array — see module schema below |
| `current_module_index` | int | 0-based index into modules array |
| `root_goal` | text | What the learner is trying to understand or do |
| `created_at` | datetime | |
| `updated_at` | datetime | Updated on every amendment |

Module JSON shape (one element of `modules`):
```json
{
  "id": "m1",
  "title": "Why attention exists — the problem it solves",
  "objectives": ["explain why RNNs struggled with long-range dependencies", "describe what a context vector is"],
  "prerequisites": [],
  "depth": "working",
  "status": "completed",
  "amended_reason": null
}
```
`depth` is `introductory | working | deep`. `status` is `pending | active | completed | deferred`.

### `learner_concepts`

| Column | Type | Notes |
|---|---|---|
| `Id` | int PK | auto |
| `org_id` | int | FK → orgs |
| `topic` | varchar | |
| `concept` | varchar | Granular unit |
| `mastery` | varchar | `exposed` / `practiced` / `verified` |
| `last_seen` | datetime | |
| `misconceptions` | text | JSON array of strings |
| `preferred_style` | varchar | `examples_first` / `theory_first` / null |
| `session_count` | int | |

### `teaching_lessons`

Stores the produced lesson content so discussion mode and revision tasks can reference it without re-running the task.

| Column | Type | Notes |
|---|---|---|
| `Id` | int PK | auto |
| `task_id` | int | FK → task_list |
| `curriculum_id` | int | FK → learner_curricula |
| `module_id` | varchar | Which module was taught |
| `lesson_markdown` | long text | Full lesson content |
| `anki_cards` | long text | Plain-text Anki import (tab-separated) |
| `session_summary` | long text | Condensed summary for the learner model |
| `sources` | long text | JSON array of {url, title, excerpt} |
| `checks` | long text | JSON array of comprehension checks (see schema below) |
| `created_at` | datetime | |

---

## Task type 1: `teaching_curriculum`

**Purpose:** Build or update the curriculum for a topic. Runs before the first lesson on a topic, and again when the learner model or user feedback warrants restructuring. No search — uses the learner model and model knowledge.

### Input schema (`input_payload`)

```python
{
    "topic": str,           # required — e.g. "Attention in LLMs"
    "org_id": int,          # required
    "root_goal": str,       # optional — what the learner wants to achieve
    "curriculum_id": int,   # optional — if updating an existing curriculum
    "learner_note": str,    # optional — e.g. "learner knows linear algebra, new to ML"
}
```

### Output schema (`output_payload`)

```python
{
    "status": "completed",
    "curriculum_id": int,   # NocoDB row Id of the created/updated curriculum
    "topic": str,
    "module_count": int,
    "modules": list[dict],  # full module array
}
```

### Handler signature

```python
# workers/task_handlers/teaching_curriculum.py
"""Kanban handler for 'teaching_curriculum' tasks.

Builds or updates the learning curriculum for a topic. Uses the learner model
to calibrate depth and sequencing. No search; runs from model knowledge.
Input: {topic, org_id, root_goal?, curriculum_id?, learner_note?}.
Output: {curriculum_id, module_count, modules}.
"""
from __future__ import annotations
import asyncio
import logging
from workers.kanban import TaskHandler

_log = logging.getLogger("teaching.curriculum")


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, payload)


def _run(payload: dict) -> dict:
    topic = (payload.get("topic") or "").strip()
    org_id = int(payload.get("org_id") or 0)
    root_goal = (payload.get("root_goal") or "").strip() or None
    curriculum_id = int(payload.get("curriculum_id") or 0) or None
    learner_note = (payload.get("learner_note") or "").strip() or None

    if not topic:
        return {"status": "failed", "error": "input_payload.topic is required"}
    if not org_id:
        return {"status": "failed", "error": "input_payload.org_id is required"}

    # Implementation:
    # 1. Load learner_concepts for this topic (existing mastery state)
    # 2. Load existing curriculum if curriculum_id provided
    # 3. Call LLM to generate/update modules array, respecting learner state
    # 4. Upsert learner_curricula row
    # 5. Return output payload
    raise NotImplementedError


_type_check: TaskHandler = handle
```

### Registration

```python
_kanban.register("teaching_curriculum", _teaching_curriculum_handler.handle, llm_bound=True)
```

---

## Task type 2: `teaching_lesson`

**Purpose:** The main lesson delivery. Runs a deep search pipeline (same as `research`'s planned search) to build a source-grounded research base for the module, then produces a full lesson: prose explanation, worked examples, diagrams where relevant, comprehension checks, Anki cards, and session summary. Updates the curriculum's `current_module_index` and upserts learner_concepts on completion.

### Input schema (`input_payload`)

```python
{
    "topic": str,           # required
    "org_id": int,          # required
    "curriculum_id": int,   # required — which curriculum to teach from
    "module_id": str,       # required — which module (e.g. "m1") to teach
    "learner_level": str,   # optional — "beginner" | "intermediate" | "advanced"; inferred from learner model if absent
}
```

### Output schema (`output_payload`)

```python
{
    "status": "completed",
    "lesson_id": int,           # NocoDB row Id in teaching_lessons
    "curriculum_id": int,
    "module_id": str,
    "lesson_markdown": str,     # full lesson — long, detailed prose + formatting
    "session_summary": str,     # condensed 1-page summary for the learner record
    "anki_cards": str,          # plain-text Anki import, tab-separated (front\tback\ttags)
    "checks": list[dict],       # [{question, expected_answer, concept, difficulty}]
    "sources": list[dict],      # [{url, title, excerpt}]
    "lesson_path": str,         # path to written lesson .md on disk
    "cards_path": str,          # path to written .txt Anki file on disk
}
```

Comprehension check shape:
```json
{
  "question": "What happens to attention scores geometrically when d_k is large and no scaling is applied?",
  "expected_answer": "Dot products grow large in magnitude, pushing softmax into regions with near-zero gradients — the model stops learning.",
  "concept": "scaled dot-product attention",
  "difficulty": "deep"
}
```

### Handler signature

```python
# workers/task_handlers/teaching_lesson.py
"""Kanban handler for 'teaching_lesson' tasks.

Runs a planned search pipeline to source-ground the lesson, then produces
a full depth lesson for one curriculum module: prose, examples, checks,
Anki cards, and session summary. Updates curriculum and learner_concepts.
Input: {topic, org_id, curriculum_id, module_id, learner_level?}.
Output: {lesson_id, lesson_markdown, session_summary, anki_cards, checks, sources, paths}.
"""
from __future__ import annotations
import asyncio
import logging
from workers.kanban import TaskHandler

_log = logging.getLogger("teaching.lesson")


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, payload)


def _run(payload: dict) -> dict:
    topic = (payload.get("topic") or "").strip()
    org_id = int(payload.get("org_id") or 0)
    curriculum_id = int(payload.get("curriculum_id") or 0)
    module_id = (payload.get("module_id") or "").strip()
    learner_level = (payload.get("learner_level") or "").strip() or None

    if not topic:
        return {"status": "failed", "error": "input_payload.topic is required"}
    if not org_id:
        return {"status": "failed", "error": "input_payload.org_id is required"}
    if not curriculum_id:
        return {"status": "failed", "error": "input_payload.curriculum_id is required"}
    if not module_id:
        return {"status": "failed", "error": "input_payload.module_id is required"}

    # Implementation:
    # 1. Load curriculum row + target module
    # 2. Load learner_concepts for this topic (mastery state, misconceptions)
    # 3. Run research plan (create_research_plan + run_research_planner_job + run_research_agent)
    #    scoped to module objectives — sources the lesson
    # 4. Call lesson LLM: given research base + module objectives + learner state,
    #    produce lesson_markdown (deep prose), session_summary, anki_cards, checks
    # 5. Write lesson.md and cards.txt to disk (teaching output dir)
    # 6. Upsert teaching_lessons row
    # 7. Update curriculum module status → completed, advance current_module_index
    # 8. Upsert learner_concepts (mastery=exposed for concepts covered)
    # 9. Return output payload
    raise NotImplementedError


_type_check: TaskHandler = handle
```

### Registration

```python
_kanban.register("teaching_lesson", _teaching_lesson_handler.handle, llm_bound=True)
```

---

## Task type 3: `teaching_revision`

**Purpose:** Revise a previous lesson based on user feedback. Same shape as `research_revision`. Reads the parent `teaching_lesson` task's `output_payload`, applies revision instructions, and produces updated lesson files. Does not re-run search — works from the existing sources.

### Input schema (`input_payload`)

```python
{
    "parent_task_id": int,          # required — Id of the teaching_lesson task to revise
    "revision_instructions": str,   # required — e.g. "go deeper on the √d_k scaling intuition"
}
```

### Output schema (`output_payload`)

Same shape as `teaching_lesson` output. `lesson_id` will be the same row (updated in place). Includes `revised_from_task_id` for traceability:

```python
{
    "status": "completed",
    "lesson_id": int,
    "revised_from_task_id": int,    # parent_task_id
    "lesson_markdown": str,
    "session_summary": str,
    "anki_cards": str,
    "checks": list[dict],
    "sources": list[dict],          # carried over from parent; may be supplemented
    "lesson_path": str,
    "cards_path": str,
}
```

### Handler signature

```python
# workers/task_handlers/teaching_revision.py
"""Kanban handler for 'teaching_revision' tasks.

Reads the parent teaching_lesson output_payload, applies revision instructions
via the lesson LLM, and produces updated lesson files. No re-search.
Input: {parent_task_id, revision_instructions}.
Output: same shape as teaching_lesson.
"""
from __future__ import annotations
import asyncio
import json as _json
import logging
from workers.kanban import TaskHandler

_log = logging.getLogger("teaching.revision")


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, payload)


def _run(payload: dict) -> dict:
    parent_task_id = int(payload.get("parent_task_id") or 0)
    revision_instructions = (payload.get("revision_instructions") or "").strip()

    if not parent_task_id:
        return {"status": "failed", "error": "input_payload.parent_task_id is required"}
    if not revision_instructions:
        return {"status": "failed", "error": "input_payload.revision_instructions is required"}

    # Implementation:
    # 1. Load parent task row from task_list
    # 2. Parse output_payload → extract lesson_id, sources, curriculum_id, module_id
    # 3. Load teaching_lessons row by lesson_id → get lesson_markdown
    # 4. Call lesson LLM with revision_instructions + original content + sources
    # 5. Overwrite lesson.md and cards.txt on disk
    # 6. Update teaching_lessons row
    # 7. Return output payload with revised_from_task_id
    raise NotImplementedError


_type_check: TaskHandler = handle
```

### Registration

```python
_kanban.register("teaching_revision", _teaching_revision_handler.handle, llm_bound=True)
```

---

## Task type 4: `teaching_check`

**Purpose:** Generate or regenerate standalone comprehension checks against an existing lesson. Runs independently of a lesson delivery — used for spaced repetition scheduling, ad-hoc assessment, or when the user wants to be tested without triggering a new lesson. References the lesson content from `teaching_lessons` via `parent_task_id`.

### Input schema (`input_payload`)

```python
{
    "parent_task_id": int,      # required — Id of the teaching_lesson task to check against
    "concept_focus": list[str], # optional — limit checks to these concepts
    "difficulty": str,          # optional — "introductory" | "working" | "deep" | "mixed" (default: "mixed")
    "count": int,               # optional — number of checks to generate (default: 5)
}
```

### Output schema (`output_payload`)

```python
{
    "status": "completed",
    "lesson_id": int,
    "checks": list[dict],       # [{question, expected_answer, concept, difficulty}]
    "checks_path": str,         # path to written checks.json on disk
}
```

### Handler signature

```python
# workers/task_handlers/teaching_check.py
"""Kanban handler for 'teaching_check' tasks.

Generates comprehension checks against an existing lesson without re-running
the lesson. Used for spaced repetition and ad-hoc assessment.
Input: {parent_task_id, concept_focus?, difficulty?, count?}.
Output: {lesson_id, checks, checks_path}.
"""
from __future__ import annotations
import asyncio
import json as _json
import logging
from workers.kanban import TaskHandler

_log = logging.getLogger("teaching.check")


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, payload)


def _run(payload: dict) -> dict:
    parent_task_id = int(payload.get("parent_task_id") or 0)
    concept_focus: list[str] = payload.get("concept_focus") or []
    difficulty = (payload.get("difficulty") or "mixed").strip()
    count = int(payload.get("count") or 5)

    if not parent_task_id:
        return {"status": "failed", "error": "input_payload.parent_task_id is required"}

    # Implementation:
    # 1. Load parent task row from task_list → parse output_payload → get lesson_id
    # 2. Load teaching_lessons row → get lesson_markdown, existing checks
    # 3. Call check LLM: given lesson content + concept_focus + difficulty + count,
    #    produce checks array in {question, expected_answer, concept, difficulty} shape
    # 4. Write checks.json to disk
    # 5. Return output payload
    raise NotImplementedError


_type_check: TaskHandler = handle
```

### Registration

```python
_kanban.register("teaching_check", _teaching_check_handler.handle, llm_bound=True)
```

---

## Summary: lifespan registrations to add

```python
from workers.task_handlers import teaching_curriculum as _teaching_curriculum_handler
from workers.task_handlers import teaching_lesson as _teaching_lesson_handler
from workers.task_handlers import teaching_revision as _teaching_revision_handler
from workers.task_handlers import teaching_check as _teaching_check_handler

_kanban.register("teaching_curriculum", _teaching_curriculum_handler.handle, llm_bound=True)
_kanban.register("teaching_lesson",     _teaching_lesson_handler.handle,     llm_bound=True)
_kanban.register("teaching_revision",   _teaching_revision_handler.handle,   llm_bound=True)
_kanban.register("teaching_check",      _teaching_check_handler.handle,      llm_bound=True)
```

---

## Task lifecycle for a first lesson on a new topic

```
User: "teach me about Attention in LLMs"
  → submit teaching_curriculum  (topic, org_id, root_goal)
  → on complete: submit teaching_lesson  (curriculum_id, module_id="m1")
  → on complete: lesson delivered to user in chat

User: "go deeper on the softmax scaling"
  → submit teaching_revision  (parent_task_id, revision_instructions)

User: "test me"
  → submit teaching_check  (parent_task_id, difficulty="deep")
```

All four task types are `llm_bound=True`. No teaching task is non-LLM-bound.
