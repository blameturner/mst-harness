# Pipeline Review: Research, Teaching, Project

> **Principle under test:** Each task handler should do one thing — get a task,
> invoke a model with a prompt, return the result, and write to DB. Tools are
> either plain Python functions or model calls in the same mould.

---

## Executive Summary

The core kanban plumbing is clean. The handlers are thin and correct. The
pathology is almost entirely in `tools/research/agent.py` (1301 lines) and
`tools/research/research_planner.py` (769 lines): complexity that should sit
at the handler level has been pulled into a shared tool module, creating a
god-file with multiple levels of abstraction, duplicated JSON-repair code,
and retry/fallback logic that makes failures opaque.

Teaching is close to the principle and mostly fine. Project handlers are not
reviewed here (propose/feature/review are a different concern).

**Where things fail:** research failures are almost always silent or
partial — the code has so many fallback layers that a broken local model
surfaces as "incomplete paper" rather than a clear error, making diagnosis
impossible from logs alone.

---

## 1. Research Pipeline

### Call map

```
kanban task: "research"
  └── workers/task_handlers/research.py :: handle()
        └── _run()
              ├── tools.research.research_planner.create_research_plan()   [DB write]
              ├── tools.research.research_planner.run_research_planner_job()
              │     └── _generate_plan()
              │           └── model_call("research_planner", prompt)        [MODEL CALL 1]
              │                 × retry up to N times
              │                 × fallback: deterministic _fallback_plan()
              └── tools.research.agent.run_research_agent()
                    ├── model_call("research_doc_type", prompt)             [MODEL CALL 2 — optional]
                    └── _build_paper()
                          ├── _fetch_corpus()                               [parallel web search × N queries]
                          │     └── _fetch_corpus_raw()                     [fallback scrape if empty]
                          ├── _write_section(opener)                        [MODEL CALL 3]
                          ├── _write_section() × len(sub_topics) parallel  [MODEL CALLS 4..N]
                          ├── _write_comparison()                           [MODEL CALL N+1]
                          ├── _write_section(closer)                        [MODEL CALL N+2]
                          ├── _write_takeaways_and_recommendation()         [MODEL CALL N+3]
                          └── _write_executive_summary()                    [MODEL CALL N+4]
```

Also: separate kanban tasks `research_planner` and `research_agent` exist as
thin handlers that call the same functions. The `research` task handler
invokes them **inline** (not via kanban), while the standalone handlers are
the "two-step legacy path". This means the same logic runs via two entry
points with slightly different wiring.

### Problems

**1. God file: `tools/research/agent.py` (1301 lines)**

This file mixes: corpus fetch, web search orchestration, section writing,
paper assembly, executive summary, comparison table, review pass,
revision-note generation, and the public entry point. It cannot be
understood in 30 seconds and no single function does one thing.

By the principle, `run_research_agent()` should be:
```python
def run_research_agent(plan_id: int) -> dict:
    plan = load_plan(client, plan_id)
    corpus, sources = fetch_corpus(plan)
    paper = write_paper(plan, corpus)
    save_paper(client, plan_id, paper)
    return {"status": "completed", ...}
```

Each sub-function is a separate concern. Instead, `_build_paper()` alone is
190 lines and calls 6 model functions in sequence and parallel.

**2. Dual entry points for the same work**

`run_research_planner_job(queue_agent=False)` is called by the `research`
handler. `run_research_planner_job(queue_agent=True)` is called by the
`research_planner` kanban handler. Same function, different mode, both
paths live in prod. When something goes wrong, which path ran?

Fix: the `research` handler should be removed in favour of the two-task
kanban path (planner → agent). The inline `_run()` in `research.py`
is redundant orchestration.

**3. Fallback stack hides failures**

Layer 1: retry planner N times with backoff  
Layer 2: `_salvage_queries()` — regex extraction of partial JSON  
Layer 3: `_fallback_plan()` — deterministic fallback without LLM  
Layer 4: section write attempt 1 (full corpus)  
Layer 5: section write attempt 2 (shrunk corpus)  
Layer 6: `_fetch_corpus_raw()` — raw scrape if orchestrator returned nothing  
Layer 7: `_build_generation_notes()` — footer noting missing sections

A complete model failure produces a partial paper with a "Generation notes"
footer and status=`completed`. From the outside: research ran, paper exists,
but it's mostly empty. The user sees a broken document; the operator sees
`status=completed` in the DB. This is the most likely cause of "consistent
failures" — they aren't failures by the system's definition.

**Fix:** if the local model is down, the task should return `status=failed`
with a clear error. Fallbacks are appropriate for transient query failures
(a single search returning nothing); they are not appropriate for the model
itself being unavailable. Add a model-health check at the start of
`run_research_agent` and fail fast.

**4. `_safe_call()` swallows exceptions silently**

```python
def _safe_call(fn, _timeout_unused: float, label: str):
    ...
    except Exception as e:
        _log.warning(...)
        return None
```

Every model call goes through this. A crashed model returns `None`.
The caller checks `if not res: continue`. This is a control-flow exception
pattern masquerading as error handling. The handler never knows why a section
failed.

**5. JSON-repair code duplicated**

`_clean_json_text`, `_strip_fence`, `_extract_json_object`, `_salvage_queries`
in `research_planner.py` are almost identical to `_parse_json_object`,
`_strip_fence`, `_salvage_list` in `tools/teaching/llm.py`. This is four
copies of the same bracket-counting parser. Should be one shared utility.

**6. `_generate_plan()` wraps model_call in a ThreadPoolExecutor**

```python
ex = _futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="research-plan")
fut = ex.submit(_run)
result, _ = fut.result(timeout=timeout_s)
```

`model_call` already runs synchronously with its own httpx timeout. The
comment in `_safe_call()` acknowledges this was the wrong approach and was
removed there — but `_generate_plan` still does it. The thread pool
accomplishes nothing except adding latency and a stale executor warning on
shutdown.

**7. Planner status flow is wrong**

After `run_research_planner_job` writes queries to the DB, it sets
`status="generating"` — but that status means "agent is writing". The planner
sets "generating" on exit; the agent then sets "searching" at start. So
`status=generating` is overloaded: it could mean "planner finished, agent not
yet started" or "agent is halfway done". The stale-reaper (`reap_stale_plans`)
acts on this status. A plan stuck between planner and agent will eventually be
reaped as "wedged".

---

## 2. Teaching Pipeline

### Call map

```
kanban task: "teaching_curriculum"
  └── workers/task_handlers/teaching_curriculum.py :: _run()
        └── tools.teaching.llm.generate_curriculum_modules()
              └── model_call("teaching_curriculum", prompt)              [MODEL CALL 1]
                    × 1 retry

kanban task: "teaching_lesson"  (two-phase)
  Phase 1:
    └── workers/task_handlers/teaching_lesson.py :: _research_phase()
          ├── tools.research.research_planner.create_research_plan()    [DB write]
          └── kanban.submit("research_planner", ...)                    [queues task]
          └── raises TaskNotReady (90s delay)
  Phase 2:
    └── workers/task_handlers/teaching_lesson.py :: _lesson_phase()
          ├── reads research plan from DB (waits if not completed)
          └── tools.teaching.llm.generate_lesson()
                ├── model_call("teaching_lesson", prompt)               [MODEL CALL 2]
                └── _generate_lesson_meta()
                      └── model_call("teaching_lesson_meta", prompt)   [MODEL CALL 3]
```

### Assessment

Teaching is close to the principle. Each handler is short, each model call
is in its own named function, and the two-phase pattern is a reasonable
approach to not blocking the LLM loop on research.

**Minor issues:**

**1. Phase 2 does a `TaskNotReady` poll instead of waiting for a DB event**

```python
if plan_status != "completed":
    raise TaskNotReady(f"research plan {research_plan_id} status={plan_status!r}", delay_seconds=120)
```

This is correct but means the kanban loop will retry `teaching_lesson` every
120s until the research completes. If research takes 20 minutes (8 web
searches + 10 LLM section writes), that's 10 wasted retries. Not harmful,
but noisy.

**2. `generate_lesson` does two model calls behind one function name**

`generate_lesson()` calls `model_call("teaching_lesson", ...)` then calls
`_generate_lesson_meta()` which calls `model_call("teaching_lesson_meta", ...)`.
The lesson handler sees one function call but two model round-trips happen.
This is hidden complexity. The meta call should be a separate explicit step
in the handler.

**3. `_generate_lesson_meta` silently returns empty on failure**

```python
text, _ = model_call("teaching_lesson_meta", prompt)
if not text:
    _log.warning("teaching_lesson_meta returned empty — using fallback metadata")
    return "", "", []
```

The lesson is saved with no anki cards and no checks. The output dict
reports `"checks": []`. This is silent degradation. A lesson without checks
is incomplete, and the handler should either fail or flag it clearly in the
output.

**4. Duplicate `_strip_fence` / `_parse_json_object` vs research**

Already noted above. These are copy-pasted into `tools/teaching/llm.py`.

---

## 3. Project Pipeline (brief)

Not in scope of detailed review but one structural note:

`project_propose.py` and `project_feature.py` both follow the principle
cleanly: get task → call model → queue downstream tasks → return. The
`project_autonomy` guardrails are a clean pre-check. No issues flagged here.

---

## Summary: Root Causes of Consistent Failures

| Symptom | Likely cause |
|---|---|
| Research "completes" with thin/missing sections | Fallback stack masks model unavailability as partial success |
| Research fails intermittently with no clear error | `_safe_call()` swallows exceptions; `status=failed` in DB but no log chain |
| Research plan stuck in "generating" for hours | Status overloaded between planner-done and agent-start; reaper fires |
| Research never starts after planner queued it | `queue_agent=True` path fails silently if `kanban.submit` throws |
| Teaching lesson with no anki cards or checks | `_generate_lesson_meta` degrades silently to `return "", "", []` |
| Hard to reproduce failures locally | Dual entry points (inline vs kanban) mean dev path ≠ prod path |

---

## Recommended Changes (priority order)

**1. Fail fast on model unavailability** (high impact, low risk)  
Add a pre-flight `model_call` health check in `run_research_agent`. If the
first call returns empty, fail the task immediately rather than building
through 6 fallback layers. The paper save is already split from status writes,
so this doesn't lose data.

**2. Remove the inline `research` handler** (medium impact, medium risk)  
Delete `workers/task_handlers/research.py`'s `_run()` and route everything
through the two-task kanban path (`research_planner` → `research_agent`). The
duplicate entry point is the most likely source of "runs fine manually, fails
in prod" issues.

**3. Fix planner status: "generating" → "planned"** (low risk)  
After the planner writes queries, set `status="planned"` not `"generating"`.
Reserve `"generating"` for when the agent is actually writing. The reaper
logic then has an unambiguous signal.

**4. Extract JSON-repair utilities** (low risk)  
`_strip_fence`, `_clean_json_text`, `_extract_json_object`, `_salvage_list`
appear in at least 3 files. Move to `shared/json_utils.py`.

**5. Surface `_generate_lesson_meta` as an explicit call** (low risk)  
In `teaching_lesson.py::_lesson_phase`, call the meta generator explicitly
and check its output. If empty, either retry or mark `checks_status="unavailable"`
in the output rather than silently returning `[]`.

**6. Remove the ThreadPoolExecutor from `_generate_plan`** (low risk)  
Replace:
```python
ex = _futures.ThreadPoolExecutor(...)
fut = ex.submit(_run)
result, _ = fut.result(timeout=timeout_s)
```
With:
```python
result, _ = model_call("research_planner", prompt)
```
The global httpx timeout is the correct control.