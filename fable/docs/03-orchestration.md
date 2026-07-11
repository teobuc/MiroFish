# 03 — Orchestration: A Catalog of Multi-Agent Patterns (and When Not to Use Them)

| | |
|---|---|
| **What transfers** | Topology. Who talks to whom, what crosses each boundary, which role runs on which model at which effort, and where the gates sit. All of it is host code and briefs — it works on any Claude tier. |
| **What doesn't** | Per-step correctness. A swarm of Haiku workers is still Haiku at every individual step. Orchestration buys *coverage, parallel wall-clock, decorrelated verification, and cost routing* — it never buys a smarter step. Anthropic's own multi-agent research system attributes ~80% of its performance variance to token spend, not architecture ([source](https://www.anthropic.com/engineering/built-multi-agent-research-system)). |

Every pattern in this catalog is a *recipe over two functions* —
`fable.subagents.spawn` and `fable.subagents.fan_out` — plus the gates from
[docs/04-verification.md](04-verification.md). There is no graph engine, no
workflow DSL, no second loop. Subagents re-enter the same state machine
described in [docs/01-architecture.md](01-architecture.md) with fresh
context, scoped tools, and a role-routed model policy. Orchestration is data.

## 0. The escalation rule (read this before any pattern)

**The cheapest sufficient pattern wins. The single agent is the default.**

Multi-agent designs are a cost and complexity escalation, not an upgrade.
Move down this table only when the row above demonstrably fails:

| Pattern class | Token multiplier vs. one chat call | When it earns its cost |
|---|---|---|
| Prompt chain / pipeline | ~1x (same tokens, split across calls) | Stages have different tools, prompts, or gates |
| Single agent with tools | ~4x | The task needs iteration and observation |
| Orchestrator + subagent swarm | ~15x | The task is genuinely parallelizable *and* exceeds one context window |

The ~4x and ~15x figures are Anthropic's measurements from their production
research system ([source](https://www.anthropic.com/engineering/built-multi-agent-research-system));
the same multipliers are stated at the point of use in the `spawn` docstring.
A pattern that does not buy back its multiplier in pass^k, wall-clock, or
routed cost is a pattern you should delete.

### Delegation heuristics (verbatim, use as written)

- **Simple fact-finding: 1 agent, 3–10 tool calls.** No subagents.
- **Comparisons across 2–4 independent sources: 2–4 subagents**, one source each.
- **10+ subagents only for genuinely decomposable research** where the
  sections share no state and the synthesis step is real work.

Opus 4.8 *under-spawns* by default — it prefers doing work in its own context.
If a task fits the second or third row, say so explicitly in the orchestrator
prompt (the calibration snippet ships in `prompts/orchestrator.md`). Do not
"fix" under-spawning by lowering the bar; most tasks belong in row one.

## 1. The two contracts every pattern relies on

### The Brief: four mandatory fields

A subagent gets a `Brief`, never a conversation. All four fields are
required by the dataclass — vague briefs are the top cause of duplicated
subagent work, and each duplicated worker costs real money at swarm
multipliers:

```python
from fable.subagents import Brief

brief = Brief(
    objective="Determine whether zep-cloud's Python SDK supports async graph queries, with version numbers.",
    output_format="Markdown digest: verdict line, supporting evidence with file/URL citations, open questions.",
    tool_guidance="Prefer grep over reading whole files. Check pyproject.toml for the pinned version first.",
    boundaries="Read-only. Do not install packages. Do not investigate the TypeScript SDK.",
    role="researcher",   # routes to mid tier / medium effort via DEFAULT_ROLES
)
```

### The digest contract

What comes back is a `SubagentReport`: a digest capped at
`config.subagent_digest_max_tokens` (default 2,000 — enforced by one
cheap-tier re-summarize on overflow), artifact *paths* for everything bulky,
a confidence score, and open questions. Full findings go to `scratch/`
files; only paths cross the boundary. The subagent's transcript goes to its
own JSONL sidechain, never into the parent's context.

**Corollary: subagents share nothing implicitly.** No parent conversation, no
sibling results, no memory of a previous spawn. If two workers need the same
fact, it goes in both briefs or in a file both briefs point at. Every
"context sharing" bug in multi-agent systems is a violation of this line.

---

## 2. Pattern catalog

Each entry: when to use, topology, wall-clock/cost analysis, a runnable
snippet against the real FABLE API, and the failure mode the pattern exists
to prevent.

---

### 2.1 Pipeline (prompt chaining with gates — no barriers)

**When to use.** The task decomposes into *sequential stages with different
contracts*: extract → transform → render; research → outline → draft. Each
stage needs a different prompt, different tools, or a different gate. This
is the workhorse pattern; reach for it before anything with `spawn` in it.

**Topology.**

```text
task ──► [stage A] ──gate──► [stage B] ──gate──► [stage C] ──gate──► done
              │                   │                   │
              ▼                   ▼                   ▼
         a.json on disk      b.py on disk        report.md
```

Stages communicate through **artifacts on disk, not transcripts**. Stage B
reads `a.json`; it never sees stage A's conversation. There are no
synchronization barriers because there is nothing to synchronize — each
stage starts the moment its input artifact passes the gate. (If you find
yourself wanting a barrier, you wanted a fan-out, §2.2, or you are
over-engineering, §4.1.)

**Wall-clock / cost.** Latency is the *sum* of stages — this is the slowest
shape per token. Tokens are ~1x a monolithic prompt (the same work, split),
and usually cheaper in practice because each stage's context contains only
its own inputs, and failed stages are retried alone instead of re-running
the whole task. A three-stage chain where stage 2 fails once costs
~1.3x; a monolithic prompt that fails once costs 2x.

**Snippet.**

```python
from fable import run, check
from fable.tools import fs_tools

# Stage A: extract. Gate: output is schema-valid, mechanically.
a = run(
    "Read spec.md and extract every API endpoint into endpoints.json "
    "as a list of {method, path, auth_required} objects.",
    tools=fs_tools(),
    verify=check.schema(ENDPOINT_SCHEMA, path="endpoints.json"),
)
assert a.status == "ok", a.status

# Stage B: generate. Gate: generated code imports and its tests pass,
# in a fresh subprocess — the transcript's word is not evidence.
b = run(
    "Generate a typed Python client in client.py covering every endpoint "
    "in endpoints.json. Write one test per endpoint in test_client.py.",
    tools=fs_tools(),
    verify=check.command("pytest test_client.py -q"),
)
assert b.status == "ok", b.status

print(f"total: ${a.cost_usd + b.cost_usd:.2f}")
```

**Failure mode prevented: error snowball.** In a monolithic prompt, a
mistake in extraction silently poisons generation, and the failure surfaces
two stages later where it is expensive to attribute. The per-stage gate
localizes the failure to the stage that caused it, while the run is still
cheap to retry. This is the same reason the trace exists: attribution should
be structural, not forensic.

---

### 2.2 Orchestrator-workers (parallel fan-out + synthesis)

**When to use.** Breadth-heavy tasks that decompose into *independent*
sections: compare N vendors, survey N papers, audit N services. Apply the
delegation heuristics from §0 literally — 2–4 workers for comparisons, 10+
only for genuinely decomposable research.

**Topology.**

```text
                 ┌──► [worker: source A] ──► digest + scratch/a.md ──┐
[orchestrator] ──┼──► [worker: source B] ──► digest + scratch/b.md ──┼──► [synthesis] ──gate──► done
  (strong)       └──► [worker: source C] ──► digest + scratch/c.md ──┘
                        (mid / medium, fresh context, scoped tools)
```

**Wall-clock / cost.** Wall-clock is `max(workers) + synthesis` instead of
`sum(workers)` — with 4 parallel researchers this is routinely a 3x latency
win. Tokens are ~15x a chat call: every worker re-pays system prompt and
tool schemas, and explores its own dead ends. Anthropic measured a 90.2%
improvement over single-agent on breadth-first research queries with
exactly this topology ([source](https://www.anthropic.com/engineering/built-multi-agent-research-system)) —
and attributes most of it to the extra tokens, not the architecture. Route
workers to the mid tier (`role="researcher"` → Sonnet at medium effort) so
the 15x lands on $3/$15 tokens, not $5/$25.

**Snippet** (direct `fan_out` — deterministic decomposition done in host code):

```python
from fable import run, check
from fable.subagents import Brief, fan_out

vendors = ["stripe", "adyen", "braintree"]
briefs = [
    Brief(
        objective=f"Document {v}'s pricing model: per-transaction fees, "
                  f"monthly minimums, volume discounts. Cite URLs.",
        output_format="Digest <= 2000 tokens; full notes to a scratch file.",
        tool_guidance="Search first, then fetch. Two sources per number.",
        boundaries=f"Only {v}. Do not compare — comparison is the parent's job.",
        role="researcher",
    )
    for v in vendors
]
reports = fan_out(briefs, max_parallel=3)

synthesis = run(
    "Write comparison.md from these digests. Every number must carry its "
    "citation from the digest; mark anything uncited as UNVERIFIED.\n\n"
    + "\n\n---\n\n".join(r.digest for r in reports),
    tools=[],
    verify=check.rubric([
        "Every vendor from the input digests appears in the comparison",
        "Every numeric claim carries a citation or an UNVERIFIED mark",
    ]),
)
print(synthesis.status, f"${synthesis.cost_usd + sum(r.cost_usd for r in reports):.2f}")
```

When the decomposition itself requires judgment, register
`spawn_subagent_tool()` on a strong-tier orchestrator instead and let the
model write the briefs — `examples/02_research_swarm.py` shows both.

**Failure mode prevented: context-window exhaustion and cross-contamination.**
Three vendors' worth of raw pages does not fit one window, and a single
agent reading vendor A's marketing copy anchors its reading of vendor B.
Isolation gives each worker a full window and an unanchored read; the digest
contract keeps the parent's window clean for synthesis.

---

### 2.3 Model cascade (strong plans, cheap executes)

**When to use.** Multi-step implementation work where most steps are
mechanically verifiable. This is FABLE's default economic topology, and the
litmus test from [docs/00-philosophy.md](00-philosophy.md) as running code:
*if you can write an automated checker for a step, a scaffolded cheap model
can probably do it; if you can only check it by being smart, you need the
smart model.*

**Topology.**

```text
[blueprint author: strong/xhigh]
        │  Blueprint: steps with per-step `verifier` field
        ▼
   ┌─ step s1  verifier: "pytest tests/test_convert.py -q" ──► route: mid tier ─┐
   │                                                            verify-and-retry │
   ├─ step s2  verifier: "ruff check src/" ─────────────────► route: mid tier ─┤──► gate ──► done
   │                                                                             │
   └─ step s3  verifier: "judgment" ─────────────────────────► route: strong ───┘
                                        (gate fail x N ──► Router.escalate: effort↑ then tier↑)
```

The routing policy is not a config file — it is the Blueprint's `verifier`
field. A step whose verifier is a shell command routes cheap, because a
wrong answer costs one retry, not a corrupted deliverable. A step whose
verifier is the literal `"judgment"` pins to the strong tier, because
nothing downstream can catch its mistakes mechanically.

**Wall-clock / cost.** Cheap-with-verifier beats one strong pass whenever
the cheaper tier's per-step pass rate clears ~40% — but cost it as an
*expectation*, not a worst case. With one cheap attempt costing `c`, a strong
attempt ≈ `5c` (the strong/cheap price ratio), up to 3 cheap tries before
escalating, and `q = 1 − p`, the expected cost is
`E = c·(1 + q + q²) + 5c·q³` — the same formula tabulated in
[docs/06-operations.md](06-operations.md) §3. At `p ≈ 0.4` that lands near
`3.04c ≈ 0.6x` one strong pass (~39% cheaper); three cheap tries *alone* cap
at `3c` worst-case, *and* every retry is gate-verified where the strong pass
is not.
Latency gains come from effort routing: mechanical steps at `low` effort
emit a fraction of the output tokens ($25/MTok on Opus makes output the
bill). Full arithmetic in [docs/06-operations.md](06-operations.md).
Escalation adds one fresh-context re-run of the failing step only.

**Snippet.**

```python
from fable import Agent, check
from fable.config import FableConfig, Router
from fable.loop import Blueprint, Step
from fable.tools import fs_tools, shell_tool

bp = Blueprint(steps=[
    Step(id="s1", action="Implement convert() in src/convert.py per spec.md",
         verifier="pytest tests/test_convert.py -q"),
    Step(id="s2", action="Wire convert() into the CLI in src/main.py",
         verifier="pytest tests/test_cli.py -q"),
    Step(id="s3", action="Rewrite README usage section for the new flag",
         verifier="judgment"),
])

router = Router(FableConfig())
for s in bp.steps:
    p = router.route_step(s.verifier)
    print(f"{s.id}: tier={p.tier} effort={p.effort}")   # s1/s2 -> mid, s3 -> strong

agent = Agent(
    tools=[*fs_tools(), shell_tool(allowlist=["pytest", "python"])],
    verify=check.command("pytest -q"),      # whole-task completion gate
)
result = agent.run("Implement the converter feature", blueprint=bp)
print(result.status, f"${result.cost_usd:.2f}", result.cache_hit_ratio)
```

Step status lives in the harness, not the model: a worker cannot mark its
own step done, and a re-planning model cannot resurrect executed steps.
Escalation is gate-failure-driven only — effort bump first, then tier bump,
via `Router.escalate` — never a learned quality estimator. FrugalGPT-style
cascade scoring ([Chen et al. 2023](https://arxiv.org/abs/2305.05176))
requires calibration data a solo developer does not have; a gate failure is
a routing signal you get for free.

**Failure mode prevented: paying frontier prices for grep-shaped work** —
and its mirror image, shipping cheap-tier judgment. The verifier field makes
the routing decision auditable per step in the trace.

---

### 2.4 Loop-until-dry discovery (checkpoint respawns)

**When to use.** Open-ended workloads with no enumerable step list: "find
every place this API is misused," "fix everything flagged by the audit,"
"keep improving coverage until the well is dry." The stop condition is
*discovered*, not planned.

**Topology.**

```text
[session 1] ──85% pressure──► Checkpoint ──► [session 2, fresh window] ──► ... ──► [session N]
     │        to memory/           boots from resume_prompt():                 dry: two consecutive
     ▼                             pwd → git log → progress.md →               sessions find nothing
 findings to disk,                 feature_list.json → pick ONE →              new -> stop
 feature_list.json grows           re-run one smoke check
```

**Wall-clock / cost.** Linear in the number of sessions; each respawn costs
one boot sequence (a few thousand tokens of filesystem rediscovery) instead
of an ever-growing context. The alternative — one giant session — degrades
quadratically: past 60% window pressure you pay cache re-write tax on every
projection edit and the model's recall of early findings decays. Fresh
sessions from files are both cheaper and more reliable than summarization
for very long runs; models rediscover state from disk well, and files don't
rot. The dry-well stop condition ("two consecutive sessions add nothing to
`feature_list.json`") belongs in host code, not the prompt.

**Snippet.**

```python
from fable import Agent, Budget, check
from fable.memory import Memory
from fable.tools import fs_tools, shell_tool

memory = Memory(".fable/memory")
agent = Agent(
    tools=[*fs_tools(), shell_tool(allowlist=["pytest", "grep"])],
    verify=check.command("pytest -q"),
    memory=memory,
)

result = agent.run(
    "Audit src/ for unhandled None returns from the zep client; fix each "
    "with a test. Record every fixed site in feature_list.json.",
    budget=Budget(max_usd=8.0, max_turns=60),
)
while result.status == "checkpointed":            # 85% pressure hit
    result = agent.resume(result.checkpoint_path)  # fresh window, boots from disk
print(result.status, result.turns, f"${result.cost_usd:.2f}")
```

**Failure mode prevented: the AutoGPT death spiral** — unbounded context
growth, lossy mid-run summarization, and re-planning of already-completed
work. The checkpoint carries only decisions, verified-done items (with
evidence ids), open issues, and the next 3 steps; everything else is
reconstructed from disk and git, which cannot hallucinate. `passes: true`
in `feature_list.json` is flipped only by the gate
(`Memory.mark_passed`), so a later session cannot be gaslit by an earlier
session's optimism.

---

### 2.5 Adversarial verification (the refuter)

**When to use.** High-stakes deliverables where mechanical checks can't see
the failure: research reports, migration plans, security-relevant diffs.
Run it *after* the mechanical gate rungs — it is the most expensive rung on
the ladder ([docs/04-verification.md](04-verification.md)).

**Topology.**

```text
[generator] ──► artifact ──► [refuter: strong/high, FRESH context]
                                  │  sees artifacts + tool outputs ONLY
                                  │  never the generator's rationale
                                  ▼
                    "one concrete counterexample or failing input"
                          │                        │
                       found ──► fed back        none ──► pass
                        verbatim, retry
```

**Wall-clock / cost.** One extra strong-tier call per gate attempt, plus the
refuter's own tool calls if it needs to execute a counterexample. Budget
20–50% of task tokens for verification on high-stakes work — stated plainly:
that is a purchase of pass^k, and it is the best-priced one available,
because a refutation caught here costs one retry instead of a shipped defect.

**Snippet.**

```python
from pathlib import Path
from fable import run, check, Evidence
from fable.tools import fs_tools
from fable.verify import refute

def no_refutation(ctx) -> Evidence:
    ev = refute(
        [Path("migration_plan.md")],
        brief="Produce one concrete input, sequence, or edge case under which "
              "this migration plan loses data or breaks rollback. One is enough.",
        client=ctx.client,
    )
    if ev is None:
        return Evidence(name="refuter", passed=True, detail="no counterexample found")
    return Evidence(name="refuter", passed=False, detail=ev.detail)

result = run(
    "Write migration_plan.md for moving the events table to partitioned storage.",
    tools=fs_tools(),
    verify=[check.file_exists("migration_plan.md"), check.callable(no_refutation, name="refuter")],
)
```

On failure, the counterexample is appended verbatim as the retry's user
message — concrete evidence, not "please try harder."

**Failure mode prevented: agreement theater.** Asking a verifier "is this
correct?" produces yes — LLM judges exhibit strong sycophancy and
self-preference biases ([Zheng et al. 2023](https://arxiv.org/abs/2306.05685)).
The refuter charter inverts the task: success is *finding* a flaw, so
sycophancy pushes toward scrutiny instead of away from it. Falsification is
also asymmetrically checkable — a claimed counterexample can itself be
executed and verified mechanically.

---

### 2.6 Perspective-diverse verify

**When to use.** When one refuter isn't enough because the failure surface
has *orthogonal axes*: a diff can be simultaneously correct, insecure, and
out of scope. Instead of one verifier with a longer prompt, run several
refuters with disjoint charters.

**Topology.**

```text
              ┌──► [refuter: correctness]   "one input where behavior is wrong"      ─┐
artifact ─────┼──► [refuter: security]      "one exploitable input or leaked secret"  ─┼──► conjunctive
              └──► [refuter: requirements]  "one spec'd requirement not met"          ─┘    (any hit fails)
                    (each: fresh context, different brief, may be different tier)
```

Decorrelation here is **structural** — different briefs, different evidence
foci, optionally different model tiers — not statistical. Three identical
judges sampled three times share every blind spot and fail together; three
charters fail independently.

**Wall-clock / cost.** The refuters are independent, so run them through
`fan_out`: wall-clock is one refuter, cost is k refuters. Route by stakes —
correctness on strong, requirements-coverage on mid. Verdicts are
conjunctive must-pass; there is no averaging, because a 9/10 on correctness
does not offset an exploit.

**Snippet.**

```python
from fable.subagents import Brief, fan_out

CHARTERS = {
    "correctness":  "Produce one concrete input where the diff'd code returns a wrong result. Report NONE if you cannot.",
    "security":     "Produce one exploitable input, injection path, or secret exposure introduced by this diff. Report NONE if you cannot.",
    "requirements": "Name one requirement from spec.md this diff was supposed to satisfy but does not. Report NONE if you cannot.",
}
reports = fan_out(
    [Brief(objective=charter,
           output_format="Either 'NONE' or the counterexample with reproduction steps.",
           tool_guidance="Read the diff and spec from disk; execute candidate counterexamples.",
           boundaries=f"Only {axis} findings. Do not comment on style. Report every "
                      f"finding with confidence and severity; a downstream filter ranks.",
           role="refuter")
     for axis, charter in CHARTERS.items()],
    max_parallel=3,
)
hits = [r for r in reports if "NONE" not in r.digest.splitlines()[0]]
passed = not hits   # conjunctive: any refutation fails the artifact
```

Note the coverage-vs-filtering split in the boundaries: the verifier reports
*everything* with confidence and severity; deciding what blocks the gate is
the harness's job. A verifier told to self-filter learns to under-report.

**Failure mode prevented: correlated blind spots.** One verifier prompt
covering three concerns attends to whichever concern the artifact makes
salient and skims the rest. Disjoint charters make each axis some agent's
*entire* job — the same one-responsibility principle that makes failure
attribution structural (§3).

---

### 2.7 Judge panel (use sparingly; refute-then-vote instead)

**When to use.** Rarely, and only for **discrete answers** — classifications,
YES/NO gates, A-vs-B picks — where agreement between samples is measurable.
FABLE deliberately does not ship a judge-panel default: panels of similar
judges make correlated errors, and majority vote over correlated errors is
noise amplification with a quorum's confidence. For free-form artifact
grading, use one calibrated rubric judge ([docs/04-verification.md](04-verification.md))
plus a refuter (§2.5), not a panel.

**Topology.**

```text
                 ┌──► [judge sample 1] ─► "YES" ─┐
discrete Q ──────┼──► [judge sample 2] ─► "YES" ─┼──► modal answer + agreement
                 └──► [judge sample 3] ─► "NO"  ─┘        agreement < 2/3 ──► escalate,
                                                          never tie-break by re-vote
refute-then-vote (preferred for stakes):
   [refuter] ──counterexample?──► found: verdict is NO, evidence attached
                                  none:  then vote on the residual question
```

**Wall-clock / cost.** k cheap parallel calls (k=3 default; judges run
mid-tier at 16K max_tokens) — wall-clock of one call, cost of k. The
escalation path (low agreement → strong tier) costs one more call and fires
only on genuinely contested cases, which is exactly where you want the
strong model. Self-consistency's gains are real for discrete answers
([Wang et al. 2022](https://arxiv.org/abs/2203.11171)) and undefined for
free-form text, where "modal answer" doesn't exist.

**Snippet.**

```python
from fable.client import FableClient
from fable.config import FableConfig, Router
from fable.verify import self_consistent

client = FableClient(FableConfig())
answer, agreement = self_consistent(
    "Given the diff in scratch/final.diff and the schema in db/schema.sql: "
    "is this migration backward compatible with v2 clients? Answer YES or NO, "
    "then one sentence of grounds.",
    k=3, client=client, role="judge",
)
if agreement < 2 / 3:
    # Contested — escalate the QUESTION to the strong tier; do not re-vote
    # the same panel until it coughs up a majority.
    policy = Router(FableConfig()).escalate(Router(FableConfig()).policy("judge"))
    ...
```

**Failure mode prevented: single-sample judge flakiness on discrete calls**
— and, in the refute-then-vote ordering, the worse failure of three
polite judges outvoting one concrete counterexample. Evidence beats quorum:
a found counterexample ends the vote.

---

### 2.8 Completeness critic

**When to use.** Deliverables whose failure mode is *omission* rather than
error: survey docs, test plans, migration checklists, "handle every call
site" refactors. Generators satisfice — they stop at the first coherent
draft. Mechanical gates catch what's wrong; nothing above this pattern
catches what's *missing*.

**Topology.**

```text
requirements ──► [enumerator]* ──► claimed-coverage list (or reuse spec/feature_list.json)
                                          │
artifact ──────────────────────► [critic: fresh context]
                                   "for each required item: COVERED (cite location)
                                    or MISSING — report ALL, do not self-filter"
                                          │
                                   missing list ──► fed back verbatim ──► generator retry
                                   (* enumerator optional if a checklist already exists)
```

The critic's charter is coverage, not quality — it never says "section 3 is
weak," only "requirement R7 has no corresponding section." Where the
required set is enumerable in host code (files touched, endpoints, call
sites), prefer a zero-token mechanical diff of the two lists and demote the
critic to judging only the fuzzy remainder.

**Wall-clock / cost.** One mid-tier call per gate attempt (the critic reads
the artifact plus a checklist; it doesn't explore). This is the cheapest
judged check in the catalog, and it composes: run it *before* the refuter,
since refuting an incomplete artifact wastes strong-tier tokens on a draft
that's going back anyway.

**Snippet.**

```python
from fable import run, check
from fable.tools import fs_tools

result = run(
    "Write test_plan.md covering every endpoint listed in endpoints.json.",
    tools=fs_tools(),
    verify=check.rubric(
        [
            "Every endpoint in endpoints.json appears in test_plan.md with at "
            "least one happy-path and one failure-path case",
            "Every auth_required endpoint has an unauthorized-access case",
            "No endpoint is marked 'TODO' or deferred without a stated reason",
        ],
        role="judge",
    ),
)
# Gate failure returns the specific missing items as evidence; the retry
# prompt contains "MISSING: DELETE /users/{id} — no failure-path case", not
# "be more thorough".
```

**Failure mode prevented: premature completion** — one of the seven detector
modes in `trace.detect_failures`, and the single most common multi-agent
defect in practice: the generator declares done at 80% coverage, and every
downstream verifier dutifully confirms that the 80% present is correct.
Completeness has to be somebody's entire job.

---

## 3. Structural attribution: one responsibility per subagent

When a multi-agent run fails, you need to know *which* agent failed. The
tempting fix — hand the transcript to an LLM and ask — does not work:
on the Who&When benchmark, the best automated judges identify the failing
agent 53.5% of the time and the failing step 14.2% of the time
([Zhang et al. 2025](https://arxiv.org/abs/2505.00212)). FABLE bans post-hoc
LLM attribution outright and buys attribution structurally instead:

- **One responsibility per subagent.** If a worker's brief contains "and,"
  consider splitting it. A single-purpose agent's failure is its own.
- **Digests cross boundaries, transcripts don't.** Each sidechain JSONL is a
  self-contained record; `TraceReader.cost_by_role()` and the evidence
  ledger tell you which role burned what and which claims lacked grounding.
- **Gates between stages.** A gate failure timestamps the defect to the
  stage that produced it (§2.1), turning attribution into a lookup.

## 4. Phase-scoped tools: the 15-line "graph engine"

The most requested orchestration feature is a workflow graph. FABLE's
position: a graph whose edges are gate results is a `for` loop, and tool
*scoping per phase* is a stronger guardrail than any DSL edge, because a
phase that cannot write files cannot corrupt state no matter what the model
decides. The entire recipe:

```python
from fable import run, check
from fable.tools import fs_tools, shell_tool

READ_ONLY = [t for t in fs_tools() if t.name in ("read_file", "glob", "grep_search")]

PHASES = [
    ("explore",   READ_ONLY,
     check.file_exists(".fable/scratch/findings.md")),
    ("implement", [*fs_tools(), shell_tool(allowlist=["pytest"])],
     check.command("pytest -q")),
    ("document",  fs_tools(),
     check.rubric(["CHANGELOG.md entry describes user-visible behavior, not internals"])),
]

for name, tools, gate in PHASES:
    r = run(PROMPTS[name], tools=tools, verify=gate)
    print(f"{name}: {r.status} ${r.cost_usd:.2f}")
    if r.status != "ok":
        break   # your edge logic goes here; it is Python, so write Python
```

Branching, retries with modified prompts, human-in-the-loop pauses — all of
it is ordinary Python around this loop. If your edge logic outgrows a page,
the problem is the decomposition, not the absence of a framework.

## 5. Cascade and swarm failure modes (and the telemetry that catches them)

Every pattern above adds a component that can itself fail. The recurring modes:

1. **The verifier is the weak link.** A cascade routes on gate results; a
   gate that passes garbage silently converts your cheap tier into your
   quality ceiling. Mechanical verifiers in fresh subprocesses wherever
   possible; judged verifiers are warn-only until calibrated
   ([docs/04-verification.md](04-verification.md)).
2. **Distribution shift.** The routing rule was tuned on last month's tasks.
   Watch the **escalation rate** per step type in the trace: an
   escalation rate drifting above ~30% means the cheap tier is no longer
   pulling its weight on that step class — pin it to mid/strong and stop
   paying for doomed first attempts. Near-zero escalation on `judgment`
   steps is the opposite smell: your judge may be rubber-stamping.
3. **Escalation latency.** Every escalation is a failed attempt plus a
   fresh-context re-run — fine at 10% of steps, catastrophic at 50%. The
   break-even arithmetic is in [docs/06-operations.md](06-operations.md).
4. **Digest lossiness.** A subagent that buries its key finding in a scratch
   file the parent never opens has technically complied with the contract.
   Briefs must name what belongs in the digest ("verdict line first").
5. **Silent scope creep across workers.** Two workers with overlapping
   boundaries both "helpfully" fix the same file. Boundaries in the Brief
   are load-bearing; `check.diff_scope` makes them mechanical.

`TraceReader` makes every one of these measurable from the JSONL:
`cost_by_role()` for runaway workers, `cache_hit_series()` for prefix
mutations in spawned loops, escalation events for routing drift. Routing
decisions should follow this telemetry, not intuition.

## 6. Anti-patterns

**Barriers where pipelines suffice.** If stage B consumes only stage A's
artifact, a global "wait for all agents, then proceed" barrier adds latency
and a synchronization bug surface for zero benefit. Barriers are only
meaningful before a genuine fan-in (synthesis over N digests). A pipeline
with one worker per stage needs no coordination at all — the gate *is* the
coordination.

**Subagents for single-file reads.** `spawn` costs ~15x tokens and a full
boot (system prompt, tool schemas, exploration). Spawning a "reader agent"
to fetch one file spends dollars to avoid a `read_file` call that costs
tenths of a cent — and the parent still pays to ingest the digest. The
delegation floor is real work: 3–10 tool calls of it. Below that, use the
tool directly in the parent's own loop.

**Context-sharing assumptions.** The parent's conversation does not exist
for the subagent. Sibling results do not exist for the subagent. "As
discussed above" in a brief refers to nothing. Symptoms: workers
re-deriving the parent's constraints, two workers researching the same
question, a synthesis step confused by digests that answer subtly different
questions. Fix: complete, self-contained briefs — all four fields — and
shared facts passed by file path.

**Majority vote over free-form output.** Three drafts of a report have no
modal answer. Voting machinery applies to discrete answers only (§2.7);
for artifacts, use one calibrated rubric judge plus a refuter.

**Panels of clones.** k samples of the same judge prompt at the same tier
share every bias and blind spot; their agreement measures the prompt, not
the truth. Decorrelate structurally (different charters, §2.6) or don't
bother.

**Post-hoc LLM attribution.** 53.5% agent-level / 14.2% step-level accuracy
(§3) is worse than useless — it's confidently wrong. Attribution is bought
at design time with one-responsibility briefs and per-stage gates.

**A second engine.** Any orchestration layer that maintains its own agent
state machine alongside `loop.py` will disagree with it within a week. If a
pattern in this catalog can't express your topology, write host Python
around `spawn`/`fan_out` (§4) — the loop stays the only engine.

---

*Next: [docs/04-verification.md](04-verification.md) — the gate ladder these
patterns keep pointing at. Previous: [docs/02-prompting.md](02-prompting.md) —
the briefs and role prompts the patterns are built from.*
