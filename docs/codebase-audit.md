# Codebase Audit

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