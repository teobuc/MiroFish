# FABLE

**Framework for Autonomous Blueprinted Long-horizon Execution**

FABLE is an open, honest, production-grade harness for building autonomous agents on
`claude-opus-4-8`. It packages the ~80% of "frontier agent magic" that is actually
harness engineering — orchestration loops, verification gates, evidence ledgers,
context/cache discipline, effort routing, multi-agent patterns — as a documented,
runnable Python framework with no dependencies beyond the `anthropic` SDK.

It will not make your model smarter. It will make your model *reliable*, *cheap*,
*auditable*, and able to run for hours instead of minutes. Those are different claims,
and this repository is careful never to confuse them.

---

## The origin story (and why the honest version is better)

A viral tweet claimed you could "clone Fable-class behavior into Opus 4.8" by asking
the frontier model for its own "operating manual" and pasting it into a system prompt.

**The premise is false.** Capability lives in weights. Anthropic's own introspection
research found models detect injected concepts in their activations only ~20% of the
time under favorable conditions — the "operating manual" a model writes about itself
is largely confabulation, not a readout of its mechanisms. And imitation-tuning
research (Gudibande et al.) showed that even 150M tokens of frontier-model outputs
transfer *style*, not *capability*. A pasted prompt transfers less. Chain-of-thought
explanations are frequently unfaithful to the computation that produced the answer
(Turpin et al.). You cannot prompt your way to a bigger model.

**But the tweet felt true for a reason.** Most of what makes frontier *agents* feel
magical is not the model — it is the harness around it. The canonical numbers:

- SWE-agent raised GPT-4's SWE-bench score from 3.8% to 12.5% by changing **only the
  agent-computer interface** — same model, better tools and feedback.
- Medprompt (prompting + ensembling harness) beat a fine-tuned Med-PaLM 2 on medical QA.
- Anthropic's multi-agent research system reported a 90.2% improvement over
  single-agent — driven mostly by parallel token spend, orchestrated well.
- τ-bench: models that pass a task >60% of the time once (pass@1) succeed all 8
  times (pass^8) less than 25% of the time. Verification and retry raise pass^k
  dramatically **without changing per-step correctness at all**.

That layer — the harness — transfers completely, because it is ordinary software.
FABLE is that layer, written down, with every claim bounded and every cost stated.
The full argument, with citations, is in [docs/00-philosophy.md](docs/00-philosophy.md).
The honest replacement for the tweet's "extraction" idea — a trajectory-based protocol
that actually works, with a checkable `gap_closure` metric — is in
[prompts/extraction.md](prompts/extraction.md).

### What transfers via scaffolding / what does not

| Transfers (harness) | Does not transfer (weights) |
|---|---|
| Procedure adherence, tool discipline | Knowledge and reasoning ceiling |
| Verification loops, output contracts | Per-step correctness *p* |
| Recovery patterns, cost discipline | Calibration of confidence |
| Long-horizon *pass^k* via gates + retries | The p^n floor on unverified chains |

---

## Architecture

One engine (the loop), wrapped by a control plane that costs zero context tokens.
Orchestration is data: subagents are just a tool whose executor re-enters the same loop.

```
┌─ CONTROL PLANE (host code — zero tokens) ─────────────────────────────────┐
│                                                                           │
│  StopRails        Gate ladder           Router            UsageLedger    │
│  max_turns        1 claim audit         role -> tier      4 usage fields │
│  budget_usd       2 mechanical (fresh   effort policy     + USD + cache  │
│  wall clock          subprocess)        escalate on       hit ratio      │
│  stall detect     3 judged rubric       gate failure                     │
│                   4 adversarial refuter                                  │
│                                                                           │
│  TraceWriter (append-only JSONL)   failure detectors   context-pressure  │
│                                                         ladder 60/75/85% │
│  ┌─ DATA PLANE (costs tokens) ────────────────────────────────────────┐  │
│  │                                                                    │  │
│  │   frozen prefix (system + tool schemas, cached, byte-stable)       │  │
│  │        │                                                           │  │
│  │        v            stop_reason?                                   │  │
│  │   ASSEMBLE ──> CALL ────────────┬─ tool_use ──> TOOLS ──┐          │  │
│  │      ^         (stream,         │   parallel-safe concurrent,      │  │
│  │      │          adaptive        │   evidence ledgered, shaped,     │  │
│  │      └──────────thinking)       │   ONE user msg of tool_results   │  │
│  │      ^                          │                       │          │  │
│  │      └──────────────────────────┼─ end_turn = a CLAIM ──┼──> GATE  │  │
│  │         retry w/ evidence       │                       │    pass? │  │
│  │                                 └─ refusal/overflow ──> typed exit │  │
│  │                                                                    │  │
│  │   subagents: spawn() re-enters this same loop, fresh context,      │  │
│  │   scoped tools, digest <= 2k tokens back across the boundary       │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────────┘
```

The load-bearing difference from a chat loop: **`end_turn` is a claim, not
completion.** The model cannot declare itself done. It submits a completion report
whose every "done/passing/created/fixed" statement must cite a `tool_use_id` whose
ledgered output supports it; then mechanical checks re-run in fresh subprocesses
(the agent's transcript of a test run is a claim, not proof); then, optionally, a
judged rubric and an adversarial refuter. The harness sets done.

---

## Quickstart

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
```

```python
from pathlib import Path
from fable import run, tool, check

@tool
def write_file(file_path: str, content: str) -> str:
    """Write content to the file at the given absolute path, creating it."""
    Path(file_path).write_text(content)
    return f"Wrote {len(content)} chars to {file_path}"

@tool(parallel_safe=True)
def word_count(file_path: str) -> str:
    """Count lines and words in the file at the given absolute path."""
    text = Path(file_path).read_text()
    return f"{len(text.splitlines())} lines, {len(text.split())} words"

result = run(
    "Write a limerick about prompt caching to /tmp/limerick.txt, "
    "then report its word count.",
    tools=[write_file, word_count],
    verify=check.file_exists("/tmp/limerick.txt"),
    budget_usd=1.00,
)

print(result.status)                      # "ok" — gate-verified, not self-declared
print(f"${result.cost_usd:.4f}")          # every run is metered
print(f"cache {result.cache_hit_ratio:.0%}")  # <80% means you broke the prefix
print(result.trace_path)                  # append-only JSONL of everything
```

That is the whole beginner API: `run`, `@tool`, `check`. You get streaming, adaptive
thinking, prompt-cache discipline, budget rails, a stall detector, an evidence ledger,
and a verification gate without asking for any of them. Omit `verify=` and the result
comes back `status="ok_unverified"` — the type system nags you, honestly.

## Four tiers of adoption

- **Tier 0 — `run()`.** One function. Default executor prompt, budget rails, trace.
- **Tier 1 — `+ verify=`.** A `Check`, a list of them, or a full `Gate`. This is the
  single highest-leverage line you will add.
- **Tier 2 — `Agent`, `plan=True`, subagents, memory.** Typed Blueprints whose
  per-step `verifier:` field drives model/effort routing; `spawn`/`fan_out` for
  parallel workers; file-based memory with checkpoint/resume for runs longer than
  a context window.
- **Tier 3 — hooks.** `pre_call` / `post_tool` / `on_checkpoint` for surgical control.

## File map

```
README.md                    You are here.
docs/
  00-philosophy.md           The honest thesis: what transfers, what doesn't; the tweet, debunked and salvaged.
  01-architecture.md         The loop state machine, control vs data plane, typed exits, end_turn-as-claim.
  02-prompting.md            System-prompt engineering for Opus 4.8: literalism, effort, tool descriptions.
  03-orchestration.md        Eight multi-agent recipes with token multipliers; when one agent is enough.
  04-verification.md         The gate ladder, judge discipline, TDD with a harness-owned red test, pass^k evals.
  05-memory-context.md       Cache economics, the 60/75/85 pressure ladder, file-based memory, resume litany.
  06-operations.md           Cost engineering: effort routing, cascades, observability; MiroFish case study.
prompts/
  orchestrator.md            Top-level autonomous orchestrator (cannot declare completion — the gate decides).
  planner.md                 Blueprint Author: typed steps, each with a verifier field that IS the routing policy.
  executor.md                Focused executor: grounded claims, silence default, scope literalism.
  verifier.md                Adversarial refuter: "produce one concrete counterexample," never "do you agree?"
  researcher.md              Search-first researcher: cite or mark unverified; digest contract.
  extraction.md              The honest methodology-extraction protocol the tweet should have described.
src/fable/
  __init__.py                Eleven exports, one screen: run, Agent, RunResult, tool, Tool, check, Gate, Check, Evidence, FableConfig, Budget.
  config.py                  Frozen FableConfig: model tiers, role policies, budgets, every tunable constant.
  client.py                  The ONLY module that imports the anthropic SDK: streaming, retries, frozen prefix, usage ledger.
  tools.py                   @tool decorator, schema generation, parallel-safe execution, evidence capture.
  loop.py                    The one engine: ASSEMBLE -> CALL -> DISPATCH -> TOOLS | GATE, rails, pressure ladder.
  subagents.py               spawn/fan_out: same loop, fresh context, scoped tools, <=2k-token digests back.
  verify.py                  check.* constructors, Gate, claim audit, refuter, self-consistency, assert_red.
  memory.py                  Markdown memory store, checkpoints, resume prompt, harness-only pass-marking.
  trace.py                   Append-only JSONL trace + seven deterministic failure-mode detectors.
  prompts.py                 Tiny loader for prompts/*.md; slots resolved once — cache discipline enforced.
  evals.py                   pass@1 / pass^k and the three-arm gap_closure honesty metric.
examples/
  01_single_agent.py         ~80 lines: run() + fs_tools + a fresh-process pytest gate; reads its own trace.
  02_research_swarm.py       Orchestrator + 3 parallel researchers + rubric gate; per-role costs make the 15x honest.
  03_coding_agent.py         Full cascade: blueprint, red test, diff-scope-blocked implementer, refuter, checkpoint/resume.
```

## When to use FABLE

- Tasks with a **checkable definition of done** — tests pass, file exists, schema
  validates, command exits 0. Verification is the whole point; feed it something
  verifiable and cheap models plus retries routinely beat one expensive pass.
- **Long-horizon work** (minutes to hours): multi-step coding, research synthesis,
  data pipeline runs — anywhere unverified chains would decay as p^n.
- When you need **auditability**: every run leaves an append-only trace where every
  "done" claim links to the tool output that proves it.
- When you need **cost control**: budget rails, effort routing, cache discipline,
  and a ledger that tells you cost by role.

## When NOT to use FABLE

- **Single-shot Q&A or chat.** A plain `messages.create` call is simpler and cheaper.
  Harness overhead only pays for itself over multiple turns.
- **Tasks where you cannot state what "done" looks like.** FABLE will run them
  (`status="ok_unverified"`) but you are buying none of what makes it worth having.
- **Tasks that need capability the model lacks.** No gate ladder fixes a wrong
  answer the model cannot produce in any of k attempts. Verification raises pass^k,
  never per-step p. If pass@1 is near zero, you need a better model, not a harness.
- **Hard-realtime or sub-second latency paths.** Gates, fresh-subprocess checks, and
  retries trade latency for reliability, deliberately.

## What it costs (honest numbers up front)

| Pattern | Token multiplier vs one chat call |
|---|---|
| Single agent with tools | ~4x |
| Multi-agent swarm | ~15x |
| Verification budget on high-stakes tasks | 20–50% of total tokens |

That verification spend is a *purchase* of pass^k, and it is usually the cheapest
one available: failing runs burn ~4x, so cheap early gates are a cost feature.
A worked cost model in [docs/06-operations.md](docs/06-operations.md) suggests
cache discipline (0.1x reads), cascade routing, and effort calibration can take
a naive all-Opus workload from ~$34k/mo to ~$3–5k/mo — modeled from stated,
illustrative assumptions, not measured benchmarks; run `evals.run_eval` and read
your own ledger before believing any of those numbers on your workload.

## For MiroFish developers

FABLE was extracted alongside MiroFish and its trace lines carry the same field
names as the existing `ReportLogger` JSONL (`timestamp`, `elapsed_seconds`,
`action`, `details`), so the frontend log viewer is expected to read FABLE runs —
verify against your viewer before relying on it; this compatibility is a design
goal, not yet a tested guarantee. Three shims adopt it incrementally: (1) a
`chat()/chat_json()` facade over `FableClient`, (2) the hand-rolled ReAct section
generator replaced by `Agent` + `@tool`-wrapped Zep tools (native tool_use deletes
the three-tier regex parser), (3) the JSON-mode retry paths —
`_call_llm_with_retry` in `simulation_config_generator.py` and the
fence-stripping `chat_json` parse in `utils/llm_client.py` — replaced by
`client.structured()`. Details in [docs/06-operations.md](docs/06-operations.md).

## FAQ

**Does this make Opus 4.8 as good as a Fable-class model?**
No. Nothing in this repository changes what the model can do on a single step —
that lives in the weights, and no prompt, loop, or gate moves it. What FABLE does
is make Opus 4.8 **as good as Opus 4.8 can be**: verified instead of self-reported,
recoverable instead of brittle, metered instead of open-ended, and able to sustain
multi-hour horizons that raw p^n decay would otherwise kill. On tasks where
verification and retry dominate — which is most real agentic work — that difference
is larger than most model-tier differences. On tasks gated by raw per-step
intelligence, it is zero, and we say so.

**So the viral tweet was completely wrong?**
The mechanism it proposed (ask the model for its operating manual, paste it in) is
confabulation-powered and does not survive an A/B test. The instinct behind it —
"most of the frontier feel is transferable" — is right, but the transferable part
is harness engineering, not self-description. FABLE ships the honest version,
including a `gap_closure` eval (`fable.evals`) so you can measure exactly how much
of the weak-to-strong gap the scaffolding closes on *your* tasks. We publish low
numbers when we get them; a measured boundary is the product working.

**Why is `end_turn` not trusted?**
Because on agentic benchmarks, self-reported completion is one of the most common
failure modes: fabricated status, premature completion, error laundering (three of
the seven detectors in `fable.trace.detect_failures`). The model's transcript of a
test run is a claim; the gate re-runs the command in a fresh subprocess and keeps
the exit code in an evidence ledger the model never touches.

**Do I need the multi-agent machinery?**
Almost certainly not at first. Single agent is the default; the cheapest sufficient
pattern wins. Reach for `spawn`/`fan_out` when the task genuinely decomposes and
you have ~15x token budget for it. See [docs/03-orchestration.md](docs/03-orchestration.md).

**Which models does it support?**
Anthropic only, deliberately: `claude-opus-4-8` (strong), `claude-sonnet-5` (mid),
`claude-haiku-4-5` (cheap). Staying single-provider keeps thinking, effort, caching,
and structured-output semantics honest instead of lowest-common-denominator.

**What does FABLE never claim?**
That it raises per-step correctness. τ-bench is the standing reminder: pass@1 above
60% collapses to pass^8 below 25% without verification. FABLE moves the second
number, never the first.

## License

Same license as the containing repository. See [LICENSE](../LICENSE).
