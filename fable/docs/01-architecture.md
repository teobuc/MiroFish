# 01 — Architecture: One Dumb Loop, Wrapped in Smart Rails

| | |
|---|---|
| **What transfers** | Everything in this document. The loop, the gates, the rails, the ledger, the trace are host code — they work identically regardless of which model sits in the CALL box. |
| **What doesn't** | What happens *inside* the CALL box. The loop cannot make a model call smarter; it can only make its failures cheap, typed, and recoverable. |

FABLE has exactly one engine: the state machine in `src/fable/loop.py`.
Subagents run it recursively. Orchestration is data fed to it, not a second
engine beside it. This document walks the machine, the components that
implement it, and the failure containment around it.

## 1. The deliberate-triviality thesis

The loop is intentionally dumb: assemble messages, call the model, dispatch on
`stop_reason`, run tools or run the gate, repeat. No graph DSL, no learned
router, no planner process negotiating with an executor process. Every
production harness we studied — SWE-agent, Claude Code, Anthropic's research
system, Aider — converged on the same shape: a while-loop over tool calls,
with all sophistication pushed into (a) what the model sees (context
engineering) and (b) what the host verifies (gates and rails).

The reason is debuggability compounding. Every piece of cleverness inside the
loop is a piece you cannot inspect when a run goes sideways at 2am and $4 of
spend. Cleverness in the *control plane* (host code) is a stack trace;
cleverness in the *data plane* (model behavior) is archaeology. So FABLE
keeps the loop trivial and spends its complexity budget on rails, gates, and
evidence — all host-side, all deterministic, all testable without an API key.

## 2. The state machine

```text
                              ┌──────────────────────────────────────────────┐
                              │  CONTROL PLANE (host code, zero context $)   │
                              │  StopRails · Gates · Router · UsageLedger    │
                              │  TraceWriter · detectors · pressure policy   │
                              └──────────┬───────────────────────────────────┘
                                         │ observes / halts / redirects
                                         ▼
  INIT ──► ASSEMBLE ──► CALL ──► DISPATCH(stop_reason)
              ▲                     │
              │                     ├─ "tool_use"   ──► TOOLS ──► (append results) ──┐
              │                     │                                                │
              └────────────────────────────────────◄─────────────────────────────────┘
                                    │
                                    ├─ "pause_turn" ──► (handled inside FableClient; loop never sees it)
                                    ├─ "max_tokens" ──► append + "continue" note; >3 ──► RunResult("overflow")
                                    ├─ "refusal"    ──► RunResult("refusal")            [terminal]
                                    │
                                    └─ "end_turn"   ──► GATE  ── pass ──► RunResult("ok")
                                                         │
                                                         ├─ fail ──► evidence fed back ──► ASSEMBLE
                                                         │           (≤ gate_max_retries)
                                                         └─ exhausted ──► escalate once ──► or RunResult("failed_gate")

  RAILS (checked every iteration, host-side):
    max_turns │ budget_usd (projected pre-call) │ wall clock │ stall detector
    any trip ──► typed terminal RunResult with usage + trace path
```

### INIT (once per run, no model call)

1. Resolve `FableConfig` — frozen for the run. Immutability is load-bearing:
   an immutable config is what makes the prefix byte-stable (step 2).
2. Compile the `FrozenPrefix`: tool schemas from the `ToolRegistry` plus the
   system prompt, with `cache_control={"type": "ephemeral"}` on the *last*
   system text block. Hash it. Every subsequent call asserts the same digest;
   a mismatch raises `PrefixMutationError`. This is CI-testable and is the
   single biggest cost lever in the framework (cache reads are 0.1x input
   price — see docs/05-memory-context.md §1).
3. Put ALL dynamic content — the task, the date, budget status,
   `memory.index()`, the resume litany — into the *first user message*. Never
   into the prefix. One volatile byte in the prefix and every call re-pays
   full input price.
4. Open the `TraceWriter` (append-only JSONL) and the `UsageLedger`.
5. If memory is configured and `.fable/memory/state/` exists, prepend
   `memory.resume_prompt()`: pwd, git log, progress.md, feature_list.json,
   pick ONE feature, re-run one smoke check. Fresh sessions rediscover state
   from disk; they do not inherit a summarized transcript
   (docs/05-memory-context.md §6).
6. Optional planning (Tier 2, `plan=True`): the first call emits a
   `Blueprint` via structured output — typed `Step`s, each with an observable
   `action` and a `verifier` (a shell command, or the literal `"judgment"`).
   A mechanical plan gate checks it — host code, no model call: every step
   has a non-empty verifier and an observable action, and the step count
   stays within `blueprint_max_steps` (≤7 by default). A failing plan raises
   a typed `ValueError` immediately; there is no critic call and no
   bounce-back retry. The blueprint persists
   to `state/plan.md` and `state/feature_list.json`. Step status is
   **harness-owned**: the model cannot mark executed steps back to pending or
   re-plan finished work. (This is the fix for AutoGPT's signature death
   spiral — endless re-planning of already-done work.) Tier 0/1 skips all of
   this; a trivial task gets a one-step implicit blueprint and no plan call.

### ASSEMBLE

`messages` is a *projection* over the append-only trace — never the trace
itself. History is never rewritten. When context pressure forces clearing or
compaction (§6), the projection changes; the JSONL keeps every byte. Resume,
fork, and audit fall out for free, because the ground truth was never
mutated.

### CALL

All model access goes through `FableClient.call()` — the only module that
imports the `anthropic` SDK. Non-negotiables, in code and here:

- Always `client.messages.stream(...)` as a context manager +
  `stream.get_final_message()` (required for max_tokens beyond ~16k; used
  unconditionally for uniformity).
- `thinking={"type": "adaptive"}` set explicitly when the role's policy has
  thinking on. Omitting the parameter *disables* thinking on Opus 4.8 — the
  default is off, so the harness never relies on it.
- `output_config={"effort": policy.effort}` — effort is the primary
  intelligence/cost dial (docs/06-operations.md §2).
- No `temperature`/`top_p`/`top_k` (400 on Opus 4.8), no `budget_tokens`
  (removed), no assistant prefill (400) — structured output replaces prefill.
- The ledger records all four usage fields (`input_tokens`,
  `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`)
  plus USD on every call, and maintains a rolling cache-hit ratio. Below 80%
  it warns: that is the canonical symptom of a mutated prefix.

### DISPATCH — on `stop_reason`, checked BEFORE touching content

Order matters: on `"refusal"` the content array may be empty, so any code
that reads `content[0]` before checking `stop_reason` is a latent crash.
`ModelTurn.text` encodes this check so callers cannot get it wrong.

- **`"refusal"`** → terminal `RunResult(status="refusal")`, with
  `stop_details` attached. No retry — a refusal is a signal, not a flake.
- **`"pause_turn"`** → never reaches the loop. `FableClient.call()` appends
  the partial assistant content and re-sends internally.
- **`"max_tokens"`** → append the truncated content plus an operator note
  ("output truncated, continue"); after 3 recoveries, terminal
  `status="overflow"`.
- **`"tool_use"`** → TOOLS phase, below.
- **`"end_turn"`** → CANDIDATE-DONE. This is the load-bearing difference from
  a chat loop and gets its own section (§4).

### TOOLS

Executed by `tools.execute()`, which enforces, in order:

1. **Parse, never string-match.** Every tool input is json-parsed. (With
   `strict: true` on tool definitions the API guarantees schema-valid input,
   but the parse still happens — belt and suspenders costs nothing.)
2. **Concurrency by declaration.** Tools flagged `parallel_safe` run
   concurrently in a thread pool; mutating tools serialize in block order.
   The tool author declares safety; the harness never guesses.
3. **Evidence before shaping.** Each raw result is recorded to the Evidence
   Ledger — content hash, exit code, raw-output path — *before* any
   truncation. The model sees shaped output; the gate's claim audit sees
   ground truth. This ordering is what makes the audit deterministic (§4).
4. **Shape.** Results are capped at `tool_result_cap_chars` (default 25,000);
   overflow spills to `scratch/` and is replaced by the path, head/tail
   excerpts, and a grep hint. Empty output becomes
   `"Command ran successfully with no output"` (silence reads as failure to
   models). Exceptions become `is_error: true` tool_results — a tool failure
   is an *observation* for the model, never a crash of the loop.
5. **Message discipline.** Append the full `response.content` as the
   assistant turn, then exactly ONE user message containing ALL tool_result
   blocks. Then back to ASSEMBLE.

## 3. Components and the import DAG

| Module | Responsibility | Plane |
|---|---|---|
| `config.py` | `FableConfig`, `ModelTier`/`TIERS`, `RolePolicy`, `Budget`, `Router`. Every tunable constant in the system lives here — a unit test greps `loop.py` for bare numeric literals. | control |
| `trace.py` | Append-only JSONL `TraceWriter`, `TraceReader` reports, `detect_failures` (seven deterministic detectors). Shares MiroFish's `ReportLogger` field names (`timestamp`, `elapsed_seconds`, `action`, `details`). | control |
| `client.py` | The only SDK import. `FrozenPrefix`, `ModelTurn`, `UsageLedger` (usage + Evidence Ledger), `FableClient` with the single retry/backoff implementation and the one structured-output repair ladder. | boundary |
| `prompts.py` | Loads `prompts/*.md`, strips frontmatter, resolves `{{slots}}` once at load time — never per-turn (cache discipline). | control |
| `tools.py` | `@tool` decorator, schema generation, `ToolRegistry` (freezable, linted), `execute()` per §2-TOOLS, built-ins (`fs_tools`, `shell_tool`, `think_tool`). | boundary |
| `verify.py` | `Evidence`, `Check`, `Gate`, the `check.*` constructors, `audit_claims`, `refute`, `self_consistent`, `assert_red`. | control |
| `memory.py` | File-based memory (`MEMORY.md`, `lessons/`, `state/`, `scratch/`), `Checkpoint`, `resume_prompt()`, path containment, `mark_passed` (the only code path that flips `passes: true`). | control |
| `loop.py` | The engine. §2's state machine, rails, pressure ladder, gate invocation. | both |
| `subagents.py` | `Brief`, `spawn`, `fan_out`, `spawn_subagent_tool`. Re-enters `loop.py`; nothing re-enters it. | data |
| `evals.py` | `run_eval` (pass@1 / pass^k), `gap_closure` (the honesty metric from docs/00). | control |

Import DAG (acyclic; authors and reviewers enforce it):

```text
config ── stdlib only
trace ── stdlib only
client ──► config, trace
prompts ── stdlib only
tools ──► config, trace
verify ──► config, client, trace
memory ──► config
loop ──► config, client, tools, verify, memory, trace, prompts
subagents ──► loop (and everything below)
evals ──► loop, subagents
```

The one rule worth italicizing: *`loop.py` never imports `subagents.py`.* The
orchestrator acquires multi-agent capability by registering the tool returned
by `subagents.spawn_subagent_tool()` in its ordinary tool list. Orchestration
is data — a tool call like any other — not a second engine. One engine means
one place where rails, gates, tracing, and pressure policy exist, and
subagents inherit all of it by construction rather than by copy-paste.

### Control plane vs data plane

- **Data plane** (costs tokens): messages, tool results, digests,
  checkpoint content used as boot context. Everything the model reads.
- **Control plane** (costs zero tokens): rails, gates, router, ledger, trace,
  failure detectors, containment checks, pressure policy. Host code that
  observes the data plane and halts, redirects, or escalates.

This split is why FABLE's reliability machinery is free at the margin: a
budget check, a stall detector, and a claim audit consume no context window
and add no cache-invalidation risk. When in doubt about where a feature
belongs, put it in the control plane; only move it into the data plane when
the model *needs to see it to act differently* (and then deliver it as a
mid-conversation system message, §6, to protect the cached prefix).

## 4. end_turn is a claim: the gate handshake

In a chat loop, `end_turn` means done. In FABLE it means *the model claims to
be done* — and models' completion claims are exactly as trustworthy as their
introspection (docs/00-philosophy.md §1a). The harness, not the model,
decides completion. Sequence:

```text
model                       loop (host)                      gate (host)
  │                            │                                │
  │── end_turn + completion ──►│                                │
  │   report {summary,         │── report + ledger ────────────►│ 1. claim audit (0 tokens):
  │   claims:[{text,           │                                │    every done/passing/created/
  │   tool_use_ids}],          │                                │    fixed claim must cite a
  │   artifacts}               │                                │    tool_use_id whose LEDGER
  │                            │                                │    evidence supports it
  │                            │                                │ 2. mechanical: decisive commands
  │                            │                                │    re-run in a FRESH subprocess
  │                            │                                │    (transcript of a test run is
  │                            │                                │    a claim, not proof)
  │                            │                                │ 3. judged: fresh-context rubric,
  │                            │                                │    artifacts only, never the
  │                            │                                │    generator's rationale
  │                            │                                │ 4. adversarial (optional):
  │                            │                                │    refuter hunts one concrete
  │                            │                                │    counterexample
  │                            │◄─── GateResult + Evidence ─────│
  │                            │
  │◄── failures, verbatim, ────│  (on fail; ≤ gate_max_retries,
  │    as a user message       │   then escalate once via Router
  │                            │   or RunResult("failed_gate"))
```

The ladder runs cheapest-first, so most failures die at step 1 or 2 without
spending a judge token. Checks return `Evidence` objects (command, exit code,
output tail, artifact path) — never bare booleans — and failure evidence goes
back to the model verbatim, because "the gate failed" teaches nothing while
"pytest exited 1, tail: AssertionError on line 214" teaches everything.

If no gate is configured (`verify=None`), an `end_turn` terminates with
`status="ok_unverified"` — deliberately not `"ok"`. The type system nags, and
the nag is the pedagogy. Full gate design: docs/04-verification.md.

## 5. Data flow of one turn

1. **Assemble** the projection over the trace (plus any pending pressure
   edits).
2. **Call** through `FableClient` — prefix digest asserted, stream consumed,
   usage → ledger, `model_turn` event → trace.
3. **Dispatch** on `stop_reason` (§2).
4. **Execute tools** — raw result → Evidence Ledger, `tool_call`/
   `tool_result` events → trace, shaped result → data plane.
5. **Feed back** — one user message of tool_results, or gate evidence, or an
   operator system message.
6. **Rails check** — before the next call, project its cost from measured
   context size (`ledger.project_next_call_usd`) and trip *before* spending,
   not after.

Every arrow in that list emits a `TraceEvent`. The trace is a product
surface, not a debug log: `TraceReader` computes cost-by-role, cache-hit
series, and claim-to-evidence lookups from it, `trace.detect_failures` runs
its seven deterministic detectors over it, and the line schema shares
MiroFish's ReportLogger field names so their existing JSONL viewer is
expected to read it — verify against a real trace (docs/06-operations.md §6).

## 6. Context pressure (summary; full treatment in docs/05)

Thresholds are measured against `input + cache_read + cache_creation` versus
the tier's window, and read from `config.budget`:

- **60%** — append a mid-conversation `{"role": "system", ...}` operator
  message (after a user turn, never `messages[0]`): prefer terse output,
  spill large results to files. This channel exists precisely because it
  preserves the cached prefix while still steering the model.
- **75%** — clear oldest tool_results from the *projection* (keep the last
  5; a placeholder notes the removal and the scratch path). Only if it
  reclaims ≥10k tokens: every projection edit invalidates cached suffix and
  re-writes at 1.25x input price, so the loop does the arithmetic before
  editing. Client-side by default; the API-native
  `clear_tool_uses_20250919` context edit (beta
  `context-management-2025-06-27`) sits behind `use_context_editing=True`.
- **85%** — write a `Checkpoint` to memory, then either continue compacted
  or return `status="checkpointed"` so the caller respawns a fresh session
  that boots from `resume_prompt()`. For very long runs, a fresh window plus
  filesystem rediscovery beats lossy summarization: files don't rot,
  summaries do.

## 7. Typed exits: the full Status set

`RunResult.status` is a closed literal set. Every run ends in exactly one of
these, with usage totals and the trace path attached — there is no exit that
loses the ledger.

| Status | Fires when |
|---|---|
| `ok` | Gate passed; `evidence` carries the proof. |
| `ok_unverified` | `end_turn` with no gate configured. Honest, and nagging. |
| `failed_gate` | Gate exhausted `gate_max_retries` and no escalation path remained. |
| `refusal` | `stop_reason == "refusal"`; `stop_details` attached; terminal immediately. |
| `max_turns` | Tool-turn rail tripped. |
| `budget_exceeded` | Projected cost of the *next* call would cross `budget.max_usd`. |
| `timeout` | Wall-clock rail tripped. |
| `stalled` | Stall detector saw n-gram repetition over recent `(tool, args_hash)` pairs — the agent is circling. |
| `overflow` | Three `max_tokens` recoveries failed. |
| `checkpointed` | 85% pressure path chose respawn; `checkpoint_path` is set — feed it to `Agent.resume`. |
| `escalated` | Run handed off upward after a Router escalation was itself exhausted. |

## 8. Subagent recursion: one engine, orchestration as data

`subagents.spawn(brief)` runs the *same* `loop.Agent` with: fresh context
(none of the parent's transcript), tools scoped to the brief, model and
effort routed by role via the `Router`, and a hard return contract — a
digest of at most `subagent_digest_max_tokens` (default 2,000) plus artifact
*paths*. Full findings go to `scratch/` files; only paths cross the
boundary. The sidechain transcript gets its own JSONL, so attribution is
structural: one subagent, one responsibility, one trace file. Cost honesty:
multi-agent runs are ~15x chat tokens, and that number is stated in the
`spawn` docstring where you will hit it, not buried here. Patterns built on
`spawn`/`fan_out` are cataloged in docs/03-orchestration.md.

## 9. Stop rails and the AutoGPT failure map

AutoGPT-era agents established the canonical long-horizon failure modes.
Each maps to a specific host-side rail — none is handled by asking the model
to please behave:

| Classic failure mode | What it looks like | Rail that kills it |
|---|---|---|
| Divergence / rabbit-holing | Agent wanders off-scope, each step locally plausible | `max_turns`; `check.diff_scope` fails out-of-scope file touches at the gate; scope literalism in `prompts/executor.md` |
| Re-planning executed work | Plan regenerated every few turns, done work re-done | Harness-owned `Step.status` — the model cannot flip `done` back to `pending`; blueprint persisted to disk, not held in the model's head |
| Perfectionism / never terminating | Endless polishing, "one more improvement" | Gate defines done externally: pass = stop; "good enough" termination norm in `prompts/orchestrator.md`; wall-clock rail as backstop |
| Loop / stall | Same command, same args, again and again | Stall detector: n-gram repetition over `(tool, args_hash)` history → `status="stalled"` |
| Unbounded cost | The $200 overnight surprise | `budget_usd` rail, checked against the ledger's *projection* before each call — the run stops before the overrun, not after |
| Fabricated completion | "All tests pass" with no test run in the transcript | Claim audit: every completion claim must cite ledger evidence; ungrounded claims fail at zero token cost |
| Error laundering | Failure output paraphrased into optimistic prose | Evidence Ledger keeps raw hashes/exit codes pre-shaping; `trace.detect_failures` flags divergence between ledger and narrative |

Every rail trip produces a typed `RunResult`, never an exception, never a
silent truncation. The run's last act is always the same: totals to the
ledger, `run_end` to the trace, a status you can `match` on.

---

*Previous: [00-philosophy.md](00-philosophy.md) — why the harness is the
transferable layer. Next: [02-prompting.md](02-prompting.md) — what the model
in the CALL box needs to be told, and what Opus 4.8 no longer needs to be
told.*
