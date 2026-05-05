1
# Project Agent & Gitea Integration Audit

_Read-only analysis. Date: 2026-05-05._

---

## 1. Function & Class Map

### `infra/gitea_client.py`
| Symbol | Type | Purpose |
|---|---|---|
| `GiteaClient` | class | REST client wrapping Gitea API with PAT auth |
| `.list_repos()` | method | List repos for user/org |
| `.get_repo()` | method | Fetch single repo metadata |
| `.create_repo()` | method | Create new repo on Gitea |
| `.get_file()` | method | Read file content at path |
| `.put_file()` | method | Write/update file (single commit) |
| `.delete_file()` | method | Delete file via API |
| `.get_tree()` | method | Recursive tree fetch |
| `.get_commits()` | method | Commit history for a ref |
| `.list_branches()` | method | Branch listing |
| `.create_webhook()` | method | Register webhook on repo |

### `app/routers/gitea.py` (759 lines)
| Route | Method | Purpose |
|---|---|---|
| `/gitea/connection` | GET/PUT/DELETE | Manage Gitea server connection config |
| `/gitea/repos` | GET | List available repos |
| `/gitea/orgs` | GET | List orgs |
| `/projects/import-from-gitea` | POST | Pull zip, parse files, create project |
| `/projects/{id}/create-gitea-repo` | POST | Create remote repo from local project |
| `/projects/{id}/push-to-gitea` | POST | Push project files to Gitea |
| `/projects/{id}/gitea/pull/...` | GET/POST | Pull changes from Gitea into project |
| `/projects/{id}/gitea/status` | GET | Sync state metadata |

### `workers/code/agent.py` — `CodeAgent` (567 lines)
| Symbol | Purpose |
|---|---|
| `CodeAgent` | Orchestrates code workspace changes via streaming LLM |
| `.run(mode)` | Entry point; modes: `plan`, `execute`, `apply`, `review`, `explain`, `decide`, `scaffold`, `refine` |
| `.apply_file_fences()` | Parses `` ```file path=... mode=... `` blocks from stream, writes to FS |
| `.apply_tool_directives()` | Parses `` ```tool name=fs_read/fs_write/fs_delete `` blocks; multi-turn loop |
| `_tool_loop()` | Runs up to 3 hops of tool→result→model cycles |
| `_emit_workspace_event()` | Fires SSE workspace change events |

### `workers/code/fs_parser.py` — `FileFenceParser` (190 lines)
| Symbol | Purpose |
|---|---|
| `FileFenceParser` | Streaming regex parser for file fence blocks |
| `.parse(chunk)` | Stateful parse of streaming text; yields `FileFenceResult` |
| `.apply(result)` | Applies `replace` / `append` / `patch` (unified diff) / `delete` to project FS |

### `workers/code/fs_tools.py`
| Symbol | Purpose |
|---|---|
| `FSToolParser` | Parses `` ```tool `` directives from LLM output |
| `apply_tool_directives()` | Executes `fs_list`, `fs_read`, `fs_write`, `fs_delete` and returns results |

### `app/routers/code_launch.py`
| Symbol | Purpose |
|---|---|
| `start_code_job()` | Creates `CodeAgent`, runs in background, returns `job_id` |
| `stream_job_events_response()` | SSE stream of job events to client |

### `infra/ai_flows.py` + `app/routers/projects_ai.py`
Single-shot LLM calls (not agentic — one-turn, no tool loop):

| Flow | Route | Purpose |
|---|---|---|
| `review_diff()` | `POST /{id}/review` | Code review on a diff |
| `summarise_file()` | `POST /{id}/fs/file/summary` | File summary |
| `regenerate_readme()` | `POST /{id}/readme/regenerate` | README maintenance |
| `update_faq()` | `POST /{id}/faq/append` | FAQ updates |
| `classify_paste()` | `POST /{id}/smart-paste` | Smart paste routing |
| `generate_playbook()` | `POST /{id}/playbooks/generate` | Migration playbook |
| `regenerate_from_spec()` | `POST /{id}/scaffold-from-spec/regenerate` | Spec-driven code gen |

### User Agent Framework (`workers/user_agents/`)
| Symbol | Purpose |
|---|---|
| `Agent` (agent.py) | Generic agent: loads config from DB, builds prompts, streams, supports RAG + memory |
| `GeneratorAgent` | Wraps `Agent` for structured JSON output with Pydantic validation |

---

## 2. How a Repo Is Registered & Managed

**Registration flow (`PUT /gitea/connection`):**
1. User supplies Gitea base URL + PAT.
2. Client verified via `GiteaClient.list_repos()`.
3. Connection config stored (DB or settings).

**Import from Gitea (`POST /projects/import-from-gitea`):**
1. Target repo selected from `/gitea/repos`.
2. Gitea zip archive downloaded and extracted.
3. Files parsed and inserted into project workspace (versioned FS).
4. Project record created with `gitea_repo` metadata.

**Push to Gitea (`POST /projects/{id}/push-to-gitea`):**
1. Project files serialised.
2. Each file PUT via `GiteaClient.put_file()` (creates or updates with single commit per file).
3. Sync state written to project metadata.

**Pull from Gitea (`POST /projects/{id}/gitea/pull/...`):**
1. Remote tree fetched via `GiteaClient.get_tree()`.
2. Diff computed against local project snapshot.
3. Changed files applied to project FS; versions created.

**Sync state** tracked per-project: last push SHA, last pull SHA, dirty flag. Exposed at `/gitea/status`.

---

## 3. How Agentic Code Changes Are Invoked

**Entry point:** `POST /projects/{id}/code` (via `code_launch.py`) or inline via project chat.

**Execution path:**
```
HTTP request → start_code_job() → CodeAgent.run(mode)
    → streaming LLM call (Anthropic SDK)
    → FileFenceParser parses ``` file ``` blocks from stream
        → apply(): write/patch/delete files in project FS
    → FSToolParser parses ``` tool ``` blocks
        → multi-turn loop (up to 3 hops):
            tool directive → execute → inject result → re-call LLM
    → workspace change events emitted via SSE
    → client streams events from /stream/{job_id}
```

**Supported modes:** `plan`, `execute`, `apply`, `review`, `explain`, `decide`, `scaffold`, `refine`.

**File fence format the LLM must emit:**
````
```file path="src/foo.py" mode="replace" summary="..."
<content>
```
````
Modes: `replace`, `append`, `patch` (unified diff), `delete`.

**Tool directive format:**
````
```tool name="fs_read" path="src/foo.py"
```
````

---

## 4. What Works vs. What's Broken / Stubbed

### Working
- `GiteaClient` — full CRUD, tree, commits, branches, webhook stub.
- Import from Gitea — zip download → project creation pipeline.
- Push to Gitea — file-by-file PUT; sync state tracking.
- `CodeAgent` — streaming execution with file fence apply and tool loop.
- `FileFenceParser` — `replace`, `append`, `delete` modes.
- `patch` mode — unified diff apply (depends on `patch` lib; worth verifying in tests).
- Single-shot AI flows — all seven flows in `projects_ai.py`.
- User agent framework — generic + generator agents, scheduling, RAG.

### Broken / Stubbed / Suspect
| Item | Location | Issue |
|---|---|---|
| Webhook registration | `GiteaClient.create_webhook()` | Stub only — no handler endpoint exists in this repo to receive Gitea push events |
| Pull conflict resolution | `gitea.py` pull routes | No merge/conflict strategy documented; likely overwrites silently |
| `patch` mode validation | `fs_parser.py` | Unified diff apply can fail silently if context lines don't match; error handling unclear |
| Tool loop hop limit | `agent.py _tool_loop()` | Hard-coded 3 hops; no backpressure or partial-success handling if loop exits early |
| Multi-file atomic apply | `fs_parser.py` | Files applied incrementally during stream; a mid-stream failure leaves project in partial state |
| `projects_analysis.router` | `main.py` | Registered in router list but router file not found during audit — may be missing or empty |

---

## 5. PO / Architect Logic

**None found.**

There is no `ProductOwnerAgent`, `ArchitectAgent`, or equivalent class in the codebase. No role-based agent orchestration exists.

What exists instead:

| Approximation | Where | Notes |
|---|---|---|
| `decide` mode | `CodeAgent.run("decide")` | LLM prompted to make a single architectural decision; no structured output or follow-through |
| `plan` mode | `CodeAgent.run("plan")` | Produces a written plan; does not auto-execute it |
| `scaffold` mode | `CodeAgent.run("scaffold")` | Generates skeleton files from a spec; one-shot, no review loop |
| `review` mode | `CodeAgent.run("review")` | Code review pass; emits comments, no auto-apply |
| `regenerate_from_spec()` | `projects_ai.py` | Single-shot spec→code, no iteration |

There is **no agent that owns a backlog, breaks features into tasks, assigns work, or drives an iterative build loop**. The current architecture is a human-in-the-loop model: a human triggers a mode, the agent executes one pass, the human reviews the output.