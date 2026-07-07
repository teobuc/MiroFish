# 06 — Operations: Cost, Latency, and the Ledger That Keeps You Honest

| | |
|---|---|
| **What transfers** | All of it. Effort routing, model cascades, cache economics, budget rails, and trace-based observability are host-side engineering — they work identically on any Claude model and typically move the cost line by 5–10x at equal-or-better pass^k. |
| **What doesn't** | Quality per call. Routing a step to `claude-haiku-4-5` does not make Haiku smarter, and no budget dial raises per-step correctness p. Cascades are safe exactly where a verifier can catch the cheap tier's mistakes; where only judgment can check the work, you pay for the strong tier or you ship errors (see the litmus test in [00-philosophy.md](00-philosophy.md)). |

An autonomous agent is a loop that re-sends its entire history to a
$5-per-million-token endpoint dozens of times per task. Unmanaged, that is
the single most expensive way to use a language model ever devised. Managed —
frozen prefix, tier routing, effort calibration, hard rails — the same loop
costs an order of magnitude less and fails in typed, visible ways. This
document is the arithmetic.

Prices used throughout (per MTok, from `fable.config.TIERS` — these are the
API facts, not estimates):

| tier | model id | input | output | cache write (1.25x) | cache read (0.1x) | window |
|---|---|---|---|---|---|---|
| strong | `claude-opus-4-8` | $5.00 | $25.00 | $6.25 | $0.50 | 1M |
| mid | `claude-sonnet-5` | $3.00 | $15.00 | $3.75 | $0.30 | 1M |
| cheap | `claude-haiku-4-5` | $1.00 | $5.00 | $1.25 | $0.10 | 200K |

## 1. The worked cost model: 1,000 agent-hours a month

Assumptions, stated so you can re-derive every number: 1,000 agent-hours per
month; 45 model calls per agent-hour; average live context 115K tokens per
call (histories grow — this is mid-run average, not peak); average output 7K
tokens per call on an uncalibrated strong-tier agent (xhigh everywhere,
narration on).

**Rung 0 — naive: all-Opus, no caching, no routing.**

```
input   45 × 115K × $5.00/M  = $25.88 / agent-hour
output  45 ×   7K × $25.00/M = $ 7.88 / agent-hour
total                        ≈ $33.75 / agent-hour  →  ≈ $34K / month
```

**Rung 1 — cache discipline (docs/05): ≈ 5.3x off the input line.**
Assume a byte-stable prefix and a steady loop settle the ledger near 91%
cache reads, 5.5% fresh input, 3.5% cache writes — an illustrative
steady-state mix like the 45 calls/hour above, not a measured benchmark;
read your own `cache_hit_ratio` before trusting it:

```
blended input price factor = 0.91×0.1 + 0.055×1.0 + 0.035×1.25 ≈ 0.189  (≈ 5.3x cut)
input   $25.88 → $4.88 / agent-hour
total   ≈ $12.8 / agent-hour  →  ≈ $12.8K / month
```

This is why `PrefixMutationError` exists and why the ledger warns below an
80% hit ratio: one timestamp interpolated into the system prompt silently
puts you back on Rung 0.

**Rung 2 — cascade routing: ≈ 2.5x off what remains.** Route by the litmus
test (a step with a command verifier goes to mid/cheap under verify-and-retry;
judgment steps stay on strong). Two effects compound: the blended per-token
price drops (an illustrative settled mix of 25% strong / 45% mid / 30% cheap
calls gives a ≈0.58 price factor), and the cheap-role calls carry far smaller
contexts (scoped subagents, 16K `max_tokens`, thinking off), which the
per-call arithmetic above hides. Net of an assumed ~15% retry overhead,
the model pencils routed workloads in at 2–3x; call it 2.5x — again an
illustrative assumption of this worked model, not ledger data:

```
total   ≈ $12.8K → ≈ $5.1K / month
```

Your mix will differ. That is the point of `ledger.by_role()` — measure your
own mix, don't trust this table.

**Rung 3 — effort calibration: the output-token dial.** Output is now the
dominant line ($25/MTok on strong). Mechanical roles at `effort="low"` with
thinking off emit 5–10x fewer output tokens than xhigh on the same step, and
they were routed to cheap output prices anyway:

```
total   ≈ $3.5–5K / month  (modeled, under all the assumptions above)
```

The design intent is equal-or-better pass^k than Rung 0 — Rung 0 had no
gate, so its failures shipped, while Rungs 1–3 fund verification out of the
savings (section 4). That is an empirical claim about *your* workload, not
a measured result of this table: check it with `evals.run_eval` (pass@1 /
pass^k per rung) before believing it.

## 2. Effort: the primary output-token dial

`output_config={"effort": ...}` is the main intelligence/cost lever on the
Claude API — it scales how much reasoning and output the model produces per
call. FABLE sets it per role in `config.DEFAULT_ROLES`; every tunable flows
from `FableConfig`.

| effort | use for | behavior and cost profile |
|---|---|---|
| `low` | mechanical roles: extraction, formatting, digest compression | Respects scope strictly, minimal exploration; smallest and fastest output. Pair with `thinking=False`. |
| `medium` | research digests, routine tool use, judges | Balanced; the default for the `researcher` and `judge` roles. |
| `high` | API default; standard interactive work | More willing to explore; noticeably more output tokens. |
| `xhigh` | coding and agentic work — the recommended default for `orchestrator`/`executor` | Deep multi-step reasoning; the most output tokens short of `max`. When Opus 4.8 reasons *shallowly*, raise effort — do not prompt-hack around it ([02-prompting.md](02-prompting.md)). |
| `max` | last rung of gate-failure escalation | Reserved by `Router.escalate`; never a resting default. |

Two rules of thumb. First, effort moves output tokens, and output tokens on
the strong tier cost 5x input — an effort miscalibration on a chatty role is
usually the largest single line in a surprised bill. Second, effort is also
the escalation currency: `Router.escalate` climbs effort one notch before it
climbs tier, because an effort bump is cheaper than a tier bump and often
sufficient.

## 3. Router economics: when cheap-plus-verifier beats strong

The cascade question is never "is Haiku as good as Opus" (it is not); it is
"is Haiku *plus a verifier that catches its mistakes, plus retries* cheaper
than Opus, at equal delivered quality". With a mechanical verifier the answer
is arithmetic. Let one cheap attempt cost `c` and one strong attempt cost
`s ≈ 5c` (the strong/cheap price ratio), with up to 3 cheap attempts before
escalating to strong, and per-attempt cheap pass rate `p`:

```
E[cost] = c × (1 + q + q²) + s × q³        where q = 1 − p
```

| p (cheap pass rate) | expected cost | vs. one strong pass (5c) |
|---|---|---|
| 0.2 | 5.00c | break-even |
| 0.4 | 3.04c | **39% cheaper** |
| 0.6 | 1.88c | 62% cheaper |
| 0.8 | 1.28c | 74% cheaper |

Verifier overhead and escalation latency eat into the margin at the low end,
so the honest guidance: the cascade wins comfortably when the cheap tier
passes more than ~40% of the time, is roughly a wash by ~20%, and below that
you are paying for latency and retries to end up on Opus anyway — pin the
step to strong. Note what the table does *not* say: nothing about the cheap
model getting smarter. Verify-and-retry raises pass^k over attempts; p per
attempt is fixed by the weights.

Two operational rules make this data-driven instead of vibes-driven:

- **Escalation-rate telemetry.** Every `Router.escalate` emits an
  `escalation` TraceEvent. A step-type whose escalation rate drifts above ~30%
  should be re-pinned to the strong tier in the Blueprint (`verifier:
  judgment`) — you are paying cheap-tier latency as a pure tax.
- **No learned quality estimator.** FABLE escalates on *gate failure only*.
  FrugalGPT-style answer-quality scorers are the documented weak link of
  cascades and un-calibratable by a solo dev; a failed check is a routing
  signal you can trust.

## 4. The verification budget

On high-stakes tasks, expect **20–50% of total tokens to go to verification**
— fresh-process re-runs are nearly free, but judges, refuters, and gate-retry
turns are not. Say the number out loud in planning: it is not overhead, it is
the purchase price of pass^k. The ladder keeps it cheap-first
([04-verification.md](04-verification.md)): the claim audit costs zero
tokens, mechanical checks cost subprocess time, and only work that survives
those rungs reaches a mid-tier judge or a strong-tier refuter. An
un-verified run is cheaper per run and more expensive per *delivered correct
result* — which is the only denominator that matters.

## 5. The token snowball

A failing run does not cost one run — it burns roughly **4x**: the failed
attempt, the retry turns carrying the failure evidence, the re-verification,
and the longer context every subsequent call now drags. This is why early
cheap gates are a cost feature, not a quality nicety: `audit_claims` (zero
tokens) killing an ungrounded completion claim on turn 12 is 4x cheaper than
a refuter discovering the same lie on turn 40. The same logic prices the
stall rail — an agent repeating `(tool, args_hash)` pairs is converting
budget into heat, and the rail exists so the ledger stops it before the
budget does.

## 6. Observability: the trace is the product surface

Every run writes an append-only JSONL of `TraceEvent`s
(`fable/trace.py`) — one event per loop action, host-side, zero context
cost:

```
event ∈ { run_start, model_turn, tool_call, tool_result, gate_check,
          rail_trip, pressure, checkpoint, escalation, run_end }
detail: tool, args_hash, result_hash, exit_code, stop_reason,
        tokens{input, output, cache_read, cache_creation}, cost_usd
```

(Subagent runs write their own JSONL — each `spawn` produces a separate
trace at `SubagentReport.trace_path` rather than a `subagent` event in the
parent's file.)

`TraceReader` turns the file into the three reports operations actually
needs, plus the deterministic failure detectors:

```python
from fable.trace import TraceReader, detect_failures

reader = TraceReader(result.trace_path)
reader.cost_by_role()        # where the money went: {"orchestrator": 1.84, ...}
reader.cache_hit_series()    # per-call hit ratio; a dip marks the prefix mutation
reader.evidence_for_claim(tool_use_id)   # claim audit, interactively
detect_failures(reader)      # seven failure-mode detectors, no LLM attribution
```

The trace lines use the same field names as MiroFish's ReportLogger JSONL
(`timestamp`, `elapsed_seconds`, `action`, `details`), so their existing log
viewer is expected to read FABLE runs — a design goal to verify against one
real trace, not yet a tested guarantee. Clearing and compaction insert marker
events; the log itself is never rewritten — resume, fork, and audit all read
the same file. (A live dashboard hooks `run(on_event=...)` rather than
polling: every event is pushed as it is written.)

The ledger is the other half: `UsageLedger` records all four usage fields on
every call (`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`), prices them from the tier table, and computes
the rolling cache-hit ratio. **Below 0.80, something in your prefix is
mutating** — that is the canonical symptom, and the fix is in
[05-memory-context.md](05-memory-context.md).

## 7. Budget rails, task budgets, and rate limits

**Host-side rails first.** `Budget(max_usd, max_turns, max_wall_seconds)` is
enforced by the harness at zero context cost, and the USD rail trips
*before* spending: `ledger.project_next_call_usd()` prices the next call from
measured context size and the observed hit ratio, so the run ends at
`budget_exceeded` with money still in the account, not after the overdraft.

**Model-visible budgets (beta, opt-in).** The task-budgets beta shows the
model a countdown so it can allocate its own remaining effort — useful for
long single calls that should degrade gracefully instead of truncating:

```python
import anthropic

client = anthropic.Anthropic()
with client.beta.messages.stream(
    model="claude-opus-4-8",
    max_tokens=64_000,
    betas=["task-budgets-2026-03-13"],
    thinking={"type": "adaptive"},
    output_config={
        "effort": "xhigh",
        "task_budget": {"type": "tokens", "total": 150_000},  # min 20_000
    },
    messages=[{"role": "user", "content": task}],
) as stream:
    message = stream.get_final_message()
```

In FABLE this sits behind `FableConfig(use_task_budget=True)`, like every
beta: the core loop runs on the GA surface, and the host-side rails remain
authoritative — a model-visible countdown is a courtesy to the model, not a
control.

**Rate limits.** Two facts shape posture. First, **cache reads do not count
against input-token rate limits** — at a 90% hit ratio, caching multiplies
effective throughput under the same limit by roughly the same 5x it cuts
cost, which is why cache discipline is a throughput strategy, not just a
billing one. Second, on 429/5xx the SDK auto-retries twice and `FableClient`
adds one layer of jittered backoff above it (`RateLimitError` →
`APIStatusError` → `APIConnectionError`, most-specific first) — the single
retry implementation in the codebase. `fan_out(max_parallel=4)` is the
matching default: each live subagent holds a streaming connection and a slice
of your limit; raise it only after the ledger says you have headroom.

## 8. Case study: adopting FABLE inside MiroFish

MiroFish (the host repository) is a working multi-agent product with the
classic pre-harness pathologies: four hand-rolled retry implementations, a
three-tier regex parser for tool calls, and a response normalizer whose
`<think>`-strip fix never reached two of the raw client call sites. Adoption
is three shims, not a rewrite:

1. **`LLMClient` facade → `FableClient`.** Keep MiroFish's `chat()` /
   `chat_json()` signatures as a thin facade over `FableClient.call()` /
   `structured()`. The normalizer then lives in exactly one place — the fix
   that never propagated structurally cannot fail to propagate again — and
   every call inherits the ledger, the retry ladder, and refusal handling.
2. **`_generate_section_react` + `_parse_tool_calls` + `_execute_tool` →
   `Agent` + `@tool`.** Wrap the existing zep tools with the `@tool`
   decorator and let native `tool_use` blocks replace prompt-format parsing.
   The three-tier regex parser is deleted, not migrated: with
   `strict: true` tool definitions the API guarantees schema-valid input,
   and `execute()` json-parses it — string-matching tool calls is a bug
   class, not a compatibility layer.
3. **The JSON-mode retry paths → `client.structured()`.**
   `_call_llm_with_retry` in `simulation_config_generator.py` and the
   fence-stripping `chat_json` parse in `utils/llm_client.py` — JSON-mode
   prompting plus regex extraction plus bespoke retries — collapse into the
   one repair ladder (native `json_schema` output → `json.loads` → single
   re-ask carrying the validation error).

Their JSONL log viewer is expected to keep working throughout — the trace
shares ReportLogger's field names (section 6); verify against one real trace
before depending on it.

## 9. The closing honesty clause

Everything in this document changes **cost** (5–10x, measured by the ledger),
**reliability** (pass^k, purchased with verification tokens), and **horizon**
(checkpoint/resume and pressure ladders let runs outlive a context window).
None of it changes per-step intelligence. A cascade routes around the cheap
model's limits; it does not remove them. A budget rail bounds a failure; it
does not prevent it. A trace explains a run; it does not improve it. If a
number in your dashboard seems to say otherwise, the dashboard is measuring
the harness — which was the claim all along, and the reason FABLE ships
`evals.gap_closure` so you can check it instead of believing it.
