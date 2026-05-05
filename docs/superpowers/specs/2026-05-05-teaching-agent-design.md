# Teaching Agent — F.1 Design

_Date: 2026-05-05. Status: approved design, pending implementation plan._

---

## 1. Two Modes — Lesson and Discussion

Teaching operates in two modes within the same session window. The user switches between them explicitly; they are not inferred.

**Lesson mode** is a prepared, source-grounded delivery. Before teaching anything, the agent runs a planned search — the same deep, multi-query search pipeline used in chat's planned search mode — to build a research base for the topic. It synthesises that into a structured lesson: real sources, accurate detail, citations where claims need grounding. Lesson mode is what makes the agent better than Wikipedia, not worse: it pulls from current sources, contextualises for the learner's level, and teaches the mechanism rather than summarising the article. The search runs once per lesson topic and is stored so subsequent sessions don't re-fetch the same ground.

**Discussion mode** is conversational and runs within the same window after a lesson, or as a standalone Q&A. It does not auto-invoke web search. The agent draws on the lesson content already prepared, its own knowledge, and the learner model. When it doesn't know something or isn't confident, it says so plainly — "I'm not certain about that detail; we'd need to search to get it right" — and stops there. It does not speculate or fill gaps with plausible-sounding content. The user can then toggle search on manually (planned or basic, same controls as the chat window) to fetch what's needed. No auto-invocation; the user drives it.

**Memory and knowledge:** Both modes use the full knowledge infrastructure available to chat — the same RAG lookup, corpus, and knowledge graph are all in play. Teaching is not isolated from the rest of the system. Prior conversations, stored context, and project knowledge all inform what the agent knows and teaches from. The learner model and curriculum (sections 4 and 5) are additional layers on top, specific to the teaching context. The agent does not behave as if it is starting from zero; it starts from everything it already knows.

**Why no auto-search in discussion:** Discussion should feel like talking to a knowledgeable professor, not triggering a research pipeline mid-conversation. Latency, context interruption, and the false confidence that a search result produces are all costs. The right model is: the professor teaches from preparation, acknowledges the edges of their knowledge honestly, and the student decides whether to pause for a source.

**Both modes in the same window:** Switching from lesson to discussion doesn't open a new chat. The session context is continuous. The lesson content, verified concepts, and curriculum state are all available in discussion. A learner can say "okay, let's just talk through this" mid-lesson and continue in discussion mode, then say "give me the next lesson segment" to resume lesson mode.

---

## 2. The Teaching Loop

**The chosen model: Socratic progression with artifact output and a learner model.**

The loop is not chat-style Q&A and not a document generator. It is a structured session that moves through four phases in order:

**Assess → Teach → Verify → Consolidate**

- **Assess**: Before teaching anything, the agent asks 2–3 diagnostic questions to locate where the learner is. It does not explain until it knows what it's explaining from. If a learner model already exists for this topic, assessment is shortened to a single calibration question.
- **Teach**: The agent teaches like a professor, not a search engine. Formatting tools — bullet points, diagrams, code blocks, Mermaid — are fine and often helpful; the problem is not format, it is depth. A concept is not taught when it has been named or summarised. It is taught when the learner understands: what it is, why it exists, how it works mechanically, when to use it, what breaks when you misuse it, and how it connects to what they already know. For "Attention in LLMs" this means covering why dot-product similarity, what Q/K/V represent and where they come from, what softmax does to the scores geometrically, why the √d_k scaling exists, and what "weighted sum of values" produces concretely — not a five-bullet summary that a Wikipedia skim would beat. The agent goes beyond the what into the why and how. It uses analogies, worked examples, and diagrams where they help. It does not consider a concept covered until the mechanism is in place, not just the label.
- **Verify**: After each concept, the agent asks the learner to explain it back, apply it, or predict an outcome. Passive receipt is not counted as understanding. The agent judges the response and either corrects misconceptions or advances.
- **Consolidate**: At end of session, the agent produces a session summary (Markdown) and a set of spaced-repetition cards. It also updates the learner model.

The loop can be entered mid-session (user brings a partially understood topic) or from scratch. It terminates when the user ends the session or when mastery is verified for the session's declared scope.

**The agent is always aware of its own teaching state.** At any point in a session it knows: what has been covered so far, what the learner demonstrated understanding of, what gaps were exposed, and where the logical next concept is. It surfaces this explicitly — after each concept it tells the learner where they stand and what comes next, rather than waiting to be asked. At natural breakpoints it proposes a learning path forward: "You've got the mechanism for self-attention. The natural next step from here is multi-head attention — understanding why you run several attention operations in parallel — or we can go sideways into positional encoding, which you'll need before the full Transformer makes sense. What do you want?" This is the difference between a professor and a chatbot: the professor holds the map and helps you navigate it.

---

## 2. Failure Modes in Chat-Style Teaching

Chat fell short in three specific ways:

**Example 1 — Explanation-without-resistance.** A user asks "explain async/await in Python." The model delivers a 600-word monologue. The user reads it, says "makes sense," closes the tab. Three weeks later they can't write an `async def` from memory. There was no moment where understanding had to be demonstrated. The explanation felt complete because it was complete — but comprehension was never tested. Feeling-of-knowing is not knowing.

**Example 2 — No curriculum arc; concepts taught in dependency-inverted order.** A user asks questions in the order they occur to them: first "what's a generator," then "what's asyncio," then "why does the GIL matter." A teacher would gate asyncio behind generators and gate the GIL behind thread semantics. Chat has no notion of prerequisites — it answers each question in isolation, leaving the learner with a set of disconnected facts instead of a mental model with load-bearing structure.

**Example 3 — Shallow coverage masquerading as teaching.** A user asks "teach me about Attention in LLMs." The model produces a high-level summary — five points about what attention does, maybe a diagram of Q/K/V, a note that softmax normalises scores. Each sentence is technically correct. The learner now has vocabulary, not understanding. They cannot derive why softmax, cannot explain what a value vector represents, cannot predict what breaks if you remove the √d_k scaling, cannot explain why this mechanism outperformed RNNs for long sequences. The model defaulted to overview mode — the path of least resistance, worse than a Wikipedia read — because it was never told that covering the what is not the same as teaching. A professor would have started with the problem attention is solving, built the intuition for similarity scoring, derived the mechanism, then shown the formalism as notation for something already understood.

**Example 4 — Formalism before intuition.** A user asks how L2 regularization works. Chat opens with "L2 adds a penalty term λ‖w‖² to the loss function, which penalises large weights." Technically correct. The learner reads it, nods, moves on with no real understanding of why large weights are a problem or what "penalty" means in an optimization sense. A professor would start from the problem: "Imagine your model can memorize training data perfectly. What goes wrong? It becomes brittle — it fits noise, not signal. Now: what if we said the model has to achieve a good loss *and* stay humble — keep its weights small? You're now trading off fit against confidence. That tradeoff is regularization. λ‖w‖² is just the notation for that constraint." Intuition first; formalism as a compressed notation for something already understood.

**Example 5 — Conflict-avoidance on wrong answers.** A learner says "so backpropagation is basically just taking the derivative of the loss with respect to the inputs." This is wrong — it's the derivative with respect to the weights, not the inputs. Chat responds: "That's close! Backpropagation does involve derivatives, and it also uses the chain rule to..." The error is never named. The learner leaves with a misconception intact, now reinforced by the agent's implicit acceptance. A professor would say: "Not quite — and the distinction matters. You said inputs, but it's weights. The inputs are fixed for a given example; it's the weights we're adjusting. Let's look at why that matters for what gradient descent is actually doing."

**Example 6 — Scope explosion.** A user says "teach me machine learning." Chat produces a listicle of every ML subcategory, or asks what they want to focus on and waits passively. Neither is what a professor does. A professor immediately narrows: "That's a semester. Let's start somewhere concrete — supervised learning, specifically classification. What do you already know about statistics and probability? That'll tell me where to begin." Without scope negotiation at the start, depth is impossible — you can't teach ML, you can only gesture at it.

**Example 7 — No transfer testing.** A learner works through attention and the agent verifies they can explain the mechanism. Session ends. Next session: a different topic. The learner's attention knowledge was tested in exactly one context — transformers. A professor would at some point say: "You understand attention as it applies to text sequences. Now — Vision Transformers split images into patches and run attention over those patches. Does that make sense given what you know? What would the Q/K/V vectors represent in that setting?" Transfer is the real test of understanding. If the learner can only explain a concept in the context they learned it, they've memorised, not understood.

**Example 8 — No connection-making across sessions.** The learner learns softmax in the context of classification, then later learns it again in the context of attention scores, then again in policy gradients. Chat treats each instance as fresh. A professor who knows the learner's history would say: "This is the same softmax you learned for classification — turn a vector of scores into a probability distribution. Here it's doing the same thing to attention scores. The mechanism is identical; only the scores being normalised are different." Without those bridges, every concept is an island. The learner accumulates facts but not a mental model.

**Example 9 — Session amnesia that degrades the second session.** A user spends 45 minutes learning database transactions. Three weeks later they come back to ask about isolation levels. The agent has no memory: it re-explains dirty reads from scratch, the user gets impatient and skips ahead, the agent doesn't push back, the subtlety of repeatable-read vs serializable gets elided. The second session is worse than if they'd had a tutor who remembered the first. State-free teaching punishes returning learners.

---

## 3. Output Artifacts

Each Teaching session produces exactly two artifacts:

**Session summary (Markdown):**
Saved to a configurable path (default: `~/Teaching/<topic>/sessions/YYYY-MM-DD.md`). Contains:
- Topic and declared scope
- Concepts taught, in order
- The worked example used for each concept
- Misconceptions surfaced and corrections made
- Where the learner ended up (what's mastered, what's deferred)

**Spaced-repetition cards (plain-text Anki import format):**
One file per session, appended to `~/Teaching/<topic>/cards.txt`. Format: `front\tback\ttags`. Each card covers one verifiable unit — a definition, a prediction, a "what happens when" scenario. Cards are not comprehension questions; they are retrieval cues for the specific mechanism the learner was taught.

No PDF, no slide deck, no elaborate formatting. Markdown + plain-text Anki is the entire artifact surface. These two formats are durable, portable, and don't require any tooling beyond a text editor.

---

## 4. The Curriculum

Every topic has a curriculum — a structured, ordered plan of what to learn, in what sequence, and why. The curriculum is created at the start of a topic and persists across sessions. It is not a rigid syllabus; it is a living document that the agent updates as the learner progresses or as their interests redirect.

**What the curriculum contains:**
- A root goal: what the learner is trying to understand or be able to do
- A sequence of modules, each covering one coherent concept cluster
- For each module: its prerequisites, its learning objectives, and its estimated depth (introductory / working / deep)
- A marker of current position: which module is active, which are complete, which are deferred
- Open questions and threads: things the learner raised that weren't pursued yet

**How it's used during a session:**
The agent opens each session by surfacing the curriculum state: "Last time we finished self-attention. Today's natural next step is multi-head attention. We've also got positional encoding on the list — you asked about it last session but we deferred it. Want to continue in order, or pick something?" The learner can redirect. If they do, the agent updates the curriculum and continues.

**How it's amended — suggestions and requests:**
Both the agent and the learner can amend the curriculum at any time. The agent makes proactive suggestions when it detects a gap, a more logical ordering, or a topic the learner raised that isn't yet on the plan: "You mentioned transformers in that question — that's not on the curriculum yet, but it depends on everything we're building toward. Want to add it as the endpoint?" The learner can make requests at any point: "skip that, I already know it," "go deeper on this before we move on," "add something about how this is used in practice." Both are treated as curriculum edits, logged with a reason, so the history of why the plan looks the way it does is recoverable. The curriculum always reflects the actual learning arc, not the original plan.

**Where it lives:** NocoDB, one row per topic per user in a `learner_curricula` table. The curriculum itself is stored as structured JSON (modules array with status, objectives, prerequisites). The agent reads and writes it at session boundaries.

---

## 5. State Across Sessions — The Learner Model

**Yes. Without a learner model, sessions two through N are worse than session one.**

**Where it lives:** NocoDB, consistent with the project's existing state pattern. One table: `learner_concepts`.

### Schema

| Column | Type | Notes |
|---|---|---|
| `topic` | varchar | e.g. "Python async", "SQL transactions" |
| `concept` | varchar | Granular unit, e.g. "event loop", "dirty read" |
| `mastery` | enum | `exposed` / `practiced` / `verified` |
| `last_seen` | datetime | For spaced-repetition scheduling |
| `misconceptions` | text | JSON array of errors the learner made |
| `preferred_style` | varchar | `examples_first` / `theory_first` / `null` (inferred across sessions) |
| `session_count` | int | How many times this concept has appeared |

### What each field does

- `mastery` distinguishes "I explained it to them" (`exposed`) from "they applied it correctly" (`practiced`) from "they explained it back under pressure" (`verified`). Only `verified` counts as learned.
- `misconceptions` are stored so the agent can probe them directly in future sessions — not just reteach, but specifically target what broke before.
- `preferred_style` is inferred from behavior: does the learner engage more after examples or after definitions? After two sessions this becomes a prior. It is never asked directly — it is observed.
- `last_seen` + `session_count` feed a simple SM-2-style scheduling signal that surfaces "this concept is due for review" at session start.

### Rules

- The learner model is read-only to the user (they can view it, not edit it).
- The agent updates mastery only after the Verify phase confirms an outcome — not speculatively after explanation.
- The model is scoped to a single user. No shared or aggregate learner state across users.
