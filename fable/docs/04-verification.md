# 04 — Verification: The Harness Decides When You're Done

| | |
|---|---|
| **What transfers** | Everything here. Gates, claim audits, fresh-process re-execution, rubric judges, refuters, and eval harnesses are host code. They work on any model that can call tools and emit JSON, and they are the single largest reliability lever a harness owns. |
| **What doesn't** | Per-step correctness. A gate cannot make the model's individual attempts better — it raises **pass^k** (the probability that a *gated, retried* run ends correct) while per-step **p** stays exactly what the weights give you. Verification buys reliability, not intelligence. |

The core asymmetry of 2024–2026 agents: generation capability outran
verification capability. Models produce plausible completions faster than
anyone checks them, and an agent that grades its own homework converges on
*claiming* success, not achieving it. On tau-bench, frontier models pass
individual retail-domain tasks >60% of the time at pass@1 but drop below
25% at pass^8 — the same task, eight trials, all must succeed
([Yao et al. 2024](https://arxiv.org/abs/2406.12045)). That gap is not a
knowledge gap. It is a verification gap, and it is closable from the host
side.

FABLE's answer is architectural, not exhortational: in `src/fable/loop.py`,
`stop_reason == "end_turn"` is a **claim**, not a completion. The model
cannot declare itself done. It submits a completion report; the **gate**
decides. This document covers the gate ladder, how to write checks that
can't be gamed, and how to measure whether any of it worked.

## 1. The gate ladder: cheapest check first

`Gate.run()` executes checks in a fixed order, stopping at the first hard
failure. The order is a cost gradient — each rung is roughly 10–100x more
expensive than the last, so the cheap rungs act as filters for the
expensive ones:

```text
1. Claim audit      — deterministic script, zero tokens
2. Mechanical       — commands re-run in a FRESH subprocess
3. Judged           — one structured-output rubric call, fresh context
4. Adversarial      — refuter(s) hunting for a counterexample (high-stakes only)
```

A failing run burns roughly 4x the tokens of a passing one (the failed
attempt, the diagnosis, the retry, the re-verification). Cheap early gates
are therefore a *cost* feature, not just a correctness feature: a $0 claim
audit that catches a fabricated "tests pass" saves the $0.40 judge call and
the $2 retry loop that would have followed the lie downstream.

Two rules bind every rung:

**Checks return `Evidence`, never booleans.** An `Evidence` carries the
command, exit code, output tail, and artifact path. When a check fails, that
evidence is fed back to the model *verbatim* as a user message — "check
`pytest_gate` failed: exit 1, last 40 lines: ..." — because concrete failure
output is the highest-value token stream you can put in front of a model.
"The tests failed, try again" produces flailing; the actual traceback
produces a fix.

**Retries are bounded.** `gate_max_retries` (default 3, from `FableConfig`)
caps the fail→feedback→retry cycle. Unbounded retry against a fixed check
teaches the model the letter of the check rather than the intent — given
enough attempts, an agent will find the edit that makes *this* assertion
pass while breaking everything the assertion was a proxy for. After
exhaustion: escalate once (effort bump, then tier bump, fresh context — see
[03-orchestration.md](03-orchestration.md)) if `on_exhaust="escalate"`,
otherwise return `status="failed_gate"` with the accumulated failure
evidence. A typed failure with evidence is a good outcome. A gamed pass is
the worst outcome the system can produce.

## 2. Rung 1 — the grounded-claims audit (zero tokens)

When a gate exists, the final model call requests a structured completion
report instead of prose:

```json
{
  "summary": "Added retry logic to fetch_user; all tests pass.",
  "claims": [
    {"text": "all 34 tests pass", "tool_use_ids": ["toolu_01AbC..."]},
    {"text": "created src/retry.py", "tool_use_ids": ["toolu_01XyZ..."]}
  ],
  "artifacts": ["src/retry.py", "tests/test_retry.py"]
}
```

`verify.audit_claims(report, ledger)` is a deterministic script: every
claim of the done/passing/created/fixed family must cite a `tool_use_id`
whose **Evidence Ledger** entry supports it. The ledger is the trap that
makes this work — `tools.execute()` records the hash, exit code, and raw
output path of every tool result *before* truncation and shaping
(see [01-architecture.md](01-architecture.md)). The model saw the shaped,
capped version; the auditor consults ground truth. A claim of "tests pass"
citing a tool call whose ledger entry shows exit code 1 fails the audit. A
claim citing nothing fails the audit. Cost: zero tokens, sub-millisecond.

This closes the most common agent failure we observed in the wild:
**fabricated status**. The model writes "All tests passing ✓" in its final
message when the last pytest run in the transcript exited 1 — or when no
pytest run occurred at all. Instruction-level fixes ("never claim success
without evidence") decay over long contexts. A schema field the model must
fill, checked by a script against a ledger the model cannot write to, does
not decay.

## 3. Rung 2 — mechanical checks, fresh process

```python
from fable import check, Gate

gate = Gate(checks=[
    check.command("pytest -q", expect_exit=0),
    check.command("ruff check src/", expect_exit=0),
    check.file_exists("dist/report.pdf"),
    check.schema(REPORT_SCHEMA, path="out/summary.json"),
    check.diff_scope(["src/**", "docs/**"]),
])
```

The load-bearing word is **fresh**. `check.command` re-executes the command
in a new subprocess spawned by the harness — it does not grep the
transcript for a pytest run the agent performed. The agent's transcript of
a test run is a claim: the agent may have run a subset (`pytest
tests/test_easy.py`), run it in a stale environment, run it before its last
edit, or (rarely, but observed) echoed fabricated output through a shell
tool. The gate's own subprocess, in the workspace, after all edits, is
proof. The cost is one redundant command execution; the benefit is that the
entire class of "the transcript looks green" failures becomes impossible.

`check.diff_scope(allowed_globs)` is the structural guardrail against two
distinct failures. First, **scope drift**: the task said fix `src/auth.py`
and the diff touches fourteen files. Second, **test tampering**: any hunk
touching test or CI files by the implementer role fails the check outright.
This is deliberately mechanical rather than instructional. "Do not modify
the tests" in a system prompt does not survive an agent that has spent
three retries failing a test — deleting the assertion is, locally, the
shortest path to green. A glob check that rejects the diff does survive it.
(§6 covers who *is* allowed to write tests.)

## 4. Rung 3 — judged checks: rubrics that can be graded

Some properties have no command: "the summary is faithful to the source,"
"the error messages are actionable," "the API design is consistent." These
go to a judge — a fresh-context model call that grades artifacts against a
rubric via structured output. Judges are the most abused component in
agentic systems, so FABLE constrains them heavily.

### 4.1 Writing gradeable rubrics

A rubric criterion is gradeable when two competent humans, shown the same
artifact, would give the same score. Almost every rubric failure is a
gradeability failure. The difference:

**Bad rubric (vibes — do not ship):**

```python
check.rubric([
    "The report is high quality",
    "The writing is clear and engaging",
    "Sources are used well",
    "The analysis is insightful",
])
```

Every criterion here is a synonym for "good." A judge scoring these
measures fluency and confidence — which the *generator* optimized for — so
scores cluster at 0.8–0.95 regardless of factual quality. This rubric
passes fabricated citations with flying colors.

**Good rubric (each criterion names an observable, falsifiable property):**

```python
check.rubric([
    "Every numeric claim in the report cites a source URL that appears in sources.md",
    "The report answers the specific question asked (compare X and Y on cost), "
    "not an adjacent question (describe X and Y)",
    "No claim in the summary contradicts the data table in appendix A",
    "Each of the 3 required sections (methodology, findings, limitations) is present "
    "and non-empty",
    "The limitations section names at least one concrete way the analysis could be wrong",
])
```

Heuristics for the rewrite:

- **Point at an artifact location.** "Cites a source URL that appears in
  sources.md" is checkable by inspection; "sources are used well" is not.
- **Prefer presence/absence and consistency over quality adjectives.**
  "No claim contradicts the table" beats "accurate." "Section present and
  non-empty" beats "complete."
- **Encode the task's actual contract.** The most common judged failure is
  answering an adjacent, easier question. Write the contract into the
  criterion verbatim.
- **If a criterion could be a `check.command`, make it one.** "All code
  blocks are valid Python" should be a script, not a judge opinion. Judges
  are for what only judgment can assess — and the honest list of such
  things is shorter than it first appears.

### 4.2 Structured-output grading

`check.rubric` compiles to a single `client.structured()` call: all
criteria graded in one response against a schema
(`additionalProperties: false`, no min/max constraints — scores are
validated host-side):

```python
RUBRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion": {"type": "string"},
                    "score": {"type": "number"},           # 0.0-1.0, validated by the harness
                    "justification": {"type": "string"},   # quote/line supporting the score
                },
                "required": ["criterion", "score", "justification"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}
```

One call, multi-dimension — not one call per criterion. Per-criterion calls
cost k-times more and lose cross-criterion consistency (a judge that sees
all dimensions grades each in the context of the others, which is how human
rubric grading works). The `justification` field is mandatory: a score with
a supporting quote is spot-checkable by the human audit (§8); a bare number
is not.

Scoring is **conjunctive**: every `must_pass` criterion must clear its
threshold. Never a weighted average — averages let one excellent dimension
launder one failed dimension, and "faithful but unreadable" and
"beautifully written fabrication" should both fail, not net out to 0.7.

### 4.3 Judge discipline (the anti-collusion rules)

- **Fresh context.** The judge runs in its own context window. It never
  sees the generator's conversation, plan, or rationale — only the
  artifacts and, where relevant, ledger-captured tool outputs. A judge that
  reads the generator's reasoning inherits the generator's framing and
  approves its mistakes for the generator's reasons.
- **Different prompt lineage.** The judge's system prompt
  ([prompts/verifier.md](../prompts/verifier.md)) shares no text with the
  executor's. Shared lineage produces shared blind spots.
- **Known biases, mechanical countermeasures.** LLM judges exhibit
  position bias, verbosity bias, and self-preference
  ([Zheng et al. 2023](https://arxiv.org/abs/2306.05685)). For pairwise
  comparisons: run both orders, and treat a verdict that flips with order
  as a tie. Prefer rubric-against-artifact over pairwise wherever possible
  — position bias cannot exist when there is only one artifact. Route
  judging to a different tier than generation (`DEFAULT_ROLES` puts judges
  on `mid`) to blunt self-preference.
- **Warn-only until calibrated.** An uncalibrated judge gate is theater —
  it adds latency and cost while measuring an unknown quantity.
  `check.rubric` emits an "uncalibrated" warning until a `calibration_ref`
  is set. The protocol: collect 50–200 human-labeled examples of pass/fail
  artifacts for your task; a judge must agree with the labels ≥90% before
  its check flips from warn-only to `must_pass`. This is a day of labeling
  work. It is the difference between a gate and a superstition.

## 5. Rung 4 — the adversarial refuter

For high-stakes artifacts, add the strongest check that exists short of
production: a model whose *only* job is to break the artifact.

The charter (see [prompts/verifier.md](../prompts/verifier.md)) is
asymmetric by design:

> Produce one concrete counterexample, failing input, or contradicting
> source. Do not evaluate overall quality. Do not say whether you agree.

Never ask "is this correct?" — agreement questions trigger sycophantic
approval, and a yes/no verdict is unfalsifiable. A refutation is a
*checkable object*: a failing input can be run, a contradicting source can
be read, a broken edge case can be reproduced. `verify.refute()` returns
`None` (no refutation found) or an `Evidence` describing the concrete
break, which feeds back into the retry loop like any other failure.

### 5.1 N refuters and the kill rule

One refuter samples one attack strategy. For genuinely high-stakes gates,
run N independent refuters (N=3 typical; independent = fresh contexts, and
ideally varied framings: one hunts logic errors, one hunts requirement
gaps, one hunts edge-case inputs) via `subagents.fan_out`:

- **Any *verified* refutation kills.** If any refuter produces a
  counterexample that the harness can mechanically confirm (run the failing
  input, fetch the contradicting source), the artifact fails — even if the
  other N-1 found nothing. Refutation is existential: one real bug is a
  real bug regardless of the vote.
- **Unverifiable refutations go to majority.** When a refutation cannot be
  mechanically confirmed (a claimed conceptual flaw in a design doc), fall
  back to a vote among the refuters on that specific claimed flaw — does a
  majority, shown the claim and the artifact, confirm it? This filters the
  hallucinated refutation, which is the refuter's characteristic failure
  mode.

### 5.2 Why refute-then-vote, not majority approval

The tempting default — generate once, ask 3 judges "is it good?", take
majority — fails because judge errors are **correlated**. Approval-style
judges share the same fluency bias, the same sycophancy, the same blind
spots; three of them approving a plausible fabrication is barely more
informative than one. Refuters decorrelate by construction: each pursues
its own attack path, and the *artifact* of a refutation (a failing input)
is independently checkable, so a single success is decisive without any
voting at all. Majority voting is reserved for the narrow case in §5.1 and
for self-consistency (§5.3), where the vote is over *discrete answers*, not
quality opinions.

### 5.3 Self-consistency: discrete answers only

`verify.self_consistent(prompt, k=3)` samples the same question k times in
fresh contexts and returns `(modal_answer, agreement)`. Use it **only** for
questions with discrete, comparable answers — a classification, a number, a
yes/no with fixed criteria. Agreement below 2/3 means the question is
underdetermined at this tier: escalate (more context, stronger tier, or a
human), don't average. Never apply self-consistency to open-ended
generation — three different essays have no mode, and picking the
"most representative" one is a judge call wearing a statistics costume.

## 6. Test-driven agent loops

TDD is the strongest verification pattern for coding agents because the
check is written *before* the artifact exists, by a party that hasn't seen
the implementation. FABLE's version makes the harness the referee at all
three points where agent-TDD gets gamed:

1. **The harness owns the red state.** After tests are written (by the
   agent in a test-author role, or by a human), `verify.assert_red(test_cmd,
   workspace)` runs them in a fresh subprocess and *rejects any new test
   that already passes* before implementation. A test that is born green is
   vacuous — it asserts nothing about the change. This catches the
   agent-written test that accidentally (or conveniently) tests existing
   behavior.
2. **The implementer cannot touch the tests.** The implementation phase
   runs with `check.diff_scope` excluding test and CI globs. Structural,
   not instructional (§3): the ban survives the third frustrated retry
   because it is enforced on the diff, not requested of the model.
3. **Done = harness-executed green.** The completion gate is
   `check.command("pytest -q")` in a fresh process, plus the claim audit.
   The implementer's own report of green is rung-1 input, not rung-2 proof.

The full recipe is `examples/03_coding_agent.py`. Sequence: blueprint with
per-step `verifier` fields → `assert_red` → implement under `diff_scope` →
fresh-process pytest gate → refuter.

**Mutation-testing recipe (not a shipped module).** A green suite proves
the code satisfies the tests, not that the tests constrain the code. Where
the stakes justify it: run `mutmut` (or equivalent) over the changed lines
and require ≥70% of mutants killed. Cheap fallback when a mutation tool is
overkill: **revert-and-confirm-red** — after the gate passes, revert the
implementation hunks in a scratch copy and re-run the suite; if it still
passes, the tests never tested the change, and the run fails with that
evidence. One extra subprocess, catches the fully vacuous suite.

## 7. Trajectory evals: measuring the harness itself

Everything above gates a *single run*. Two more layers evaluate the
*system* — because "we added verification" is itself a claim that needs
evidence.

### 7.1 Failure-mode detection on traces

`trace.detect_failures(reader)` runs seven deterministic detectors over a
run's JSONL trace:

| Mode | Detector signal (exact trace pattern) |
|---|---|
| Fabricated status | a `gate_check` event failing a `claim:*` audit entry — a done-claim whose cited evidence shows an error, a nonzero exit, or no ledger record |
| Premature completion | run ended `ok_unverified` after a plan gate ran: steps were planned, no completion gate ever verified them |
| Scope drift | a `diff_scope` gate failure reporting out-of-scope file touches |
| Test tampering | a `diff_scope` gate failure reporting implementer-role changes to test/CI paths |
| Verification skipping | run ended in an ok status with no `tool_result` carrying an exit code — no decisive command ran before the done-claim was accepted |
| Loop / stall | a `stall` rail trip, or the same `(tool, args_hash)` pair executed 3+ times |
| Error laundering | an `is_error:true` result never retried to success, in a run that still ended with an ok status |

Deterministic on purpose. Post-hoc *LLM* attribution of failures — "which
agent/step caused this run to fail?" — measured at 53.5% accuracy at the
agent level and 14.2% at the step level ([Zhang et al. 2024, Who&When
benchmark](https://arxiv.org/abs/2505.00212)), which is unusable for
anything downstream. FABLE's alternative is *structural* attribution: one
responsibility per subagent, evidence ids on every claim, detectors on
observable trace patterns. When you need to know what went wrong, grep the
trace, don't poll a model.

### 7.2 pass@1 vs pass^k

Report **pass^k**, k≥4, for any agent you intend to run unattended.
pass@1 answers "can it ever do this?"; pass^k answers "can I stop watching
it?" — and the tau-bench numbers at the top of this document show they
diverge by 35+ points on the same tasks. `evals.run_eval(tasks,
agent_factory, k=4)` runs each task k times with automated graders
(mechanical preferred) and reports both, plus **per-task flips** — the
task-level pass/fail vector across trials. Flips are the diagnostic
payload: a task that goes `[T, F, T, F]` is a nondeterministic failure
worth a detector or a check; a task that goes `[F, F, F, F]` is a
capability wall no amount of harness will fix. Set release floors on
pass^k, not pass@1. `examples/03_coding_agent.py` ships with `FABLE_EVAL_K`
support so these numbers are one env var away, and `evals.gap_closure` (three arms:
weak model, weak+FABLE, strong model) tells you honestly how much of the
gap the scaffolding closed — publish that number even when it is low
(see [00-philosophy.md](00-philosophy.md)).

## 8. The mandated human audit

Automated gates drift: the task distribution shifts, the model finds the
seam in a check, a judge's calibration rots. Standing policy:

- Human-review **5–10% of passing runs**, sampled randomly. You are looking
  for the pass that shouldn't have passed — every one you find is a new
  check or a tightened rubric criterion.
- Human-review **100% of retry-rescued runs** (failed the gate, then passed
  on retry or escalation) until their audited quality matches first-pass
  runs. Retry-rescued passes are where letter-of-the-check gaming
  concentrates, because the model has seen the check's failure output and
  optimized against it specifically.

Budget note: on high-stakes tasks, expect verification (gates + judges +
refuters + retries they trigger) to consume 20–50% of total tokens. This is
stated in `Gate`'s docstring and it is a purchase, not an overhead: you are
buying the pass^k delta, and [06-operations.md](06-operations.md) prices
it against the cost of shipping the failures instead.

## 9. Honesty box

Nothing in this document raises per-step correctness. The model inside the
CALL box attempts each step with exactly the probability its weights
provide. What the gate ladder changes is what happens to the *attempts*:
wrong ones get caught, evidence gets fed back, retries get bounded,
escalations get one shot, and the run's final status is grounded in
evidence a script checked rather than prose a model wrote. That moves
pass^k — often dramatically — and moves p not at all. If someone tells you
their scaffold made the model smarter, ask for their three-arm
`gap_closure` numbers.
