# 02 — Prompting Opus 4.8: System-Prompt Engineering for a Literal Model

| | |
|---|---|
| **What transfers** | Prompt structure, snippet discipline, tool descriptions, narration/autonomy dials, effort routing, cache-safe layout — all of it is harness engineering and works on any sufficiently instruction-following model. |
| **What doesn't** | No prompt raises per-step correctness. A system prompt cannot add knowledge, reasoning depth, or calibration the weights lack. Prompting buys *adherence*, not intelligence. See [docs/00-philosophy.md](00-philosophy.md). |

This document covers how FABLE's shipped prompts (`prompts/*.md`) are written and why, so you can tune them without breaking the properties they encode. Every snippet shown here ships verbatim in a prompt file and is loaded through `fable.prompts.load()` — which resolves `{{slots}}` exactly once at load time, from static values only, because a byte-stable system prompt is what keeps the prompt cache warm (see [docs/05-memory-context.md](05-memory-context.md)).

---

## 1. Opus 4.8 deltas: what changed and what to delete from your old prompts

Opus 4.8 is a *literal* model. Prompting habits developed against earlier models — hedged instructions, motivational shouting, redundant scaffolding — are now dead weight or actively harmful. The deltas, each with the concrete edit it implies:

**1. Literal instruction following.** The model does what you say, including the parts you said accidentally. "Improve this file" produces improvements you did not want. State scope explicitly, both directions:

> Make only the changes the task requires. Do not refactor, reformat, rename, or "clean up" code you were not asked to change.

If your prompt says "be thorough," the model will be thorough about everything, including things you wanted skipped. Say *what* to be thorough about. (Anthropic, "Claude 4 prompt engineering best practices" — <https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/claude-4-best-practices>.)

**2. Favors reasoning over tools.** When a step is hard, Opus 4.8's default failure mode is to reason harder rather than to gather more evidence. The fix is not a prompt exhortation — it is raising `effort` (§2) and shipping the `investigate_before_answering` snippet (§6.3) so evidence-gathering is framed as a procedural requirement rather than a suggestion.

**3. Under-spawns subagents.** Given a delegation tool, Opus 4.8 tends to do everything serially itself. If you register `spawn_subagent_tool()`, include the calibration snippet:

> When a task splits into three or more independent parts that do not share working state, spawn subagents for those parts instead of doing everything serially yourself. Calibration: one agent with 3–10 tool calls for simple fact-finding; 2–4 agents for comparisons; 10 or more agents only for genuinely decomposable research. Below three independent parts, delegation overhead exceeds its value — do it yourself.

(Delegation heuristics from Anthropic, "How we built our multi-agent research system" — <https://www.anthropic.com/engineering/built-multi-agent-research-system>.)

**4. Delete anti-laziness language.** ALL-CAPS, "MUST", "CRITICAL", "YOU WILL BE PENALIZED" — these were workarounds for models that under-complied. On a literal model they distort priority: the model treats the shouted line as dominating everything else, including instructions that actually matter more. None of FABLE's shipped prompts contains them. If you find yourself reaching for caps, the real fix is usually a gate check (the harness enforces it) or an effort bump (the model can afford it).

**5. Delete narration scaffolding.** Old prompts carried "summarize your progress every 3 tool calls" because old models went silent for 40 turns. Opus 4.8 produces natural progress updates on its own; the scaffolding now produces *double* narration. Delete it and control narration with the dial in §7 instead.

---

## 2. Effort is the primary dial — prompt around it, not against it

`output_config={"effort": ...}` is the main lever for intelligence-per-dollar, and it changes how you prompt:

| Effort | Behavior | Prompt implications |
|---|---|---|
| `low` | Minimal exploration, terse output, strict scope adherence | Ideal for mechanical subagents. Instructions are followed narrowly — write them completely, nothing is inferred. |
| `medium` | Balanced | Research/judge default in FABLE (`DEFAULT_ROLES`). |
| `high` | API default | General work. |
| `xhigh` | Deep exploration, willing to run long tool chains | FABLE's default for orchestrator/executor roles. Recommended for coding and agentic work. |
| `max` | Ceiling | Escalation target only (`Router.escalate`), not a default. |

The rule: **when the model reasons shallowly, raise effort; do not prompt-hack.** A paragraph of "think really carefully step by step" is a worse and more expensive version of `effort="xhigh"`. Conversely, at `effort="low"` the model respects scope strictly — which is exactly what you want for a mechanical formatter subagent, and exactly what you do not want for open-ended debugging.

Effort is set per-role in `FableConfig.roles` (see [src/fable/config.py](../src/fable/config.py)); the prompts assume the policy in force and do not mention effort in their text. Costs and routing economics are in [docs/06-operations.md](06-operations.md).

## 3. Thinking: adaptive, explicit, off if omitted

- The only valid form is `thinking={"type": "adaptive"}`. `budget_tokens` is removed (400 error).
- **Omitting `thinking` runs without thinking on Opus 4.8.** FABLE sets it explicitly whenever `RolePolicy.thinking` is true; the `mechanical` role omits it deliberately.
- Thinking display defaults to `"omitted"`. For debugging, `{"type": "adaptive", "display": "summarized"}` surfaces summaries — useful while tuning prompts, noise in production traces.

Do not write prompt text that tries to manage the thinking budget ("think briefly", "don't overthink") — adaptive thinking self-regulates against effort, and such text just adds prefix bytes.

## 4. Cache-disciplined prompt structure

The prompt cache pays 0.1x on reads and charges 1.25x on writes, and it is a strict prefix match. That dictates the layout FABLE enforces mechanically (`FrozenPrefix`, `PrefixMutationError`):

1. **Frozen prefix** — tool schemas, then system prompt, `cache_control={"type": "ephemeral"}` on the last system text block. Byte-stable for the whole run. This is why `prompts.load()` resolves slots once from static values: a timestamp or task string interpolated into the system prompt silently zeroes your cache-hit ratio.
2. **All dynamic content in the first user message** — task, date, budget status, `memory.index()`, resume litany.
3. **Mid-conversation system messages as the operator channel** — `{"role": "system", "content": ...}` appended after a user turn (never `messages[0]`). This is how the harness talks to a running agent (context pressure warnings, budget notices) without touching the cached prefix. Prompts should tell the model these exist:

> Messages labeled as operator notices may arrive mid-task with budget or context status. Adjust behavior when they do: prefer terse output, spill large results to files, converge on completion.

Full cache arithmetic in [docs/05-memory-context.md](05-memory-context.md).

## 5. Removed API surface and its replacements

- **No assistant prefill** — starting an assistant turn for the model is a 400 on Opus 4.8. Every old prefill trick ("prefill `{` to force JSON") is replaced by structured outputs (`output_config={"format": {"type": "json_schema", ...}}`) or by system-prompt output contracts. FABLE's completion report and Blueprint both use structured output.
- **No temperature/top_p/top_k** — removed on Opus 4.8. Determinism knobs are gone; use self-consistency over discrete answers (`verify.self_consistent`) where it matters.
- **Long-document placement**: put large documents at the *top* of the user message and the query at the *bottom*. Queries buried above a 50k-token document degrade measurably.
- **XML sectioning**: fence prompt regions with tags the model can address — `<task>`, `<constraints>`, `<report_format>`. Opus 4.8's literalism makes section boundaries load-bearing: instructions inside `<constraints>` are treated as constraints.

---

## 6. The snippet library

These ship verbatim in `prompts/*.md`. Each has a name, the text, where it lives, and its citation. When you fork a prompt, keep snippets intact or re-ablate — the frontmatter `ablation_status` field tracks which snippets have been verified to move a metric.

### 6.1 `default_to_action` (orchestrator, executor)

> By default, implement changes rather than only describing them. If intent is ambiguous, infer the most useful likely action and proceed, using tools to discover missing details instead of guessing or stopping to ask. Attempt the task with the tools you have before reporting that you cannot do it.

Citation: Claude 4 best practices, "be explicit about taking action" — <https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/claude-4-best-practices>.

### 6.2 `use_parallel_tool_calls` (orchestrator, executor, researcher)

> When you intend multiple tool calls and there are no dependencies between them, make all of the independent calls in the same block. Prioritize parallel calls whenever actions can proceed simultaneously rather than sequentially — reading three files, running independent searches. Never guess or use placeholder values for missing parameters.

Citation: Claude 4 best practices, parallel tool calling guidance (same URL). Parallel tool calls are default-on in the API; this snippet raises the model's use of them.

### 6.3 `investigate_before_answering` (executor, researcher)

> Investigate before answering. Read the relevant file, run the relevant command, inspect the actual state of the system before stating a conclusion about it. Never speculate about code you have not opened. An answer grounded in one read of the real file beats a plausible guess every time.

Citation: Claude Code system-prompt lineage; Anthropic, "Claude Code best practices" — <https://www.anthropic.com/engineering/claude-code-best-practices>.

### 6.4 `anti_hardcoding` (executor)

> Write a high-quality, general-purpose solution. Do not hard-code values or special-case logic so that specific test inputs pass; implement the actual logic that solves the problem generally. If the task as stated is infeasible, or a test itself is wrong, say so plainly instead of working around it. A solution that games the checks is a failure, not a success.

Citation: Anthropic Claude 4 model card / best practices anti-reward-hacking guidance (same best-practices URL). This snippet is the prompt-side half of a pair; the harness-side half is `check.diff_scope` blocking the implementer from test files ([docs/04-verification.md](04-verification.md)).

### 6.5 `anti_over_engineering` (executor, orchestrator)

> Avoid over-engineering. Make only the changes the task requires. Do not add error handling, fallbacks, configuration options, or abstractions for situations that cannot occur in this codebase. Do not refactor, rename, reformat, or tidy code you were not asked to change — leave unrelated imperfections as found. Three similar lines do not need a helper function.

Citation: Claude 4 best practices, scope guidance (same URL). This is also FABLE's **no-tidying rule**: on a literal model it works, because the model actually obeys "leave it as found."

### 6.6 `reversibility` (executor, orchestrator)

> Prefer reversible actions. Creating files, editing under version control, and running read-only commands are safe to do autonomously. Before any action that is hard to undo — deleting files or branches, force-pushing, dropping or overwriting data, sending anything external — stop and ask.

Citation: Anthropic agent-safety guidance in Claude 4 best practices; standard containment practice.

### 6.7 `multi_window_restart` (orchestrator, executor — long-horizon runs)

> Your context window may end before the task does. Work so a fresh session can continue: keep durable state in files (progress notes, the plan, test status), record decisions where they will be found, and never keep important state only in conversation. If you are resumed, reconstruct state from the filesystem before acting — read the progress file, check the git log, re-run one verification command — rather than trusting any summary of what supposedly happened.

Citation: Anthropic Sonnet 4.5 context-awareness prompting guidance; the mechanism it feeds is FABLE's checkpoint/respawn path ([docs/05-memory-context.md](05-memory-context.md)).

### 6.8 `coverage_vs_filtering` (verifier, researcher)

> Report every issue you find, each with a severity and a confidence score. Do not pre-filter to only the findings you are certain about, and do not withhold minor findings to appear decisive. A downstream filter ranks and selects; your job is coverage.

Citation: LLM-judge bias literature (verbosity/decisiveness bias) summarized in [docs/04-verification.md](04-verification.md); pattern from Anthropic multi-agent research system write-up.

---

## 7. The dials

Each dial is a swappable paragraph. The shipped prompts pick a default; the frontmatter `knobs` field says which paragraph to swap.

### 7.1 Narration: silence vs milestones

**Silence (default — executor, all subagents):**

> Work without narration. Do not announce what you are about to do or summarize what you just did unless the result changes the plan. The trace records every action; your completion report carries the evidence. Output during the run is for course corrections only.

**Milestones (orchestrator when a human is watching the trace):**

> Emit a one-line status at phase boundaries only: what finished, its evidence id, what starts next. Nothing between boundaries.

Why silence is the default: narration tokens are output tokens at $25/MTok on Opus 4.8, they duplicate the trace, and self-narrated progress is exactly the unverified claim channel the gate exists to distrust.

### 7.2 Autonomy: small-decisions-don't-ask

> Decide small things yourself: file names, variable names, choice between two equivalent standard-library approaches, ordering of independent steps. Do not ask, and do not present options for these. Ask only when a decision is expensive to reverse or changes the scope of the deliverable — and when you ask, ask one specific question with your recommended default.

An autonomous agent that round-trips to a human for trivia is a chat app with extra steps; one that never asks force-pushes to main. The reversibility snippet (§6.6) is the boundary between the two.

### 7.3 Grounded progress claims

> Every claim of progress names its evidence. Say "pytest exited 0 (tool call toolu_01Ab...)" and "created /abs/path/report.md (toolu_01Cd...)", not "tests should pass now" or "the report is ready." If you have not run it, you do not know it, and you say so. Claims without a tool-call id will be rejected by the completion gate.

This is the prompt-side contract that makes `verify.audit_claims` (a zero-token deterministic script) possible: the completion report schema requires `tool_use_ids` per claim, and the ledger has ground truth for each id. Prompt and harness enforce the same rule from both sides.

### 7.4 Search-first

> When you need information about the codebase or environment, search before you reason. Glob for the file, grep for the symbol, read the definition, then conclude. Do not answer from what the code "probably" looks like, and do not reconstruct file contents from memory of an earlier read if cheap re-reading is possible.

JIT retrieval beats speculation and beats RAG for code (see [docs/05-memory-context.md](05-memory-context.md) §8).

---

## 8. Anti-overplanning: the AutoGPT fixes

AutoGPT-era agents died of planning: infinite plan refinement, re-planning already-executed work, perfectionism with no stop. Four prompt-level fixes, all present in `prompts/orchestrator.md` and `prompts/planner.md` (the harness-level fix — step status is harness-owned and the model cannot re-plan executed steps — is in [docs/01-architecture.md](01-architecture.md)):

1. **Closed capability list.**
   > Your complete capability set is the tools listed above. If an action is not achievable through those tools, you cannot do it — do not plan steps that assume capabilities you do not have, and do not ask for new ones mid-run.
2. **Conditional plan depth.**
   > Match plan depth to task size: one step if the task is trivial, and never more than seven steps. A plan step is an observable action with a checkable outcome, not a phase of thinking. If you cannot name the command or artifact that verifies a step, it is not a step.
3. **Few-shot concise plans.** `prompts/planner.md` includes a one-step blueprint example and labels it legal and encouraged — without the example, models pad plans to look thorough.
4. **"Good enough" termination norm.**
   > Done means the acceptance checks pass — not that no further improvement is imaginable. When the verifier is green, stop. Polishing beyond the checks spends budget and adds risk without adding verified value.

And the completion handshake that makes all four enforceable:

> You cannot declare the task complete. You submit a completion report — summary, claims with tool-call evidence ids, artifact paths — and the gate decides. "end_turn" is a claim, not a status.

## 9. Tool descriptions are prompt engineering

Tool descriptions are read on every turn and steer every dispatch decision; they are the highest-leverage prose in the system after the system prompt itself. Anthropic's multi-agent team found that having an agent rewrite flawed tool descriptions cut task completion time for future agents using those tools by 40% (<https://www.anthropic.com/engineering/built-multi-agent-research-system>).

Rules, enforced by `ToolRegistry`'s lint where mechanically checkable:

- **Write for a junior developer** who has never seen your system: what the tool does, when to call it, what it returns, and its failure modes.
- **Be prescriptive**: "Call this when you need file contents and know the path; for finding paths, use glob instead." The *when* clause prevents the two most common dispatch errors — wrong tool, and reasoning instead of calling.
- **Unambiguous parameter names**: `user_id`, not `user`. The registry lint rejects ambiguous names and empty docstrings.
- **State constraints in the description**, not only in code: "Paths must be absolute; relative paths are rejected." The model reads descriptions; it does not read your validators.
- **Keep the set small.** FABLE ships fewer than eight built-ins. Overlapping tools ("search" vs "find" vs "locate") force the model to guess your intent; distinct purposes, distinct names.

In FABLE, `@tool` takes the schema from type hints and the description verbatim from the docstring — so the docstring *is* the prompt. Review docstrings the way you review the system prompt.

## 10. Prompt file conventions

Every file in `prompts/` carries YAML frontmatter — `id`, `version`, `model_notes`, `source_citation`, `ablation_status`, plus `when_to_use` and `knobs` — stripped by `prompts.load()` before the text reaches the model. `{{slots}}` are resolved once at load time from static values only; per-turn dynamic content never enters these templates. HTML comments in the bodies mark knob boundaries; they reach the model and are harmless, but you may strip them in a fork.

| File | Role | Doc section |
|---|---|---|
| [prompts/orchestrator.md](../prompts/orchestrator.md) | Top-level autonomous orchestrator | §6.1, §6.2, §7, §8 |
| [prompts/planner.md](../prompts/planner.md) | Blueprint Author (plan phase, in-context) | §8 |
| [prompts/executor.md](../prompts/executor.md) | Focused task executor | §6.3–§6.7, §7.1, §7.3 |
| [prompts/verifier.md](../prompts/verifier.md) | Adversarial refuter | §6.8 |
| [prompts/researcher.md](../prompts/researcher.md) | Research subagent | §6.2, §6.3, §6.8 |
| [prompts/extraction.md](../prompts/extraction.md) | Honest methodology-extraction protocol | [docs/00-philosophy.md](00-philosophy.md) |

What none of these prompts can do: raise per-step correctness. They buy adherence to procedure, honest reporting, and cheap verifiability — the pass^k levers. The intelligence lever is `effort`, and above that, the model tier. See [docs/06-operations.md](06-operations.md) for when to pull which.
