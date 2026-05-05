# Research Reliability & Output Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make research tasks run reliably end-to-end by removing a brittle cross-system dependency and preventing silent retry loops, while improving output quality through richer per-doc-type section guidance.

**Architecture:** Two focused changes. First: strip `workers.tool_queue` imports from `tools/research/agent.py` — these were no-ops under kanban but coupled research to the Huey job system and could cause silent failures. Replace with plain `_log` calls. Second: wrap `workers/task_handlers/research.py`'s `_run()` in a top-level try/except so any unhandled exception returns a failure dict instead of propagating to kanban and triggering 3 automatic full-pipeline retries. Also enrich DOC_TYPES with per-type body/opener/closer guidance so section writers produce genuinely type-specific content.

**Tech Stack:** Python 3.11, FastAPI, kanban task queue backed by NocoDB, `shared.models.model_call` for LLM calls.

---

## File Map

| File | Change |
|------|--------|
| `tools/research/agent.py` | Remove 3 `tool_queue` import blocks; replace `report_progress`/`is_job_cancelled`/`bind_job_id`/`current_job_id` with `_log` calls; simplify `_write_section` to 2 attempts; enrich DOC_TYPES with `opener_role`/`closer_role`/`body_guidance`; update `_section_prompt` and `_build_paper` to use them |
| `workers/task_handlers/research.py` | Wrap `_run()` body in top-level `try/except` |

---

### Task 1: Wrap `_run()` in research.py handler

**Files:**
- Modify: `workers/task_handlers/research.py`

The current `_run()` has no top-level guard. Any exception from `create_research_plan()`, the doc_type stash, or the NocoDB client propagates to kanban's `_execute()`, which retries the task up to 3 times — running the full 30-minute pipeline again each time.

- [ ] **Step 1: Add top-level try/except to `_run()`**

Replace the entire `_run` function body so it looks like this (the imports stay inside to avoid circular imports at module load):

```python
def _run(payload: dict) -> dict:
    topic = (payload.get("topic") or "").strip()
    org_id = int(payload.get("org_id") or 0)
    doc_type_hint = (payload.get("doc_type") or "").strip() or None

    if not topic:
        return {"status": "failed", "error": "input_payload.topic is required"}

    try:
        import json as _json
        from infra.nocodb_client import NocodbClient
        from tools.research.research_planner import create_research_plan, run_research_planner_job
        from tools.research.agent import run_research_agent
        from tools.research.output import build_output_payload

        plan_result = create_research_plan(topic=topic, org_id=org_id, defer_run=True)
        if plan_result.get("status") not in ("deferred", "pending"):
            return {"status": "failed", "error": f"plan creation failed: {plan_result.get('error', plan_result.get('status'))}"}
        plan_id = int(plan_result.get("plan_id") or 0)
        if not plan_id:
            return {"status": "failed", "error": "plan creation returned no plan_id"}

        client = NocodbClient()

        if doc_type_hint:
            try:
                row = client._get("research_plans", params={"where": f"(Id,eq,{plan_id})", "limit": 1})
                existing = (row.get("list") or [{}])[0]
                schema = _json.loads(existing.get("schema") or "{}")
                schema["_doc_type"] = doc_type_hint
                client._patch("research_plans", plan_id, {"schema": _json.dumps(schema)})
            except Exception as e:
                _log.warning("doc_type stash failed  plan_id=%d  err=%s", plan_id, e)
                # Non-fatal: research continues with default doc_type

        planner_result = run_research_planner_job(plan_id, queue_agent=False)
        if planner_result.get("status") in ("failed", "not_found", "disabled"):
            return {"status": "failed", "plan_id": plan_id, "error": planner_result.get("error", planner_result["status"])}

        agent_result = run_research_agent(plan_id)
        if agent_result.get("status") != "completed":
            return {"status": "failed", "plan_id": plan_id, "error": agent_result.get("error", agent_result.get("status"))}

        row = client._get("research_plans", params={"where": f"(Id,eq,{plan_id})", "limit": 1})
        paper = ((row.get("list") or [{}])[0]).get("paper_content") or ""

        sources: list[dict] = agent_result.get("sources") or []
        final_doc_type: str = agent_result.get("doc_type") or doc_type_hint or "research_report"

        return build_output_payload(plan_id, final_doc_type, paper, sources)

    except Exception as exc:
        import logging as _logging
        _logging.getLogger("research.handler").error(
            "research handler uncaught  topic=%r  err=%s", topic[:80], exc, exc_info=True
        )
        return {"status": "failed", "error": str(exc)[:400], "topic": topic}
```

- [ ] **Step 2: Verify the file compiles**

```bash
cd /Users/michaelturner/PycharmProjects/JeffGPT-Harness && python -c "from workers.task_handlers.research import handle; print('OK')"
```

Expected: `OK`

---

### Task 2: Remove `tool_queue` imports from `_fetch_corpus`

**Files:**
- Modify: `tools/research/agent.py` (lines ~315–392)

`_fetch_corpus` imports `report_progress`, `is_job_cancelled`, `JobCancelled`, `current_job_id`, `bind_job_id` from `workers.tool_queue`. Under kanban, `current_job_id()` returns `None` and everything is a no-op — but the import couples research to the Huey system and pulls in 73KB of code.

- [ ] **Step 1: Replace the tool_queue block in `_fetch_corpus`**

Find this block (starts around line 315):
```python
    from workers.tool_queue import (
        report_progress, is_job_cancelled, JobCancelled,
        current_job_id, bind_job_id,
    )
    import concurrent.futures
    import threading
    # Capture the parent contextvar — ThreadPool workers do NOT inherit it
    # by default, so without bind_job_id() the report_progress and
    # is_job_cancelled calls inside _one would be no-ops.
    parent_job_id = current_job_id()
    max_workers = min(4, max(1, len(queries)))
    completed_count = [0]
    completed_lock = threading.Lock()

    def _one(idx_q):
        idx, q = idx_q
        with bind_job_id(parent_job_id):
            if is_job_cancelled():
                return idx, q, None
            report_progress(f"search start [{idx}/{len(queries)}]: {q[:60]}")
            res = _safe_call(...)
            with completed_lock:
                completed_count[0] += 1
                done_n = completed_count[0]
            report_progress(
                f"search done [{done_n}/{len(queries)}]: {q[:60]}"
            )
            return idx, q, res
```

And the completion loop that checks `is_job_cancelled()`.

Replace it with (keeping all the actual search logic, just removing tool_queue coupling):

```python
    import concurrent.futures
    import threading
    max_workers = min(4, max(1, len(queries)))
    completed_count = [0]
    completed_lock = threading.Lock()

    def _one(idx_q):
        idx, q = idx_q
        _log.info("corpus search start [%d/%d]: %s", idx, len(queries), q[:60])
        res = _safe_call(
            lambda q=q: run_web_search(
                q, org_id=org_id, intent_dict=intent,
                extraction_function_name=extraction_function_name,
            ),
            timeout_s,
            f"search[{idx}/{len(queries)}]:{q[:40]}",
        )
        with completed_lock:
            completed_count[0] += 1
            done_n = completed_count[0]
        _log.info("corpus search done [%d/%d]: %s", done_n, len(queries), q[:60])
        return idx, q, res
```

And in the futures loop, remove the `is_job_cancelled()` check entirely:

```python
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="research-search",
    )
    try:
        futures = [pool.submit(_one, item) for item in enumerate(queries, start=1)]
        for fut in concurrent.futures.as_completed(futures):
            try:
                idx, q, res = fut.result()
            except concurrent.futures.CancelledError:
                continue
            if not res:
                continue
            try:
                ctx, src, conf = res
            except (TypeError, ValueError):
                continue
            if ctx and conf not in ("failed", "none", "deferred"):
                blocks.append(f"--- Query: {q} (confidence={conf}) ---\n{ctx}")
            for s in (src or []):
                if not isinstance(s, dict):
                    continue
                url = (s.get("url") or "").strip()
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                sources.append(s)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
```

- [ ] **Step 2: Verify the file compiles**

```bash
cd /Users/michaelturner/PycharmProjects/JeffGPT-Harness && python -c "from tools.research.agent import _fetch_corpus; print('OK')"
```

Expected: `OK`

---

### Task 3: Remove `tool_queue` imports from `_build_paper`

**Files:**
- Modify: `tools/research/agent.py` (lines ~784–870)

`_build_paper` has two `tool_queue` import blocks: one for `report_progress as _rp` (line ~784) and one inside the body section parallel block (lines ~809–814) for `_report`, `_cancelled`, `_Cancelled`, `_current_job_id_get`, `_bind_job_id`.

- [ ] **Step 1: Replace the `_rp` import block**

Find (around line 784):
```python
    from workers.tool_queue import report_progress as _rp
    revision_notes = revision_notes or {}
```

Replace with:
```python
    revision_notes = revision_notes or {}
```

And replace every `_rp(...)` call in `_build_paper` with `_log.info(...)`. There are ~8 calls — each like `_rp("fetching corpus: ...")`. Change each to `_log.info("build_paper: fetching corpus: %d queries", len(queries))` (adjust message to use % formatting per structured log conventions).

- [ ] **Step 2: Replace the body section tool_queue import block**

Find (around line 809):
```python
    from workers.tool_queue import (
        report_progress as _report,
        is_job_cancelled as _cancelled,
        JobCancelled as _Cancelled,
        current_job_id as _current_job_id_get,
        bind_job_id as _bind_job_id,
    )
    import concurrent.futures
    import threading
    body_pieces_ordered: list[str | None] = [None] * len(sub_topics or [])
    body_failed: list[str] = []

    section_done = [0]
    section_lock = threading.Lock()
    # Capture parent contextvar; thread-pool workers don't inherit it.
    parent_job_id = _current_job_id_get()

    def _write_body(idx_sub):
        idx, sub = idx_sub
        with _bind_job_id(parent_job_id):
            if _cancelled():
                return idx, sub, None
            _report(f"section start [{idx + 1}/{len(sub_topics or [])}]: {sub[:80]}")
            sec = _write_section(...)
            with section_lock:
                section_done[0] += 1
                done_n = section_done[0]
            _report(
                f"section done [{done_n}/{len(sub_topics or [])}]: {sub[:80]}"
                f" {'OK' if sec else 'EMPTY'}"
            )
            return idx, sub, sec
```

Replace with:
```python
    import concurrent.futures
    import threading
    body_pieces_ordered: list[str | None] = [None] * len(sub_topics or [])
    body_failed: list[str] = []

    section_done = [0]
    section_lock = threading.Lock()

    def _write_body(idx_sub):
        idx, sub = idx_sub
        _log.info("build_paper: section start [%d/%d]: %s", idx + 1, len(sub_topics or []), sub[:80])
        sec = _write_section(
            topic=topic, doc_type=doc_type, section_title=sub,
            section_role=spec.get("body_guidance") or f"Cover '{sub}' as a substantive body section of the document.",
            corpus=corpus, hypotheses=hypotheses, target_words=700,
            revision_note=revision_notes.get(sub),
        )
        with section_lock:
            section_done[0] += 1
            done_n = section_done[0]
        _log.info(
            "build_paper: section done [%d/%d]: %s %s",
            done_n, len(sub_topics or []), sub[:80], "OK" if sec else "EMPTY",
        )
        return idx, sub, sec
```

And in the futures loop, remove the `if _cancelled():` / `raise _Cancelled(...)` block:

```python
    if sub_topics:
        max_workers = min(3, len(sub_topics))
        _log.info("build_paper: writing %d body sections (parallel x%d)", len(sub_topics), max_workers)
        pool_s = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="research-section",
        )
        try:
            futures = [pool_s.submit(_write_body, item) for item in enumerate(sub_topics)]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    idx, sub, sec = fut.result()
                except concurrent.futures.CancelledError:
                    continue
                if sec:
                    body_pieces_ordered[idx] = sec
                else:
                    body_failed.append(sub)
        finally:
            pool_s.shutdown(wait=False, cancel_futures=True)
```

- [ ] **Step 3: Verify the file compiles**

```bash
cd /Users/michaelturner/PycharmProjects/JeffGPT-Harness && python -c "from tools.research.agent import _build_paper; print('OK')"
```

Expected: `OK`

---

### Task 4: Remove `tool_queue` imports from `run_research_agent`

**Files:**
- Modify: `tools/research/agent.py` (lines ~967–999)

`run_research_agent` imports `report_progress`, `is_job_cancelled`, `JobCancelled` from `tool_queue` and calls them around the plan loading and corpus fetch steps.

- [ ] **Step 1: Remove the tool_queue import and replace calls**

Find (around line 967):
```python
    from workers.tool_queue import report_progress, is_job_cancelled, JobCancelled

    client = NocodbClient()
    try:
        report_progress(f"loading plan {plan_id}")
        plan_row = ...
        ...
        report_progress(
            f"plan loaded: {len(queries)} queries, {len(sub_topics)} sub-topics, doc_type={doc_type}"
        )
        if is_job_cancelled():
            raise JobCancelled("cancelled before search")

        _patch_or_log(client, plan_id, {"status": "searching"}, "searching")
        report_progress(f"searching: {len(queries)} queries → web search + extract")
```

Replace with:
```python
    client = NocodbClient()
    try:
        _log.info("research_agent: loading plan %d", plan_id)
        plan_row = ...
        ...
        _log.info(
            "research_agent: plan loaded  plan_id=%d  queries=%d  sub_topics=%d  doc_type=%s",
            plan_id, len(queries), len(sub_topics), doc_type,
        )

        _patch_or_log(client, plan_id, {"status": "searching"}, "searching")
        _log.info("research_agent: searching  plan_id=%d  queries=%d", plan_id, len(queries))
```

- [ ] **Step 2: Verify the file compiles**

```bash
cd /Users/michaelturner/PycharmProjects/JeffGPT-Harness && python -c "from tools.research.agent import run_research_agent; print('OK')"
```

Expected: `OK`

---

### Task 5: Simplify `_write_section` from 3 attempts to 2

**Files:**
- Modify: `tools/research/agent.py` (lines ~531–595)

The 3rd attempt uses 1/3 target words and an 8KB corpus — almost always produces content too thin to be useful. Two attempts (full context, then 2/3 context) is sufficient.

- [ ] **Step 1: Change `attempts=3` default to `attempts=2` and remove the 3rd branch**

```python
def _write_section(*, topic: str, doc_type: str, section_title: str, section_role: str,
                   corpus: str, hypotheses: list[str], target_words: int,
                   revision_note: str | None = None,
                   attempts: int = 2) -> str | None:
    """Write one section with shrinkage on first failure.

    Attempt 1: full target_words, corpus[:30000], no max_tokens cap.
    Attempt 2: ~2/3 target, corpus[:16000], max_tokens=3000.
    """
    timeout_s = _research_timeout("section_timeout_s", DEFAULT_SECTION_TIMEOUT_S)
    last_err = "unknown"
    for n in range(1, attempts + 1):
        if n == 1:
            this_target = target_words
            this_corpus = corpus[:30000]
            this_max_tokens: int | None = None
        else:
            this_target = max(400, (target_words * 2) // 3)
            this_corpus = corpus[:16000]
            this_max_tokens = 3000
            _log.warning(
                "section %r retry %d/%d — shrinking (target=%d words, corpus=%d chars, max_tokens=%d)",
                section_title[:40], n, attempts, this_target, len(this_corpus), this_max_tokens,
            )
        prompt = _section_prompt(
            topic=topic, doc_type=doc_type, section_title=section_title,
            section_role=section_role, corpus=this_corpus, hypotheses=hypotheses,
            target_words=this_target, revision_note=revision_note,
        )
        kwargs: dict = {"temperature": 0.3}
        if this_max_tokens:
            kwargs["max_tokens"] = this_max_tokens
        res = _safe_call(
            lambda: model_call("research_section_writer", prompt, **kwargs),
            timeout_s,
            f"section[{n}/{attempts}]:{section_title[:30]}",
        )
        if not res:
            last_err = "timeout_or_error"
            continue
        try:
            text, _ = res
        except (TypeError, ValueError):
            last_err = "bad_tuple"
            continue
        text = (text or "").strip()
        if text:
            return text
        last_err = "empty"
    _log.warning("section %r FAILED after %d attempts  last_err=%s",
                 section_title[:40], attempts, last_err)
    return None
```

- [ ] **Step 2: Verify**

```bash
cd /Users/michaelturner/PycharmProjects/JeffGPT-Harness && python -c "from tools.research.agent import _write_section; print('OK')"
```

Expected: `OK`

---

### Task 6: Enrich DOC_TYPES with type-specific section guidance

**Files:**
- Modify: `tools/research/agent.py` (lines 48–193)

Each doc type gains three fields:
- `opener_role`: what the opener section specifically needs to accomplish
- `closer_role`: what the closer section needs to land  
- `body_guidance`: how body sections should be structured for this type

These replace the generic `"Cover '{sub}' as a substantive body section"` role used by `_build_paper`.

- [ ] **Step 1: Replace DOC_TYPES with the enriched version**

```python
DOC_TYPES = {
    "research_report": {
        "opener": "Background",
        "closer": "Discussion and Conclusions",
        "tone": "academic, neutral, evidence-led prose. Cite sources for every concrete claim.",
        "summary_role": "Summarise the question investigated, the strongest findings, and the conclusions reached.",
        "opener_role": "Establish the research question, scope, methodology, and key prior work. Frame what is known and what is being investigated.",
        "closer_role": "Synthesise findings across body sections, evaluate the evidence, state conclusions directly, and note any limitations or gaps.",
        "body_guidance": "Lead with the evidence: quote data, dates, and specific findings from sources. Draw explicit comparisons across studies. Flag contradictions. Avoid narrative padding.",
    },
    "business_plan": {
        "opener": "Market Opportunity",
        "closer": "Roadmap and Operating Plan",
        "tone": "decisive operator voice — concrete, numbers-first, written for a founder or investor audience.",
        "summary_role": "State the venture proposition, market size, customer, business model, and the funding/effort ask.",
        "opener_role": "Quantify the market opportunity with TAM/SAM/SOM figures. Name the customer segment and their pain. State why now.",
        "closer_role": "Lay out the 12-18 month roadmap with concrete milestones, team requirements, and capital needs. Make the ask explicit.",
        "body_guidance": "Lead each section with a number or quantified claim. Present evidence in the investor's frame: who has money, who is spending, what has been proven. Close each section with the business implication.",
    },
    "market_analysis": {
        "opener": "Market Overview",
        "closer": "Outlook and Implications",
        "tone": "analyst voice — quantified, comparative, written for a strategy or investment reader.",
        "summary_role": "Headline the market size, growth, structure, and the most important shifts underway.",
        "opener_role": "State the market definition, size (total and addressable), and growth rate. Identify the key players and market structure.",
        "closer_role": "Synthesise the macro outlook: where the market is going, what will drive or suppress growth, and the two or three most consequential shifts underway.",
        "body_guidance": "Quantify every claim. Compare across geographies, segments, or time periods where possible. Use analyst framing: market share, growth rates, structural drivers.",
    },
    "technical_brief": {
        "opener": "Context and Constraints",
        "closer": "Recommendation and Tradeoffs",
        "tone": "engineering voice — precise, comparative, opinionated where the evidence permits.",
        "summary_role": "State the problem, the candidate approaches, and the recommended approach with the load-bearing reason.",
        "opener_role": "State the problem clearly, the constraints that shape the solution space, and the evaluation criteria being used.",
        "closer_role": "State the recommended approach explicitly, explain why it wins on the load-bearing criteria, and name the key tradeoffs the reader must accept.",
        "body_guidance": "Structure around comparisons: option A vs option B on each criterion. Be specific about performance, compatibility, and operational cost. Don't hedge where the evidence is clear.",
    },
    "comparison": {
        "opener": "Evaluation Criteria",
        "closer": "Recommendation",
        "tone": "fair-minded comparative reviewer — every option treated symmetrically before any recommendation.",
        "summary_role": "Name the options compared, the criteria used, and the leading recommendation.",
        "opener_role": "Define what is being compared, the evaluation criteria and their relative weights, and why these specific options were selected.",
        "closer_role": "Name the winner for each use case, not a single universal pick. Make the selection logic explicit.",
        "body_guidance": "Treat each option symmetrically. For every dimension, cover all options before moving on. Never recommend inside body sections — that belongs in the closer.",
    },
    "how_to": {
        "opener": "Prerequisites and Setup",
        "closer": "Common Pitfalls and Troubleshooting",
        "tone": "practical instructor voice — second-person, concrete, sequential.",
        "summary_role": "State what the reader will accomplish, the prerequisites, and the rough effort/time required.",
        "opener_role": "State what the reader will be able to do after following this guide, list all prerequisites, and note the estimated time and difficulty.",
        "closer_role": "Cover the most common failure modes, error messages the reader might encounter, and what to try first when something goes wrong.",
        "body_guidance": "Write in second-person imperative. Each step should be atomic and verifiable. Include expected output or result after each step so the reader can confirm success.",
    },
    "policy_brief": {
        "opener": "Issue and Stakeholders",
        "closer": "Recommended Policy Direction",
        "tone": "policy advisor voice — neutral framing, balanced presentation of options.",
        "summary_role": "State the policy question, who is affected, the options considered, and the recommendation.",
        "opener_role": "Define the policy problem, who is affected, and what is at stake. Name the stakeholders and their interests.",
        "closer_role": "State the recommended policy direction, the implementation pathway, and the tradeoffs accepted. Be direct — hedge only where the evidence is genuinely unclear.",
        "body_guidance": "Present multiple perspectives before synthesising. Attribute positions to specific stakeholders or bodies. Avoid jargon; this must be readable by a non-specialist policymaker.",
    },
    "feasibility_study": {
        "opener": "Problem Statement",
        "closer": "Recommendation and Next Steps",
        "tone": "evaluator voice — rigorous, hedged where evidence is thin, decisive where it is not.",
        "summary_role": "State what was assessed, the verdict (feasible / conditionally / not), and the load-bearing reasons.",
        "opener_role": "State precisely what is being assessed, the scope boundaries, the assessment methodology, and the key questions that will determine feasibility.",
        "closer_role": "State the verdict clearly: feasible, conditionally feasible, or not feasible. List the conditions or blockers. Recommend next steps proportional to the verdict.",
        "body_guidance": "Structure around risk dimensions: technical, financial, operational, regulatory. For each, state the assumption, the evidence, and the confidence level. Flag where evidence is thin.",
    },
    "deep_dive": {
        "opener": "Background and Stakes",
        "closer": "Implications and Open Questions",
        "tone": "long-form explanatory voice — patient, layered, narrative-driven, but rigorously sourced.",
        "summary_role": "Frame why this matters, the core finding, and what the reader will understand by the end.",
        "opener_role": "Frame why this topic matters and what is at stake. Establish the historical or contextual background the reader needs before the analysis begins.",
        "closer_role": "Surface the implications that flow from the analysis. Name the open questions that remain unresolved and why they matter.",
        "body_guidance": "Build the argument layer by layer. Each section should deepen the reader's understanding, not just add more facts. Draw connections across sections. Narrative drive matters here.",
    },
    "white_paper": {
        "opener": "Executive Position",
        "closer": "Implications and Call to Action",
        "tone": "authoritative industry voice — vendor-neutral but persuasive, written for a decision-making readership.",
        "summary_role": "State the position taken, the central evidence, and the call to action.",
        "opener_role": "Establish the industry position being argued, the problem it addresses, and the authoritative framing that makes this paper credible.",
        "closer_role": "Restate the position, summarise the evidence, and make a concrete call to action for the reader.",
        "body_guidance": "Write with authority. Every section should advance the central argument. Use data as primary evidence; vendor claims without third-party corroboration should be noted as such.",
    },
    "literature_review": {
        "opener": "Scope and Methodology",
        "closer": "Synthesis and Open Questions",
        "tone": "academic synthesis voice — careful attribution to prior work, neutral, theme-driven not chronological.",
        "summary_role": "State the question, the body of literature reviewed, and the dominant findings and gaps.",
        "opener_role": "Define the research question, the scope of the literature (date range, field, inclusion/exclusion criteria), and the search methodology.",
        "closer_role": "Synthesise what the literature collectively establishes, where the major debates remain open, and what the most significant gaps are for future research.",
        "body_guidance": "Attribute every claim to specific authors or works. Group sources thematically, not chronologically. Map areas of agreement and contradiction. Don't just describe papers — synthesise them.",
    },
    "competitive_analysis": {
        "opener": "Competitive Landscape",
        "closer": "Strategic Implications",
        "tone": "competitive intelligence voice — fact-led, comparative, written for a strategy or product reader.",
        "summary_role": "Identify the competitors, the dimensions compared, and the strategic implication.",
        "opener_role": "Define the competitive landscape: who the players are, how the market is segmented, and the key dimensions on which they compete.",
        "closer_role": "Distil the strategic implications: who is winning and why, what the competitive dynamics mean for the subject, and where the strategic leverage points are.",
        "body_guidance": "Lead with observable facts: pricing, feature parity, market position, announced roadmap. Attribution matters — note sources carefully. Distinguish stated strategy from evidenced behaviour.",
    },
    "due_diligence": {
        "opener": "Investment Thesis and Scope",
        "closer": "Findings and Recommendation",
        "tone": "investor diligence voice — skeptical, evidence-grading, surface every red flag.",
        "summary_role": "State the target, the diligence scope, the top findings, and the go/no-go recommendation with conditions.",
        "opener_role": "State the investment thesis being tested, the scope of the diligence, and the key questions that would change the verdict.",
        "closer_role": "State the go/no-go recommendation with conditions. Be explicit about red flags and the evidence behind each. Do not bury deal-breakers.",
        "body_guidance": "Be skeptical by default. For every positive claim, surface the countervailing evidence. Quantify risk where possible. Flag gaps in the evidence base explicitly — absence of evidence is itself a finding.",
    },
    "product_spec": {
        "opener": "Problem and Goals",
        "closer": "Open Questions and Sequencing",
        "tone": "PRD voice — declarative, structured, written for engineers and designers to act on.",
        "summary_role": "State the problem, target user, success metrics, and the proposed solution at a glance.",
        "opener_role": "State the problem being solved for a specific user, the success metrics, and what is explicitly out of scope.",
        "closer_role": "List the open questions that engineering or design need to resolve before building starts, and the sequencing rationale.",
        "body_guidance": "Write for engineers and designers, not executives. Be declarative: 'the system will do X' not 'we should consider X'. Surface constraints and edge cases in the body, not just the happy path.",
    },
    "architecture_decision": {
        "opener": "Context and Forces",
        "closer": "Decision and Consequences",
        "tone": "ADR voice — terse, decision-focused, every claim defensible.",
        "summary_role": "State the decision, the alternatives considered, and the load-bearing reason.",
        "opener_role": "State the decision context: what changed that requires a decision, the system constraints, and the decision criteria.",
        "closer_role": "State the decision, the alternatives that were rejected and why, and the consequences the team must plan for.",
        "body_guidance": "Be terse and exact. Each alternative should be evaluated on the same criteria. Quantify where possible. The body exists to justify the closer — don't pad.",
    },
    "case_study": {
        "opener": "Situation and Background",
        "closer": "Outcomes and Lessons",
        "tone": "narrative analytical voice — storyline-driven but rigorously sourced.",
        "summary_role": "State the subject, the situation studied, and the key lessons drawn.",
        "opener_role": "Introduce the subject, the context, and what makes this case instructive. Establish the situation before intervention or decision.",
        "closer_role": "State the outcomes (quantified where possible) and extract the lessons that generalise beyond this specific case.",
        "body_guidance": "Tell the story with specificity: name dates, decisions, and actors. Attribution matters. The narrative within each section should build to a point.",
    },
    "swot_analysis": {
        "opener": "Subject and Frame",
        "closer": "Strategic Implications",
        "tone": "strategist voice — concise, comparative, structured around the SWOT quadrants.",
        "summary_role": "State the subject and the most consequential strength, weakness, opportunity, and threat.",
        "opener_role": "Define the subject, the strategic frame (time horizon, competitive context), and how the SWOT quadrants were assessed.",
        "closer_role": "Cross-map the quadrants: which strengths can exploit which opportunities, which weaknesses compound which threats. State the strategic priority.",
        "body_guidance": "Each body section covers one SWOT quadrant. Items within a quadrant should be ranked by significance. Avoid generic observations — every item should be specific and evidenced.",
    },
    "industry_report": {
        "opener": "Industry Definition and Scope",
        "closer": "Outlook and Watch List",
        "tone": "industry analyst voice — quantified, structural, written for a sector reader.",
        "summary_role": "State the industry, its size, its structure, and the most consequential dynamics.",
        "opener_role": "Define the industry scope, its current size and structure, and the dominant dynamics shaping it.",
        "closer_role": "State the 12-24 month outlook, the metrics to watch, and the two or three companies or trends most worth monitoring.",
        "body_guidance": "Quantify everything. Segment the industry by geography, vertical, or value chain as appropriate. Use analyst conventions: market share tables, growth rate comparisons, structural analysis.",
    },
    "risk_assessment": {
        "opener": "Scope and Methodology",
        "closer": "Mitigation Recommendations",
        "tone": "risk officer voice — neutral, structured by likelihood × impact, hedged where evidence is thin.",
        "summary_role": "State the assessment scope, the top risks, and the recommended mitigations.",
        "opener_role": "Define the assessment scope, the risk framework used (likelihood × impact), and the risk appetite of the subject organisation.",
        "closer_role": "Prioritise the top risks by residual exposure after mitigations. State the recommended mitigations with owners and timelines.",
        "body_guidance": "Structure each risk with: description, likelihood (high/medium/low with rationale), impact (quantified where possible), current controls, and residual risk. Don't hedge — make the rating explicit.",
    },
    "investment_memo": {
        "opener": "Thesis",
        "closer": "Decision and Conditions",
        "tone": "IC memo voice — opinionated, thesis-led, every claim backed.",
        "summary_role": "State the investment thesis, the key risks, and the recommended action.",
        "opener_role": "State the investment thesis in one paragraph: what you are buying, why it is cheap or differentiated, and what must be true for the thesis to work.",
        "closer_role": "State the recommended action (buy/hold/sell/pass) with size and timing, the key conditions, and the single most important thing to monitor.",
        "body_guidance": "Lead with the bear case, then the bull case. Every claim needs a number or a source. The memo exists to be stress-tested — surface the weak points rather than hiding them.",
    },
    "trend_report": {
        "opener": "Trend Landscape",
        "closer": "Implications and Watch Items",
        "tone": "futurist analyst voice — pattern-led, hedged on timing, evidenced by signals.",
        "summary_role": "State the trends covered, their drivers, and the implications for the reader.",
        "opener_role": "Identify the 3-5 macro trends being tracked, the signal sources used, and the timeframe of relevance.",
        "closer_role": "State the implications: who wins, who loses, and what actions the reader should consider in the next 6-18 months.",
        "body_guidance": "Ground each trend in specific signals: announcements, data, early movers, adjacent shifts. Distinguish signal from noise. Note timing uncertainty explicitly.",
    },
    "retrospective": {
        "opener": "What Happened",
        "closer": "Lessons and Forward Actions",
        "tone": "post-mortem voice — blameless, factual, lesson-extracting.",
        "summary_role": "State what was undertaken, the outcome, and the most important lessons.",
        "opener_role": "Describe what was undertaken, the intended outcome, the timeline, and the actual outcome.",
        "closer_role": "Extract the lessons that generalise: what to repeat, what to change, and what the team now knows that it didn't before.",
        "body_guidance": "Be factual and blameless. Name what happened — including failures — without assigning blame. Each section should answer: what did we intend, what actually happened, what drove the difference?",
    },
    "forecast": {
        "opener": "Forecast Question and Method",
        "closer": "Scenarios and Confidence",
        "tone": "forecaster voice — probabilistic, scenario-based, calibrated uncertainty.",
        "summary_role": "State the forecast question, the scenarios, the most likely outcome, and the confidence.",
        "opener_role": "State the forecast question precisely, the time horizon, the methodology, and the key variables that drive the forecast.",
        "closer_role": "Present the scenarios with probability estimates. State the base case, the conditions under which the bull/bear cases materialise, and the confidence level.",
        "body_guidance": "Be probabilistic throughout. For each driver, state the direction and magnitude of effect. Use ranges, not point estimates. Name the assumptions explicitly so the reader can stress-test them.",
    },
    "explainer": {
        "opener": "Why This Matters",
        "closer": "What to Take Away",
        "tone": "patient explainer voice — accessible, no jargon without unpacking, readable end to end.",
        "summary_role": "State what the reader will understand by the end and why it matters.",
        "opener_role": "Hook the reader by stating why this matters and what they will understand by the end. Establish the baseline assumption about what the reader already knows.",
        "closer_role": "Consolidate the core insight into a paragraph a reader could repeat to someone else. Avoid introducing new concepts at this stage.",
        "body_guidance": "Unpack one concept at a time. Define jargon before using it. Use analogies from domains the reader likely knows. Each section should end with the reader having a mental model they didn't have before.",
    },
}
```

- [ ] **Step 2: Verify all 26 types are present and compile**

```bash
cd /Users/michaelturner/PycharmProjects/JeffGPT-Harness && python -c "
from tools.research.agent import DOC_TYPES
assert len(DOC_TYPES) == 26, f'Expected 26, got {len(DOC_TYPES)}'
required = {'opener_role', 'closer_role', 'body_guidance', 'opener', 'closer', 'tone', 'summary_role'}
for name, spec in DOC_TYPES.items():
    missing = required - set(spec.keys())
    assert not missing, f'{name} missing fields: {missing}'
print(f'OK: {len(DOC_TYPES)} doc types, all fields present')
"
```

Expected: `OK: 26 doc types, all fields present`

---

### Task 7: Wire `opener_role` and `closer_role` into `_build_paper` and `_section_prompt`

**Files:**
- Modify: `tools/research/agent.py`

`_section_prompt` already accepts `section_role` — it just needs to be passed the right value from `_build_paper`. The body sections now use `spec.get("body_guidance")` (already wired in Task 3). The opener and closer need the same treatment.

- [ ] **Step 1: Update the opener call in `_build_paper`**

Find:
```python
    opener = _write_section(
        topic=topic, doc_type=doc_type, section_title=spec["opener"],
        section_role=f"Establish the {spec['opener'].lower()} for the rest of the document.",
        corpus=corpus, hypotheses=hypotheses, target_words=500,
        revision_note=revision_notes.get(spec["opener"]),
    ) or ""
```

Replace with:
```python
    opener = _write_section(
        topic=topic, doc_type=doc_type, section_title=spec["opener"],
        section_role=spec.get("opener_role") or f"Establish the {spec['opener'].lower()} for the rest of the document.",
        corpus=corpus, hypotheses=hypotheses, target_words=500,
        revision_note=revision_notes.get(spec["opener"]),
    ) or ""
```

- [ ] **Step 2: Update the closer call in `_build_paper`**

Find:
```python
    closer = _write_section(
        topic=topic, doc_type=doc_type, section_title=spec["closer"],
        section_role=f"Synthesise the body into the {spec['closer'].lower()}.",
        corpus=closer_corpus, hypotheses=hypotheses, target_words=600,
        revision_note=revision_notes.get(spec["closer"]),
    ) or ""
```

Replace with:
```python
    closer = _write_section(
        topic=topic, doc_type=doc_type, section_title=spec["closer"],
        section_role=spec.get("closer_role") or f"Synthesise the body into the {spec['closer'].lower()}.",
        corpus=closer_corpus, hypotheses=hypotheses, target_words=600,
        revision_note=revision_notes.get(spec["closer"]),
    ) or ""
```

- [ ] **Step 3: Final compile check**

```bash
cd /Users/michaelturner/PycharmProjects/JeffGPT-Harness && python -c "
from tools.research.agent import run_research_agent, review_research_paper, DOC_TYPES
from workers.task_handlers.research import handle
from workers.task_handlers.research_op import handle as op_handle
from workers.task_handlers.research_review import handle as review_handle
from workers.task_handlers.research_revision import handle as revision_handle
print('All imports OK')
"
```

Expected: `All imports OK`

---

### Task 8: End-to-end wiring review

**Files:** Read-only audit

- [ ] **Step 1: Confirm no remaining `tool_queue` imports in research code**

```bash
grep -rn "from workers.tool_queue import\|workers\.tool_queue" \
  /Users/michaelturner/PycharmProjects/JeffGPT-Harness/tools/research/ \
  /Users/michaelturner/PycharmProjects/JeffGPT-Harness/workers/task_handlers/research*.py
```

Expected: no output (zero matches)

- [ ] **Step 2: Confirm research handler never raises**

```bash
grep -n "raise\|JobCancelled" \
  /Users/michaelturner/PycharmProjects/JeffGPT-Harness/workers/task_handlers/research.py
```

Expected: no output

- [ ] **Step 3: Confirm all kanban task types resolve to handlers**

```bash
cd /Users/michaelturner/PycharmProjects/JeffGPT-Harness && python -c "
from app.lifespan import _kanban_registrations  # or equivalent
" 2>&1 | head -5
```

If the above import path doesn't exist, check lifespan.py manually to confirm `research`, `research_op`, `research_review`, `research_revision`, `research_planner`, `research_agent` are all registered.

- [ ] **Step 4: Spot-check ops wiring**

```bash
cd /Users/michaelturner/PycharmProjects/JeffGPT-Harness && python -c "
from tools.research.operations import ASYNC_OPS, SYNC_OPS, run_research_op
expected = {'fact_check','expand_section','add_section','counter_arguments','add_fresh_sources','refresh_recency','reframe','resize','slide_deck','email_tldr','qa_pack','action_plan','citation_audit','chat_with_paper'}
actual = set(ASYNC_OPS) | set(SYNC_OPS)
missing = expected - actual
assert not missing, f'missing ops: {missing}'
print(f'ops OK: {sorted(actual)}')
"
```

Expected: `ops OK: [...]` with all ops listed
