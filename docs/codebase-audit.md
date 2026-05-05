# Codebase Audit

---

## 10. Research Agent Module — Full Diagnostic

### 10.1 Function and Class Map

#### `tools/research/research_planner.py`

| Symbol | Kind | Description |
|---|---|---|
| `_emit_progress` | fn | Calls progress_cb with message + step metadata; eats all exceptions |
| `_fallback_plan` | fn | Deterministic plan generator when LLM fails; builds generic queries from topic string alone, no model call |
| `_planner_timeout_s` | fn | Reads `research.planner_timeout_s` config; returns int seconds |
| `_planner_retry_attempts` | fn | Reads `research.planner_retry_attempts` config |
| `_planner_retry_backoff_s` | fn | Reads `research.planner_retry_backoff_s` config |
| `_strip_fence` | fn | Strips ` ```json ``` ` fences from raw model output |
| `_extract_json_object` | fn | Brace-balanced JSON extractor; handles truncated output with best-effort close |
| `_clean_json_text` | fn | Normalises smart quotes and trailing commas in local-model JSON |
| `_salvage_queries` | fn | Last-resort: pulls just the `queries` array from malformed model output using regex |
| `_as_string_list` | fn | Coerces any value to `list[str]` |
| `_topic_keywords` | fn | Extracts word-level keywords from topic string for low-signal query detection |
| `_is_low_signal_query` | fn | Rejects queries that are too short, too generic, or don't share keywords with topic |
| `_normalize_plan_payload` | fn | Dedupes and filters queries; validates schema value types; backfills default schema if empty |
| `_default_schema_for_topic` | fn | Returns a generic 9-column comparison schema when LLM omitted one |
| `_generate_plan` | fn | Main LLM call: prompts `research_planner` model; retries with backoff; falls back to `_fallback_plan`; uses a `ThreadPoolExecutor` solely for timeout enforcement |
| `create_research_plan` | fn | **Public API entry point.** Creates a `research_plans` shell row in NocoDB and submits a `research_planner` Kanban task (or defers if `defer_run=True`) |
| `run_research_planner_job` | fn | **Kanban handler body.** Called by Kanban worker; calls `_generate_plan`, patches plan row with queries/schema, submits `research_agent` Kanban task |
| `start_research_plan` | fn | Activates a deferred (hidden) plan row and submits `research_planner` Kanban task |
| `get_next_plan` | fn | Polls NocoDB for a `status=generating` plan; used by legacy polling paths |
| `complete_plan` | fn | Sets `status` on a plan row; used by legacy polling paths |
| `reap_stale_plans` | fn | APScheduler job (every 30 min): marks plans stuck in transient states as failed if no inflight Kanban job references them and they're older than `stale_plan_hours` |

#### `tools/research/agent.py`

| Symbol | Kind | Description |
|---|---|---|
| `DOC_TYPES` | dict | 24-entry registry of document format definitions; each entry has `opener`, `closer`, `tone`, `summary_role` |
| `DEFAULT_DOC_TYPE` | const | `"research_report"` |
| `_PROTECTED_SECTIONS` | const | `{"Executive Summary", "Key Takeaways", "Sources"}` — reviewer cannot revise these |
| `_skip_doc_type_inference_basic` | fn | Reads `research.basic_skip_doc_type_inference` feature flag |
| `_research_timeout` | fn | Reads named timeout key from `research` feature config |
| `_safe_json_loads` | fn | `json.loads` with fallback on any exception |
| `_patch_or_log` | fn | `client._patch` with WARNING-level logging on failure |
| `_safe_call` | fn | Wraps a callable, logs entry/return/error with timing; `timeout_s` arg is accepted but **ignored** (timeout is handled by model pool / httpx) |
| `_research_intent_dict` | fn | Builds a search intent dict with `SEARCH_POLICY_FULL` and `CHAT_INTENT_RESEARCH` |
| `_fetch_corpus` | fn | Runs all planner queries through `run_web_search` in a `ThreadPoolExecutor` (max 4 concurrent); dedupes sources; falls back to `_fetch_corpus_raw` if all results are empty |
| `_fetch_corpus_raw` | fn | Last-resort: calls `searxng_search` then `scrape_page` directly (no LLM in the loop); caps at 3 queries, 10 results, 6 pages, 4000 chars/page |
| `_infer_doc_type` | fn | Calls `research_doc_type` model to classify topic into one of 24 types; returns `DEFAULT_DOC_TYPE` on any failure |
| `_section_prompt` | fn | Builds the per-section LLM prompt string from doc_type spec, corpus, hypotheses, optional revision note |
| `_write_section` | fn | Calls `model_call("research_section_writer")` with up to 3 attempts (progressively smaller context and token budget); returns `str` or `None` |
| `_write_executive_summary` | fn | Calls `model_call("research_section_writer")` on the assembled body; returns `str` or `None` |
| `_write_comparison` | fn | Calls `model_call("research_section_writer")` to produce a markdown table from schema columns; skipped if schema is empty |
| `_write_takeaways_and_recommendation` | fn | Calls `model_call("research_section_writer")` for `## Key Takeaways` + `## Recommendation`; returns `str` or `None` |
| `_splice_section` | fn | Regex-replaces a named `## heading` in paper markdown; appends if heading not found |
| `_build_sources` | fn | Builds `## Sources` markdown block from deduplicated source dicts |
| `_build_generation_notes` | fn | Renders a `## Generation notes` footer listing sections that failed; empty string if nothing failed |
| `_build_paper` | fn | **Core synthesis engine.** Fetches corpus, writes opener → body sections (parallel, max 3) → comparison → closer → takeaways → exec summary; raises `RuntimeError` only if nothing at all produced |
| `run_research_agent` | fn | **Kanban handler body.** Loads plan, resolves doc_type, calls `_build_paper`, saves `paper_content` to NocoDB, then ingests into RAG (`ingest_output`) and optionally appends to insight (`append_research`) |
| `review_research_paper` | fn | **Kanban handler body for review.** Loads existing paper, calls `_generate_revision_notes`, fetches fresh corpus, re-runs only flagged sections via `_write_section`, splices back into paper with `_splice_section` |
| `_generate_revision_notes` | fn | Calls `model_call("research_reviewer")` with the full paper; parses JSON response; filters out protected sections; returns `{section_title: instruction}` |
| `get_next_research` | fn | Polls NocoDB for `status=generating` plan; legacy |

#### `tools/research/critic.py`

| Symbol | Kind | Description |
|---|---|---|
| `_strip_fence` | fn | Strip markdown code fences |
| `_extract_json_object` | fn | Brace-balanced JSON extractor |
| `_coerce_gaps` | fn | Validates gap items into `{field, status, needed}` dicts |
| `_coerce_queries` | fn | Dedupes and caps query list at 8 |
| `_fallback_response` | fn | Returns a safe low-confidence critic result on any error |
| `_normalize_critic_output` | fn | Validates and clamps critic JSON fields; auto-generates queries from gaps if none provided |
| `analyze_gaps` | fn | **Public.** Calls `model_call("research_agent")` (function name configurable via `research.critic_model`); returns gap analysis dict |
| `get_confidence_threshold` | fn | Reads `research.confidence_threshold` config (default 80) |

> **Note:** `critic.py` is **not called** from anywhere in the current research flow. The iterative critic loop was removed. `analyze_gaps` exists but has no caller in production code.

#### `tools/research/operations.py`

| Symbol | Kind | Description |
|---|---|---|
| `_load_plan` | fn | Fetches a `research_plans` row by id; returns `(client, plan)` |
| `_doc_type_of` | fn | Reads `_doc_type` from schema JSON or infers via `_infer_doc_type` |
| `_load_schema` | fn | Parses `schema` JSON column from plan row |
| `_load_artifacts` | fn | Reads `schema._artifacts` dict from plan row |
| `_save_artifact` | fn | Upserts an artifact into `schema._artifacts` and patches NocoDB |
| `_section_split` | fn | Splits paper markdown into `[(full_head, title, body)]` tuples on `##` headings |
| `_preamble` | fn | Returns text before the first `##` heading |
| `_replace_section` | fn | Replaces the body of a named `##` section in-place |
| `_insert_section_before` | fn | Inserts a new `##` section before a named target |
| `_insert_section_after` | fn | Inserts a new `##` section after a named target |
| `_build_corpus` | fn | Thin wrapper: reads `queries` from plan row, calls `_fetch_corpus` |
| `fact_check_paper` | fn | Op: calls `model_call("research_reviewer")` against corpus + paper; saves artifact `fact_check` |
| `citation_audit_paper` | fn | Op: **no LLM.** Regex-parses inline `[Source: URL]` and `## Sources` block; saves artifact `citation_audit` |
| `expand_section` | fn | Op: calls `_write_section` with larger `target_words`; replaces section in paper |
| `add_new_section` | fn | Op: calls `_write_section`; inserts at specified position |
| `add_counter_arguments` | fn | Convenience wrapper over `add_new_section` with fixed heading and brief |
| `add_fresh_sources` | fn | Op: optionally calls `model_call("research_planner")` to generate queries, then calls `_fetch_corpus` and `_write_section`; inserts new section before `## Sources` |
| `refresh_for_recency` | fn | Convenience: appends current year to existing queries, delegates to `add_fresh_sources` |
| `reframe_for_audience` | fn | Op: whole-paper rewrite via `model_call("research_reviewer", max_tokens=24000)` |
| `resize_paper` | fn | Op: whole-paper resize via `model_call("research_reviewer")` |
| `_generic_artifact` | fn | Shared template for artifact-producing ops: loads paper, calls `model_call`, saves artifact |
| `generate_slide_deck` | fn | Op: calls `_generic_artifact` with slide deck prompt |
| `generate_email_tldr` | fn | Op: calls `_generic_artifact` with email digest prompt |
| `generate_qa_pack` | fn | Op: calls `_generic_artifact` with Q&A prompt |
| `generate_action_plan` | fn | Op: calls `_generic_artifact` with action plan checklist prompt |
| `chat_with_paper` | fn | Op: **no LLM.** Creates a `conversations` row in NocoDB with a `system_note` that binds it to this plan |
| `ASYNC_OPS` | dict | 12 named ops dispatched through the Kanban `research_op` handler |
| `SYNC_OPS` | dict | 2 ops (`citation_audit`, `chat_with_paper`) that run inline (but currently dispatched through the same Kanban handler — the SYNC label is vestigial) |
| `run_research_op` | fn | **Kanban handler body.** Resolves `kind` → op function in `ASYNC_OPS ∪ SYNC_OPS`; calls it; logs |

#### `tools/research_seeder/agent.py`

| Symbol | Kind | Description |
|---|---|---|
| `_cfg` | fn | Reads `research_seeder.*` config key |
| `_parse_iso` | fn | ISO datetime parser with timezone coercion |
| `_recent_rows_with_python_cutoff` | fn | NocoDB paginated fetch with Python-side date filtering fallback (NocoDB `gt` filter format varies) |
| `_existing_plan_topics` | fn | Returns `set[str]` of topics already researched in the past 7 days |
| `_candidate_topics` | fn | Fetches warm PA topics (`kind=task`, touched in last 24h), ranked by `warmth × engagement_bias` |
| `_candidate_decisions` | fn | Fetches open decision-intent PA loops created in last 24h |
| `run_research_seeder` | fn | **Entry point.** Interleaves topics and decisions; calls `create_research_plan` for up to `max_topics_per_night` (default 2); no LLM |

#### Worker handlers (`workers/task_handlers/`)

| File | Handler | What it does |
|---|---|---|
| `research_planner.py` | `handle(task)` | Unwraps `input_payload.plan_id`; delegates to `run_research_planner_job(plan_id)` via `asyncio.to_thread` |
| `research_agent.py` | `handle(task)` | Unwraps `input_payload.plan_id`; delegates to `run_research_agent(plan_id)` via `asyncio.to_thread` |
| `research_review.py` | `handle(task)` | Unwraps `plan_id` + `instructions`; delegates to `review_research_paper(plan_id, instructions)` via `asyncio.to_thread` |
| `research_op.py` | `handle(task)` | Passes full `input_payload` dict to `run_research_op(payload)` via `asyncio.to_thread` |

---

### 10.2 Full Call Chain — Normal Research Run

```
HTTP POST /insights/{id}/research  (app/routers/home.py:insight_deep_dive)
  → create_research_plan(topic, org_id, parent_insight_id)        [research_planner.py]
      → NocodbClient._post("research_plans", {..., status="pending"})
      → kanban.submit(client, "research_planner", {plan_id, org_id}, ...)

Kanban Loop A picks up "research_planner" task
  → workers/task_handlers/research_planner.py:handle(task)
      → asyncio.to_thread(run_research_planner_job, plan_id)       [research_planner.py]
          → NocodbClient._get("research_plans")           # load plan row
          → _generate_plan(topic, max_queries, progress_cb)
              → model_call("research_planner", prompt)    # ThreadPoolExecutor for timeout
              → [retry loop] → _fallback_plan() if all retries fail
          → NocodbClient._patch("research_plans", plan_id, {hypotheses, sub_topics, queries, schema, status="generating"})
          → kanban.submit(client, "research_agent", {plan_id, org_id}, ...)

Kanban Loop A picks up "research_agent" task
  → workers/task_handlers/research_agent.py:handle(task)
      → asyncio.to_thread(run_research_agent, plan_id)             [agent.py]
          → NocodbClient._get("research_plans")           # load plan row
          → _infer_doc_type(topic)                        # optional model_call("research_doc_type")
          → NocodbClient._patch("research_plans", plan_id, {status="searching"})
          → _build_paper(topic, doc_type, queries, schema, hypotheses, sub_topics, org_id)
              → _fetch_corpus(topic, queries, org_id)     # ThreadPoolExecutor, max 4 workers
                  → run_web_search(q, org_id, ...) × N queries     [search/orchestrator.py]
                  → [if all empty] → _fetch_corpus_raw()
                      → searxng_search() × up to 3 queries
                      → scrape_page() × up to 10 URLs
              → _write_section("opener") × 1             # model_call("research_section_writer")
              → _write_section(sub_topic) × N sections   # ThreadPoolExecutor, max 3 workers
              → _write_comparison()                       # model_call("research_section_writer") if schema
              → _write_section("closer") × 1             # model_call("research_section_writer")
              → _write_takeaways_and_recommendation()     # model_call("research_section_writer")
              → _write_executive_summary()               # model_call("research_section_writer")
          → NocodbClient._patch("research_plans", plan_id, {paper_content: paper})
          → NocodbClient._patch("research_plans", plan_id, {status="completed", ...})
          → ingest_output(paper, org_id, rag_collection="research",
                          knowledge_collection="research_knowledge")  [workers/post_turn.py]
              → infra.memory.remember(text, collection="research")        # Chroma embed
              → infra.memory.remember(text, collection="research_knowledge")
          → shared.insights.append_research(plan_id, paper, focus)   [if parent_insight_id set]
```

**Alternative entry point — user creates plan directly (HTTP POST /research/new):**
```
HTTP POST /research/new  (app/routers/enrichment.py)
  → create_research_plan(topic, org_id)
    [same chain as above from kanban.submit("research_planner") onward]
```

**Alternative entry point — nightly seeder (APScheduler `_research_seeder_tick`):**
```
scheduler.py:_research_seeder_tick()
  → run_research_seeder(org_id)                                    [research_seeder/agent.py]
      → _candidate_topics() + _candidate_decisions()               # NocoDB reads, no LLM
      → create_research_plan(topic, org_id) × up to 2 topics
        [same chain from kanban.submit("research_planner") onward]
```

---

### 10.3 Every Search Invocation

| Call site | File:line | Invocation pattern | When called |
|---|---|---|---|
| `_fetch_corpus` → `run_web_search` | `agent.py:334` | `run_web_search(q, org_id=org_id, intent_dict=intent, extraction_function_name=extraction_fn)` | During `_build_paper`, once per planner query, parallel up to 4 threads |
| `_fetch_corpus_raw` → `searxng_search` | `agent.py:427` | `searxng_search(q, max_results=8)` | Fallback only when all `run_web_search` calls returned empty; first 3 queries only |
| `_fetch_corpus_raw` → `scrape_page` | `agent.py:441` | `scrape_page(url, r.get("snippet", ""))` | After `searxng_search` in raw fallback; up to 10 URLs, first 6 that return text |
| `_build_corpus` → `_fetch_corpus` | `operations.py:181` | Same signature as above | Called by every op that needs fresh corpus: `fact_check_paper`, `expand_section`, `add_new_section`, `add_counter_arguments`, `add_fresh_sources` |
| `add_fresh_sources` → `_fetch_corpus` | `operations.py:409` | Same signature, using newly-generated or caller-supplied queries | After fresh query generation |
| `review_research_paper` → `_fetch_corpus` | `agent.py:1145` | Same signature | During review pass to get fresh corpus for section rewrites |

---

### 10.4 Every Branch and Loop

#### `_generate_plan` (research_planner.py)

- Retry loop: `for attempt in range(1, attempts + 1)` (default 2 attempts, 4s backoff between)
- Branch: raw model result → `_extract_json_object` → `_clean_json_text` → `json.loads`
- Branch on parse failure: `_salvage_queries` to pull queries array only
- Branch: if `normalized` has `"error"` key → continue retry loop
- Branch after all retries: if `research.planner_fallback_enabled` → `_fallback_plan`; else return error dict

#### `_fetch_corpus` (agent.py)

- Parallel loop: `ThreadPoolExecutor(max_workers=min(4, n_queries))` over queries
- Cancellation check: `if is_job_cancelled()` inside `_one()` and in `as_completed` loop → raises `JobCancelled`
- Branch: `if not res` or `if not ctx` → skip (continue collecting)
- Branch: URL deduplication `if url in seen_urls: continue`
- Branch after parallel: `if not out_corpus.strip()` → call `_fetch_corpus_raw` fallback

#### `_write_section` (agent.py)

- Retry loop: `for n in range(1, 4)` — 3 attempts with shrinking context/token budget
  - Attempt 1: `corpus[:30000]`, no `max_tokens` cap
  - Attempt 2: `corpus[:16000]`, `max_tokens=3000`
  - Attempt 3: `corpus[:8000]`, `max_tokens=1500`
- Branch on each attempt: `if not res` → `last_err="timeout_or_error"` → continue
- Branch: `if not text` → `last_err="empty"` → continue
- Returns `None` after 3 failures

#### `_build_paper` (agent.py)

- Branch: `if not queries` → raise `RuntimeError` immediately
- Branch: `if not corpus.strip()` → raise `RuntimeError`
- Parallel loop (body sections): `ThreadPoolExecutor(max_workers=min(3, n_sub_topics))` 
- Cancellation check inside body thread: `if _cancelled()` → raises `JobCancelled`
- Branch: `if schema` → call `_write_comparison`; else skip
- Sanity gate: `if not opener and not closer and n_body_ok == 0` → raise `RuntimeError`
- All other partial failures (some body sections empty, opener/closer missing) → log warning, continue

#### `run_research_agent` (agent.py)

- Branch: feature flag `research.agent_enabled` → return disabled dict
- Branch: `if not plan` → return not_found
- Branch: `_doc_type` already in schema → use it; else `_skip_doc_type_inference_basic` check; else `_infer_doc_type`
- Branch: `if is_job_cancelled()` before search → raise `JobCancelled`
- Branch: `if not paper or not paper.strip()` → patch failed, return
- Separate patches: `paper_content` saved first; each metadata field in its own patch (any individual rejection is logged but not fatal)
- Branch: `ingest_output` failure → WARNING log, not fatal
- Branch: `append_research` failure → WARNING log, not fatal

#### `review_research_paper` (agent.py)

- Branch: plan not found → return
- Branch: no prior paper → return failed
- Branch: `_generate_revision_notes` returns empty dict → preserve prior paper, mark complete, return
- Loop: `for sec_title, note in revision_notes.items()` → `_write_section` + `_splice_section`
- Branch: `if not sec_md` → skip (continue to next section)
- Branch: `if revised_count == 0 or not new_paper` → mark completed with error note, return failed

#### `run_research_op` (operations.py)

- Branch: `if not plan_id or not kind` → return failed
- Branch: `fn = ASYNC_OPS.get(kind) or SYNC_OPS.get(kind)` → if `None` → return failed (unknown kind)
- Branch: `if not isinstance(result, dict)` → return failed

#### `_generate_revision_notes` (agent.py)

- Branch: `if not res` → return `{}`
- Branch: fence stripping (starts with ` ``` `)
- JSON parse with fallback: bracket search on `JSONDecodeError`
- Branch: `if not isinstance(parsed, dict)` → return `{}`
- Loop: `for r in revisions` — skip items without `section` or `instructions`, skip protected sections

---

### 10.5 Output File Writes

The research module writes **no files to disk**. All output is persisted to NocoDB rows and Chroma vector collections.

| Output | Location | Written by | Column/Collection |
|---|---|---|---|
| Paper content (markdown) | NocoDB `research_plans` | `run_research_agent`, `review_research_paper`, all ops that mutate paper | `paper_content` column |
| Plan metadata (queries, schema, hypotheses, sub_topics) | NocoDB `research_plans` | `run_research_planner_job` | `hypotheses`, `sub_topics`, `queries`, `schema` columns |
| Status transitions | NocoDB `research_plans` | `run_research_planner_job`, `run_research_agent`, `review_research_paper` | `status` column (`pending` → `generating` → `searching` → `completed`/`failed`) |
| Artifacts (slide deck, email, Q&A, etc.) | NocoDB `research_plans.schema._artifacts` (nested JSON) | `_save_artifact` in operations.py | `schema` column (JSON dict with `_artifacts` key) |
| Conversation link (chat_with_paper) | NocoDB `conversations` | `chat_with_paper` | New row with `system_note` binding to plan |
| RAG embeddings | Chroma collection `research` | `ingest_output` → `infra.memory.remember` | Called after paper saved |
| Knowledge embeddings | Chroma collection `research_knowledge` | `ingest_output` → `infra.memory.remember` | Called after paper saved |
| Insight append | NocoDB `insights.body` (or equivalent) | `shared.insights.append_research` | Called if `parent_insight_id` was set |

---

### 10.6 Kanban Tasks — Enqueued or Depended On

| Task type | Registered in | `llm_bound` | Who submits it | What it triggers |
|---|---|---|---|---|
| `research_planner` | `app/lifespan.py:82` | `True` | `create_research_plan()`, `start_research_plan()` | On completion, submits `research_agent` |
| `research_agent` | `app/lifespan.py:83` | `True` | `run_research_planner_job()` (end of planner task) | No further task; terminal |
| `research_review` | `app/lifespan.py:84` | `True` | `POST /research/{plan_id}/review` (enrichment router) | No further task; terminal |
| `research_op` | `app/lifespan.py:85` | `True` | `POST /research/{plan_id}/op/{kind}` (enrichment router) | No further task; terminal |

**No Huey tasks are enqueued or used by the research module.** All four task types live entirely within Kanban Loop A (LLM-bound serial executor).

---

### 10.7 Orphan: `tools/research/critic.py`

`critic.py` (`analyze_gaps`, `get_confidence_threshold`) is **unreachable in production**. The iterative critic loop was removed from `agent.py`. The module is imported nowhere. It is dead code.

---

## 1. Graph Search UI — Frontend & Backend

**No frontend UI exists in this repo.** Graph search is API-only; the UI lives in a separate frontend repository.

### Backend — Core Infrastructure

| File | Role |
|---|---|
| `infra/graph.py` | FalkorDB access layer: entity/edge queries, neighbourhood fetching, alias merging, edge decay, orphan pruning |
| `infra/config.py` | `scoped_graph(org_id)` — returns org-scoped FalkorDB graph name |
| `shared/graph_recall.py` | Injects graph-expanded entity facts into chat system prompts; 5-min entity cache |
| `shared/relationships.py` | Entity-relationship JSON schema and extraction prompt (CAUSES, ENABLES, REQUIRES, etc.) |

### Backend — Extraction & Maintenance

| File | Role |
|---|---|
| `tools/graph_extract.py` | Tool queue handler that triggers post-turn graph extraction; delegates to `workers/chat/graph.py` |
| `tools/graph_maintenance/agent.py` | Scheduled alias resolution (daily) and edge decay/pruning (weekly) jobs |
| `tools/graph_maintenance/dispatcher.py` | APScheduler wrapper that enqueues graph maintenance jobs to the tool queue |
| `workers/chat/graph.py` | Extracts entity relationships from chat turns and writes to FalkorDB + Chroma |
| `workers/post_turn.py` | Post-turn orchestrator; queues `graph_extract` after embeds |
| `workers/tool_queue.py` | Registers `graph_extract`, `graph_resolve_entities`, `graph_maintenance` handlers |

### Backend — API Routers

| File | Endpoints |
|---|---|
| `app/routers/home.py` | `POST /graph/search`, `GET /graph/entity`, `POST /graph/ask`, `POST /graph/path`, `GET /graph/entity/{name}/timeline`, `GET /graph/export`, `POST /graph/entity/merge`, `GET /widgets/graph`, `POST /graph/resolve-entities` |
| `app/routers/stats.py` | `GET /graph/snapshot` — top-degree entities, sparse concepts, top-weighted edges |
| `app/routers/projects_analysis.py` | `GET /{project_id}/graph` — code import graph (files as nodes, imports as edges) |
| `app/routers/admin.py` | Admin UI config for graph job controls and feature toggles |
| `app/lifespan.py` | Registers all graph handlers and schedules dispatchers at startup |

### Tests

| File | Role |
|---|---|
| `test_graph_neighbourhood.py` | Unit tests for graph query functions and entity caching |

---

## 2. Huey Task Definitions

There are exactly **2 Huey tasks** (`@huey.task()`), both in `infra/huey_runtime.py`. Everything else uses APScheduler.

| Function | File | What invokes it | Long-running or could be sync? | Visible state or plumbing? |
|---|---|---|---|---|
| `run_tool_job(job_id)` | `infra/huey_runtime.py:233` | `workers/tool_queue.py` → `_dispatch_to_huey()` → `enqueue_tool_job(job_id)`; triggered by the per-type worker threads inside `ToolJobQueue._worker_loop()` | **Genuinely long-running.** Delegates to registered handlers; research jobs time out at 1800 s, harvest jobs up to 2 h. Must be async. | **Visible state.** Updates NocoDB job rows (`status`, `progress`, `result`, `error`). Emits `job_dispatched / job_progress / job_completed / job_failed` events that drive SSE streams the UI subscribes to. |
| `heartbeat()` | `infra/huey_runtime.py:253` | `infra/huey_runtime.py` → `_monitor_loop()` enqueues every 60 s; started by `_start_health_monitor_locked()` at FastAPI lifespan | **Could be sync.** Work is a single float write (~1 ms). Runs through Huey deliberately as a liveness probe — if the worker queue stops draining, the heartbeat won't fire and the health monitor restarts the consumer. | **Pure plumbing.** Writes to module-scope `_heartbeat_ran_at` only. Surfaced read-only in `GET /tool-queue/status` but produces no application state change. |

### APScheduler background ticks (not Huey) in `scheduler.py`

| Function | Description |
|---|---|
| `_run_agent_job()` | Fires scheduled user agents from NocoDB config |
| `_pa_tick()` | 20-min proactive assistant moves for all orgs |
| `_anchored_asks_tick()` | Fires 5 min before daily brief slots to produce deterministic questions |
| `_daily_brief_tick()` | Long-form briefing producer on schedule |
| `_research_seeder_tick()` | Nightly research kickoff seeding 1–2 plans |

---

## 3. Enrichment Abstractions

Enrichment workers are **plain handler functions**, not class instances — there is no enrichment-specific base class. Coordination happens entirely through the tool queue.

### Core Queue Infrastructure (`workers/tool_queue.py`)

| Abstraction | Type | Role |
|---|---|---|
| `ToolJobQueue` | Class | Central registry + dispatcher: registers handlers, manages per-type worker threads, enforces chat-idle gating, runs the execution loop |
| `HandlerConfig` | Dataclass | Registration metadata per job type: handler fn, max_workers, priority, dedup key, source tag |
| `ToolJob` | Dataclass | Universal job representation: type, payload, status, priority, progress, org_id, dependencies, result/error |

### Periodic Dispatchers (`tools/enrichment/dispatcher.py`)

These detect work on a schedule and enqueue jobs — they do no enrichment themselves.

| Function | Schedule | Role |
|---|---|---|
| `jumpstart_scraper(org_id)` | Every 60s | Selects oldest-due `scrape_target`, enqueues one `scrape_page` job; backs off when queue is loaded |
| `jumpstart_pathfinder(org_id)` | Every 120s | Selects oldest-approved `suggested_scrape_target`, enqueues one `pathfinder_extract` job |
| `jumpstart_discover_agent(org_id)` | Every 20min | Enqueues one `discover_agent_run` if none are currently inflight |
| `_background_dispatch_allowed(tq)` | (predicate) | Gating check — returns False when the queue signals backoff |

### Follow-up Coordinator (`tools/enrichment/scraper.py`)

| Function | Role |
|---|---|
| `_enqueue_followups()` | After a successful scrape, enqueues `extract_relationships` (priority 5) and `summarise_page` (priority 4) for the same content |

### Registration & HTTP Bridge

| File | Role |
|---|---|
| `app/lifespan.py` | Registers all 5 enrichment handlers at startup (`scrape_page`, `pathfinder_extract`, `extract_relationships`, `summarise_page`, `discover_agent`) |
| `app/routers/enrichment.py` | HTTP → tool queue bridge: user-facing endpoints that resolve org context and enqueue jobs (approve, run-now, manual seed) |

### Worker Base Class (chat/code only)

`workers/base.py` — `BaseAgent` is used by chat and code agents. Enrichment workers do **not** extend it.

---

## 4. Dead or Orphaned Modules

**The codebase is clean.** Only one candidate was found:

| File | Status |
|---|---|
| `scripts/migrate_codebases_to_projects.py` | Has `if __name__ == "__main__"` — intentional one-off migration script, never imported. Expected orphan. |

All other modules, including all harvest policies under `tools/harvest/policies/`, are reachable through the import graph. Empty `__init__.py` files are standard namespace packages.

---

## 5. NocoDB `task_list` Table

Kanban substrate for the agent task queue. Table name is `task_list` (NocoDB reserved `tasks`). `Id`, `CreatedAt`, `UpdatedAt` are auto-created by NocoDB.

| Field | NocoDB type | Notes |
|---|---|---|
| `task_type` | SingleLineText | e.g. `research`, `project_code`, `project_review`, `generic` |
| `status` | SingleSelect | `ready`, `claimed`, `running`, `done`, `failed`, `blocked` — default `ready` |
| `agent` | SingleLineText | Which agent owns the row |
| `model` | SingleLineText | Model identifier; empty = use agent default |
| `prompt_template_id` | SingleLineText | References a prompt file by name |
| `input_payload` | JSON | Task input |
| `output_payload` | JSON | Task result (optional) |
| `parent_task_id` | Number | NocoDB `Id` of parent row; empty = top-level task |
| `created_by` | SingleLineText | e.g. `user`, `agent:research` |
| `started_at` | DateTime | Optional |
| `completed_at` | DateTime | Optional |
| `error` | LongText | Optional |
| `retry_count` | Number | Default `0` |
| `not_before` | DateTime | Retry backoff gate; worker skips rows where `not_before > now` |

---

## B.3. Kanban Worker (`workers/kanban.py`)

Two cooperating async loops that drain `task_list`. No Huey inside this module — plain asyncio.

### Concurrency model

- **LLM-bound tasks** run strictly one at a time globally (Loop A).
- **Non-LLM tasks** run concurrently up to N (Loop B, default N=4).
- Each handler registration declares `llm_bound: bool`.

### Registry

```python
@dataclass(frozen=True)
class HandlerEntry:
    handler: TaskHandler   # async (task: dict) -> dict
    llm_bound: bool

_registry: dict[str, HandlerEntry] = {}

def register(task_type: str, handler: TaskHandler, *, llm_bound: bool) -> None: ...
```

### Loop A — LLM scheduler

- Polls `task_list` for `status=ready` rows whose `task_type` is registered as `llm_bound=True`.
- Before claiming: checks `is_chat_active()` from `workers.tool_queue`; sleeps `kanban.llm_grace_seconds` (default 2 s) while chat is active.
- Before claiming: enforces `kanban.llm_min_spacing_seconds` (default 2 s) since last LLM task completion.
- Claims via optimistic patch + re-fetch (NocoDB has no `SELECT … FOR UPDATE`).
- On success → `status=done`, writes `output_payload` and `completed_at`.
- On failure → exponential backoff retry (delays: 1 s, 2 s, 4 s); after 3 attempts → `status=failed`.

### Loop B — non-LLM dispatcher

- Same claim/complete/fail/retry semantics as Loop A.
- No chat-active gate, no min-spacing.
- Uses `asyncio.Semaphore(N)` to cap concurrent tasks at `kanban.non_llm_concurrency` (default 4).
- Cancels in-flight tasks cleanly on shutdown.

### Startup (supervisor pattern)

```python
db = NocodbClient()
llm_task     = asyncio.create_task(kanban.run_llm_loop(db),     name="kanban-llm")
non_llm_task = asyncio.create_task(kanban.run_non_llm_loop(db), name="kanban-non-llm")
# shutdown:
llm_task.cancel(); non_llm_task.cancel()
await asyncio.gather(llm_task, non_llm_task, return_exceptions=True)
```

### Chat-active signal

`Loop A` calls `workers.tool_queue.is_chat_active()` — the existing in-memory counter maintained by `begin_chat_turn()` / `end_chat_turn()` in the chat and code agents. No separate DB table is needed: the Kanban worker runs in the same process as the agents, so the in-memory signal is directly accessible.

---

## 7. Huey → Kanban Migration Plan

Huey is reduced to a minimal supporting role. `run_tool_job` as a generic dispatcher is removed; the 16 short-job task types migrate to Kanban handlers. Two long-running jobs stay in Huey.

### Huey after migration — exactly 3 concerns

1. **`kanban_tick`** — periodic task every 5 s that does nothing but trigger one tick of each Kanban loop. Huey owns the consumer lifecycle; the Kanban loops run as asyncio tasks inside the same process.
2. **Cron-scheduled triggers** — daily research sweeps, scheduled scrapes, etc. These cron tasks insert rows into `task_list`; they do not execute work themselves.
3. **Genuinely long-running non-LLM background jobs** that don't fit either Kanban loop's polling cadence: `pathfinder_extract`, `harvest_run`.

`heartbeat` is removed — `kanban_tick` provides the equivalent liveness signal.

### Task migration table

| task_type | Current Home | New Home | Handler (file) | What it does | Flag |
|---|---|---|---|---|---|
| `research_planner` | Huey `run_tool_job` | **Kanban-LLM** | `tools/research/research_planner.py` | Calls `model_call("research_planner")` to generate hypotheses, sub-topics, and search queries; enqueues `research_agent` as dependent job | |
| `research_agent` | Huey `run_tool_job` | **Kanban-LLM** | `tools/research/agent.py` | Executes all planner queries, fetches corpus material, synthesises a section-by-section paper via repeated `model_call()` | |
| `research_review` | Huey `run_tool_job` | **Kanban-LLM** | `tools/research/agent.py` | Second-pass quality pass: reviewer model reads full paper, emits revision notes, rewrites flagged sections | |
| `research_op` | Huey `run_tool_job` | **Kanban-LLM** | `tools/research/operations.py` | Post-build dispatcher for user-invoked paper operations; routes to ASYNC_OPS / SYNC_OPS registry | ⚠ See uncertainty #1 |
| `summarise_page` | Huey `run_tool_job` | **Kanban-LLM** | `tools/enrichment/summariser.py` | Calls `model_call("scrape_summariser")` to produce a 4-8 sentence page summary; writes embedding to Chroma | |
| `graph_extract` | Huey `run_tool_job` | **Kanban-LLM** *(proposed)* | `tools/graph_extract.py` | Runs after chat turns to extract entity relationships from turn text and merge into FalkorDB | ⚠ See uncertainty #2 |
| `extract_relationships` | Huey `run_tool_job` | **Kanban-LLM** | `tools/enrichment/relationships_extractor.py` | Calls `model_call()` to extract entity relationships from page text and merge into FalkorDB | |
| `discover_agent_run` | Huey `run_tool_job` | **Kanban-LLM** | `tools/enrichment/discover_agent.py` | Searches URLs via SearXNG, calls `model_call()` for query generation and URL classification | |
| `insight_produce` | Huey `run_tool_job` | **Kanban-LLM** | `tools/insight/agent.py` | Gathers graph/RAG/scrape material and synthesises an 800-1500 word briefing via `model_call()` | |
| `pa_topic_research` | Huey `run_tool_job` | **Kanban-LLM** | `tools/pa/background.py` | Merges RAG hits and web search results, calls `model_call("pa_topic_research")` to produce a 2-4 sentence pocket brief | |
| `simulation_run` | Huey `run_tool_job` | **Kanban-LLM** | `tools/simulation/agent.py` | Runs multi-agent dialogue round-robin using `model_call()` per turn, then generates a debrief via a second model call | |
| `daily_digest` | Huey `run_tool_job` | **Kanban-LLM** | `tools/digest/agent.py` | Clusters last 24h scrapes by domain, calls `model_call()` per cluster to summarise and write digest markdown + embeddings | ⚠ Reclassified — was non-LLM in initial plan |
| `graph_resolve_entities` | Huey `run_tool_job` | **Kanban-LLM** | `tools/graph_maintenance/agent.py` | Embeds entity node names, clusters similar pairs, calls `model_call("graph_alias_judge")` to determine canonical merges | ⚠ Reclassified — was non-LLM in initial plan |
| `scrape_page` | Huey `run_tool_job` | **Kanban-non-LLM** | `tools/enrichment/scraper.py` | Fetches a URL via HTTP/Playwright, chunks content into Chroma `discovery` collection, enqueues `extract_relationships` and `summarise_page` as follow-ups; no model calls in this handler | |
| `corpus_maintenance` | Huey `run_tool_job` | **Kanban-non-LLM** | `tools/corpus_maintenance/agent.py` | Re-enqueues stale scrape targets still cited in recent messages; detects near-duplicate pages via Jaccard/k-shingle similarity | |
| `graph_maintenance` | Huey `run_tool_job` | **Kanban-non-LLM** | `tools/graph_maintenance/agent.py` | Decays edge weights by factor, prunes orphan nodes, synthesises CO_OCCURS_WITH edges from shared chunk co-appearances | |
| `seed_feedback` | Huey `run_tool_job` | **Kanban-non-LLM** | `tools/seed_feedback/agent.py` | Computes graph sparsity, domain quality, and weak-RAG signals; enqueues SearXNG search queries for the discover agent | |
| `pathfinder_extract` | Huey `run_tool_job` | **Huey-long** | `tools/enrichment/pathfinder.py` | Fetches an approved suggestion URL, walks same-host `<a href>` links (depth 1), inserts each as a `scrape_target` row; pure HTTP, no model calls | |
| `harvest_run` | Huey `run_tool_job` | **Huey-long** | `tools/harvest/runner.py` | Orchestrates multi-URL crawl; calls `model_call()` for summarisation when harvest policy permits | |

### Classification uncertainties

**#1 — `research_op`: mixed model usage inside a dispatcher**

Routes to a registry of named ops (`ASYNC_OPS`, `SYNC_OPS`). Some ops (`citation_audit`) are cheap and synchronous; others (`chat_with_paper`) call `model_call()`. Blanket Loop A assignment is safe. Splitting into two task types is possible but adds complexity for a small op count — keep under Kanban-LLM for now.

**#2 — `graph_extract`: prompt classifies as LLM-bound but implementation may not call the model**

`_handle_graph_extract` uses a "shared extraction schema" and no `model_call()` was observed. If confirmed non-LLM it belongs under Loop B. Read `tools/graph_extract.py` before porting. Misclassifying under Loop B risks model contention with chat if a model call is present.

**#3 — `daily_digest` and `graph_resolve_entities`: reclassified from non-LLM**

Both confirmed to call `model_call()`. The initial migration plan listed both under Loop B. Running either concurrently with chat would cause model contention — the serialisation under Loop A matters more with a local model than with an external API.

---

## 8. Model Invocation Inventory

All LLM calls now route through `shared/model_client.py`. No module constructs its own HTTP client for LLM calls. Model URL resolution is centralised in `LlamaCppBackend(url_resolver=get_model_url)`.

### ModelClient abstraction (`shared/model_client.py`)

| Class/Function | Role |
|---|---|
| `LlamaCppBackend` | Async and sync calls to any llama.cpp-compatible server. `url_resolver: Callable[[str], str \| None]` maps model name → container URL (supports multiple containers). Exposes `complete()` (async), `complete_sync()` (sync httpx), `stream_sync()` (context manager yielding SSE response). |
| `OpenRouterBackend` | Async calls to OpenRouter. Lazily fetches `/v1/models` and caches a free-tier allowlist for 24 h. Rejects non-free models before any HTTP call. Falls back to hardcoded list on fetch failure. |
| `ModelClient` | Routes by model prefix: `local:<model_id>` → `LlamaCppBackend`, `openrouter:<model_id>` → `OpenRouterBackend`. |
| `build_model_client()` | Factory: reads `OPENROUTER_API_KEY` env var, passes `get_model_url` as the URL resolver. Lazy-imports `infra.config` to avoid circular init. |
| `CompletionResult` | Dataclass: `text, model_used, tokens_in, tokens_out, finish_reason, error`. |

### Call sites (post-migration)

| File | Function | How it calls ModelClient |
|---|---|---|
| `shared/models.py` | `_raw_model_call()` | `LlamaCppBackend(url_resolver=lambda _: url).complete_sync()` — URL already resolved by caller via `acquire_role` |
| `shared/models.py` | `model_call()` / `tool_call()` / `fast_call()` | Delegates to `_raw_model_call` |
| `workers/streaming.py` | `stream_model_response()` | `mc.stream_sync(model="local:<model>")` — SSE parsed in-place |
| `workers/user_agents/agent.py` | `_call_model()` | `mc.complete_sync(model="local:<model_key>")` |
| `workers/user_agents/agent.py` | `_call_model_streaming()` | `mc.stream_sync(model="local:<model_key>")` |
| `workers/user_agents/types/base.py` | `call_model()` | `mc.complete_sync(model="local:<model_key>")` |
| `tools/planner.py` | `generate_plan()` | `mc.complete(model="local:<model_id>")` — async, URL resolved via `acquire_role` |
| `tools/integrations/api_registry.py` | `_write_usage_prompt()` | `mc.complete_sync(model="local:<model_id>")` — URL resolved via `acquire_role` |

### Model selection hierarchy (unchanged)

1. **`shared/models.py` path (most tool jobs):** `model_call(function_name)` → `get_function_config` reads `config.json` for the role → `acquire_role(role)` picks a live model ID from the pool → `_raw_model_call` creates a single-URL `LlamaCppBackend`.
2. **`workers/user_agents` path:** model key from agent's NocoDB config row → `build_model_client()` → resolver calls `get_model_url(key)`.
3. **`tools/planner.py` / `tools/integrations/api_registry.py`:** `acquire_role()` yields `(_, model_id)` → `build_model_client()` → resolver maps the model ID to its container URL.

---

## 9. Settings System

NocoDB-backed runtime overrides for per-agent defaults and all `config.json` feature flags. `config.json` remains the source of truth for defaults; the settings table stores only explicit overrides.

### Backend (`infra/settings.py`)

Three logical row types in the `settings` table, keyed by `agent`:

| `agent` value | Purpose |
|---|---|
| `__system__` | System-wide fallback: `fallback_model`, any key shared across agents |
| `__config__` | config.json section overrides: `{section: {key: value}}` nested JSON |
| `<agent_name>` | Per-agent overrides: `model`, `max_tokens_per_task`, `max_tasks_per_hour`, `max_daily_tokens` |

Lookup precedence for `get_agent_setting(agent, key)`: agent row → `__system__` row → `None` (caller falls back to `config.json`).

`get_feature_with_override(section, key)` checks `__config__` overrides before reading `config.json`.

In-process dict cache with `threading.Lock`; invalidated on every write.

### API (`app/routers/settings.py`, prefix `/settings`)

| Endpoint | Role |
|---|---|
| `GET /settings` | All agent rows + system row |
| `GET/PATCH /settings/agent/{agent}` | Single agent settings |
| `GET/PATCH /settings/system` | `__system__` row |
| `GET /settings/config` | All config.json sections with `_defaults`, `_overrides`, `_merged` |
| `PATCH /settings/config/{section}` | Write one or more keys into a section's override (deep-merged) |
| `DELETE /settings/config/{section}/{key}` | Revert a single override to config.json default |

### Frontend (`frontend/src/features/home/dashboard/SettingsPanel.tsx`)

Collapsible panel in the dashboard aside. Four tabs:

| Tab | File | Role |
|---|---|---|
| Toggles | `settings/Toggles.tsx` | 23 on/off switches for all `enabled` flags across feature sections; auto-saves on click |
| Models | `settings/ModelEditor.tsx` | 11 accordion sections; per-function role/temperature/max_tokens/max_input_chars/frequency_penalty editors |
| Scheduling | `settings/Scheduling.tsx` | ~30 numeric/text fields for intervals, cron, caps, and system config; saves on Enter or ✓ |
| Agents | inline in `SettingsPanel.tsx` | System `fallback_model` + per-agent model/token/rate fields for 6 named agents |

API client: `frontend/src/api/home/settings.ts` — `getSettings`, `patchAgentSettings`, `patchSystemSettings`, `getConfigSettings`, `patchConfigSection`, `deleteConfigOverride`.

---

## 6. Directly-Invokable Tools (No Queue Required)

These three capabilities have synchronous core implementations that agent code can call directly — no Huey, no dispatcher, no tool queue.

| Capability | File | Function signature | Constraints |
|---|---|---|---|
| Summariser | `infra/ai_flows.py` | `summarise_file(path: str, content: str) -> tuple[str, int]` | None. Blocking HTTP call to model API. Returns `(summary_text, token_count)`. |
| Scraper | `tools/scraper/pathfinder.py` | `PathfinderScraper(timeout=60).scrape(url: str) -> dict` | None. Blocking httpx + optional Playwright fallback. Returns `{url, final_url, text, links, domain, canonical, status, error}`. |
| Web search | `tools/search/orchestrator.py` | `run_web_search(query: str, org_id: int, intent_dict=None, history=None, extraction_function_name="search_extraction") -> tuple[str, list[dict], str]` | Requires a valid positive `org_id` (used for vector DB writes). Under `SEARCH_POLICY_CONTEXTUAL` may return early with `confidence="deferred"` if the hard time cap is exceeded — callers must handle that case. Returns `(context_markdown, sources_list, confidence_level)`. |

Each is also wrapped by an async `execute()` registered to the tool dispatcher, which is the production path through the queue. The wrappers are not in the call path when the underlying function is imported directly.