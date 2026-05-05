# CLAUDE.md

Operating rules for this repository. Read top-to-bottom before any change. Rules higher in the file outrank rules lower in the file.

## 1. Hard Rules (Non-Negotiable)

- **Stop and ask before:** installing a new dependency, creating a new top-level folder, introducing a new pattern not already in the codebase, or making a change that touches more than 5 files.
- **Never** commit secrets, API keys, tokens, or `.env` contents. If you see one, stop and flag it.
- **Never** use `Any` from `typing` without a `# reason:` comment justifying it. Prefer concrete types, `TypeVar`, or `Protocol`. `# type: ignore` is forbidden without an inline justification.
- **Never** disable a lint rule, type check, or test to make code pass. Fix the cause.
- **Never** write code you cannot explain. If a pattern feels magical, replace it with the boring version.
- **No scope drift.** Do the task asked. Surface adjacent issues as a list at the end of the response — do not fix them unprompted.

## 2. Simplicity First

The simplest correct solution wins. Cleverness is a cost, not a feature.

**Test for every function:** Could a competent dev who has never seen this file understand it in 30 seconds? If no, simplify.

### Verbosity is a defect

- No restating the obvious in comments. `# increment counter` above `counter += 1` gets deleted on sight.
- No defensive code for conditions that cannot occur given the type system or validated inputs.
- No abstraction layers introduced "for future flexibility". Add them when the second use case actually arrives.
- No options/config flags on functions that have one caller.
- No wrapper functions that only rename a single call.

### Bad vs Good

```python
# BAD — verbose, defensive, over-abstracted
def get_user_display_name(user: User | None) -> str:
    if user is None:
        return ""
    if not user:  # duplicate guard
        return ""
    name = user.name
    if name and isinstance(name, str) and len(name) > 0:
        return name
    return ""

# GOOD — type system and Pydantic already guarantee the shape
def get_user_display_name(user: User) -> str:
    return user.name or ""
```

### Comment policy

Comments explain **why**, never **what**. Test: if deleting the comment loses information that isn't in the code, keep it. Otherwise delete it.

Acceptable comments:
- Non-obvious business rules (`# Tax rule changed July 2024 — see ATO ruling X`)
- Workarounds (`# httpx bug #1234 — remove when fixed`)
- Performance-critical decisions (`# Manual loop — measured 4x faster than list comprehension for >10k rows`)
- TODOs with brief context (`# TODO: split this file when it hits 200 lines`)

Unacceptable: anything that paraphrases the code on the next line.

## 3. Architecture

### One concern per file

A file does one thing. If you find yourself writing "and" in the file's purpose, split it. Names describe the thing, not the layer (`pricing.py`, not `pricing_helpers.py` or `pricing_utils.py`).

### Singletons at module scope

Clients, connections, and configured instances are instantiated **once** at module scope and imported. Never per-request, never inside functions that are called repeatedly, never inside loops.

```python
# GOOD
db = create_engine(connection_string)
client = anthropic.Anthropic()

# BAD
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()
```

### File size limits

- **200 lines** is the soft cap. At 200, stop and propose a split before continuing.
- **400 lines** is hard. Do not exceed without explicit approval.

### Async

- `asyncio.sleep()` only in async contexts — never `time.sleep()` inside a coroutine.
- Don't mix sync and async code. A sync function that calls `asyncio.run()` inside an async app will deadlock. Pick one model per layer and stay consistent.
- `asyncio.gather()` for independent concurrent calls. Sequential `await` inside a loop for N calls that could be parallel is an N+1 equivalent.
- Blocking I/O (file reads, subprocess) in an async context goes through `asyncio.to_thread()` or an executor — never inline.

### Backend / API

- Validate every external input with Pydantic at the boundary. Trust types only after validation.
- Authorisation checks live in the route handler or service layer, not middleware alone. Middleware proves *who*, the handler proves *whether they can*.
- Database access goes through the ORM. No f-strings or `.format()` with user input in SQL, ever. Parameterised queries only.
- Errors raised across the network boundary extend a known base exception class. Never leak internal tracebacks to clients.

## 4. Security (OWASP-aligned)

Treat every user input, URL parameter, header, and third-party response as hostile until validated.

### A01 — Broken Access Control
- Every read/write checks ownership, not just authentication. "Is this user logged in" ≠ "can this user touch this row".
- Default-deny. New endpoints require an explicit access check; absence of a check fails review.

### A02 — Cryptographic Failures
- No hand-rolled crypto. Use `secrets`, `hashlib`, or library-provided primitives (`passlib`, `python-jose`).
- Secrets come from environment variables or a secret store, never hardcoded, never from the client.
- TLS-only for anything leaving the process.

### A03 — Injection
- Parameterised queries via the ORM. Raw SQL with any interpolated value is forbidden.
- No dynamic code evaluation on untrusted input. No deserialisation of untrusted binary formats. No subprocess with `shell=True` and user-supplied values.
- No LLM-generated code executed without a sandboxed runtime.

### A04 — Insecure Design
- Rate-limit anything that costs money, sends email, or hits an external API.
- Idempotency keys on any state-changing operation that could be retried.

### A05 — Security Misconfiguration
- Production: debug mode off, stack traces never returned to clients, CORS allowlisted.
- Default to the most restrictive option; relax with justification.

### A07 — Identification & Auth Failures
- Session/auth handled by the chosen library. Never roll session logic by hand.
- Passwords: never logged, never returned in responses, never stored unhashed (`bcrypt` or `argon2`).

### A08 — Software & Data Integrity
- Pin dependency versions in `requirements.txt` or `pyproject.toml`. No unpinned security-sensitive packages.
- Verify webhook signatures before trusting payloads.

### A09 — Logging Failures
- Structured logging only (`structlog` or `logging` with a JSON formatter). No `print()` in committed code.
- Never log: passwords, tokens, full API keys, raw request bodies of auth endpoints.
- Always log: auth failures, authorisation denials, validation failures on sensitive endpoints.

### A10 — SSRF
- Outbound requests to user-supplied URLs go through an allowlist. Never `httpx.get(user_supplied_url)` directly.

### AI-specific — Prompt Injection
- Never interpolate raw user content directly into a system prompt. Treat user content as data, not instructions.
- Sanitise or wrap user content in explicit delimiters (`<user_input>...</user_input>`) before inserting into prompts.
- Never trust that an LLM output is safe to execute or render as HTML without validation.

## 5. Performance

- No N+1 queries. Batch with `IN` clauses. If you wrote a loop with a DB or network call inside, stop and rewrite.
- Cache expensive computation, not cheap computation. Caching `a + b` is noise.
- Streaming over buffering for anything that could be large — responses, file reads, LLM output.
- Measure before optimising. "I think this is faster" without a number is not a reason.

## 6. Definition of Done

A change is done when **all** of the following are true:

- [ ] `mypy` / `pyright` passes with no new suppressions.
- [ ] Linter (`ruff` or `flake8`) passes with no new ignores.
- [ ] Tests cover new logic (happy path + at least one failure mode).
- [ ] No new `Any` without justification, no new `# type: ignore`.
- [ ] No `print()`, `TODO`, or commented-out code in the diff.
- [ ] No secrets in the diff (grep for `sk_`, `Bearer `, `AKIA`, hardcoded passwords).
- [ ] File size limits respected.
- [ ] If a new dependency was added: it was approved.

## 7. When Asking Me Questions

- Ask one question at a time. Wait for the answer.
- Surface assumptions explicitly: "I'm assuming X — confirm or correct."
- Prefer multiple-choice questions over open-ended where possible.
- If a decision is reversible and low-cost, make it and note it. If irreversible or expensive, ask first.

## 8. Response Format

### Planning, calibrated to the task

Match planning depth to task complexity. Default to less, not more.

- **Trivial** (rename, typo, single-line edit, mechanical change, second-pass iteration on something just done): no plan, no spec, no TodoWrite. Just make the change.
- **Standard** (new function, multi-file change, non-obvious refactor): plan as **5 bullets or fewer** covering approach, files touched, and any tradeoff. Wait for confirmation before code.
- **Architectural / unfamiliar / learning** (new pattern, new dependency, design decision, anything I flag as a learning topic): full explanation of approach, alternatives considered, and *why this one*. Wait for confirmation.

If unsure which tier, ask in one line. Don't default to the heavier tier "to be safe" — the heavier tier is 2–4x more tokens and locks in early decisions.

### Suppress these by default

- **No implementation specs** unless I explicitly ask for one. Bullet plans only.
- **No TodoWrite for tasks under ~4 steps.** The list overhead exceeds the value.
- **No mid-implementation narration.** Don't tell me what you're about to do, just do it. I'll see the code.
- **No end-of-task recaps.** The diff is the recap. Do not list what was done, what files were created, or what I could do next.
- **No preamble restating my question.**
- **No closing offers** ("let me know if you want me to..."). If a follow-up is genuinely needed, state it as one line.

### When recap is appropriate

A short recap is acceptable only if:
- The change had a non-obvious side effect I should know about
- A follow-up step is genuinely required (e.g. "run the migration before testing")
- I explicitly asked for a summary

In those cases: one line, not a bulleted list.

### Code output

- Diffs over full files when editing existing code.
- Full files only when creating new ones.
- No nested code fences (they break my UI).
- Comments only where the rules in §2 permit them.

## 9. Code Smells to Avoid (Python)

Rules that protect against the patterns seniors flag as "rewrite this."

### Single file

- Functions operate at one level of abstraction. Don't mix DB calls, business logic, and I/O in the same function body.
- No defensive checks for states the type system and Pydantic already exclude. If `user: User`, don't guard `if user and user.id`.
- `try/except` is for handling expected failure, not control flow. Never swallow exceptions with a bare `except:` or `except Exception: pass`.
- No mutable default arguments. `def foo(items=[])` is a Python trap — use `def foo(items: list | None = None)` and assign inside.
- No `import *`. It hides dependencies, pollutes the namespace, and breaks static analysis.
- No magic numbers. Name them as module-level constants.
- Function names describe intent, not mechanism. `calculate_monthly_total`, not `process_data`. Avoid `process_*`, `handle_*`, `manage_*` as primary verbs.
- Use `dict.get(key)` or `.get(key, default)` when a key may be absent. `dict[key]` on uncertain input is a latent `KeyError`.

### Across files

- One error contract per layer. Pick a base exception class, raise it consistently — don't let `ValueError`, `RuntimeError`, and `HTTPException` all escape the same layer.
- Logs describe what the system experienced, not what the code is doing. `"user 123 payment declined"` not `"entering process_payment"`.
- Validate at the boundary (Pydantic at the API edge), trust types after. No re-validating the same data in three layers.
- Tests assert behaviour, not implementation. If swapping the implementation breaks the test without changing behaviour, the test is wrong.
- No circular imports disguised as function-level lazy imports (`from module import thing` inside a function to avoid a cycle). Fix the architecture instead.

### System-level

- The codebase has a conceptual centre. Core domain concepts appear as named classes/modules matching business language. If you can't grep the domain, the model is missing.
- Layer boundaries are real. Business rules live in one place. If a rule is enforced in three places, the boundary is decorative.
- Every feature has an explicit failure model. What happens on timeout, retry, concurrent edit, duplicate webhook. Happy path alone is not done.
- Repeated patterns get extracted. If five functions do "fetch, check ownership, mutate, return" with slight differences, that's one function with parameters.
- Code has testing seams. Pure functions over side-effectful ones. Inject `datetime.now`, HTTP clients, and DB sessions rather than importing globals.

### Process

- Commits describe intent, not mechanics. `"Fix race in invitation accept"` not `"Refactor user_service.py"`.
- One concern per commit. Features, refactors, and dep bumps are separate commits.
- Delete code that no longer earns its place. Unused imports, dead branches, deprecated helpers — remove them in the same change that obsoletes them.
- Update adjacent artefacts. New feature → matching test, type hints, updated doc. Don't leave the README describing old behaviour.
- The first generation is a draft. After the code works, re-read it and delete anything that isn't earning its place.

## 10. AI Code Smells (Python + LLM-specific)

These are the patterns that appear constantly in AI-generated or AI-adjacent Python code. They are subtle, they compound, and they are harder to spot in review than a `print()` statement.

### God functions

A single function that calls the LLM, parses the response, validates it, retries on failure, logs the result, and updates the DB. Each of those is a concern — split them. The LLM call is pure I/O; validation is a domain rule; retry is a policy; the DB write is a side effect.

### Fragile output parsing

```python
# BAD — breaks the moment the model changes its phrasing
lines = response.split("\n")
items = [l.lstrip("0123456789. ") for l in lines if l.strip()]

# GOOD — instruct the model to return structured output and validate it
class Items(BaseModel):
    items: list[str]

items = Items.model_validate_json(response)
```

Never parse LLM output with string splitting or regex on numbered lists. Instruct the model to return JSON, then validate with Pydantic. Always wrap `model_validate_json` in a `try/except ValidationError` — models hallucinate schema.

### Swallowed LLM errors

```python
# BAD — silent failure, caller can't distinguish "no result" from "API down"
try:
    result = client.messages.create(...)
except Exception:
    return None

# GOOD — let it propagate or convert to a typed error with context
```

Silent `None` returns from LLM calls mask rate limit errors, context-length errors, and network failures.

### Missing timeouts

```python
# BAD
response = httpx.get(url)

# GOOD
response = httpx.get(url, timeout=30.0)
```

Every outbound HTTP call — including to LLM APIs — needs an explicit timeout. The default is often "wait forever".

### Prompt injection via f-string

```python
# BAD — user controls the system prompt
system = f"You are a helpful assistant. The user's name is {user_input}."

# GOOD — user content is data, not instructions
system = "You are a helpful assistant."
user_message = f"<user_input>{escape(user_input)}</user_input>"
```

### Hardcoded model strings scattered across the codebase

```python
# BAD — three files each hardcode the model name separately
response = client.messages.create(model="claude-opus-4-7", ...)

# GOOD — one constant, one change to upgrade
DEFAULT_MODEL = "claude-opus-4-7"
```

### Assuming token budgets

Never assume a document fits in a context window. Measure token count before sending; chunk or summarise if over the limit. Treating the context window as infinite is a runtime error waiting to happen.

### Blocking LLM calls in async code

```python
# BAD — blocks the entire event loop while waiting for the API
async def handle_request():
    result = sync_llm_client.call(...)

# GOOD — use the async client
async def handle_request():
    result = await async_llm_client.call(...)
```

### Retry without backoff

```python
# BAD — hammers the API immediately on failure
for _ in range(3):
    try:
        return client.messages.create(...)
    except Exception:
        continue

# GOOD — exponential backoff + jitter via tenacity
@retry(
    wait=wait_exponential_jitter(),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(RateLimitError),
)
def call_llm(): ...
```

### Mutable global conversation history

```python
# BAD — shared across all users/requests in a server process
history: list[dict] = []

def chat(message: str) -> str:
    history.append({"role": "user", "content": message})
    ...
```

Conversation state is per-session. Global mutable state corrupts concurrent requests and leaks data between users.

### No logging of LLM usage

Token counts, latency, model version, and cost signals belong in structured logs on every LLM call. Without them, you have no visibility into why costs spike or where latency comes from.

### Treating LLM output as safe

LLM output is untrusted user-like input. Never pass it to dynamic code execution, shell commands, or template rendering without sanitisation. Never inject it raw into HTML without escaping.

### The underlying principle

Good code is the artefact left after thinking, not the transcript of thinking. Edit down to the conclusion. If the code reads like a sequence of cautious decisions, simplify until it reads like one confident decision.

---

## Project-Specific Rules

*Append per-project sections below. Keep them short — push general rules upward into the base.*

### Stack Manifest
<!-- e.g. Python 3.12, FastAPI 0.x, SQLAlchemy 2.x, Pydantic 2.x, Anthropic SDK -->

### Project Conventions
<!-- folder structure, naming, anything that diverges from the base -->

### Forbidden in This Project
<!-- e.g. "no synchronous DB calls in async routes", "no raw SQL" -->