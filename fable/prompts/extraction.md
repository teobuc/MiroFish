---
id: fable.extraction
version: 1.0.0
model_notes: >
  The honest replacement for "ask the model for its operating manual." This
  is a working protocol prompt for an agent (or a human following along)
  performing methodology extraction from a strong model's observed
  trajectories. It extracts WORKFLOW — observable procedure — never
  introspection, because model self-reports about internal process are
  confabulated more often than not. Run it with the orchestrator loop and
  real tool logs; it is useless as a chat prompt.
source_citation: >
  Introspection unreliability: Anthropic, "Emergent introspective awareness
  in large language models" (https://www.anthropic.com/research/introspection)
  — ~20% detection of injected concepts under favorable conditions, with
  confabulated explanations common. Unfaithful chain-of-thought: Turpin et
  al., "Language Models Don't Always Say What They Think"
  (https://arxiv.org/abs/2305.04388). Imitation transfers style not
  capability: Gudibande et al., "The False Promise of Imitating Proprietary
  LLMs" (https://arxiv.org/abs/2305.15717). Scaffolding headroom the protocol
  targets: SWE-agent ACI results, 3.8%→12.5% (https://arxiv.org/abs/2405.15793).
ablation_status: unablated
when_to_use: >
  When you want to transfer a strong model's (or strong harness's) observed
  working procedure to a weaker/cheaper configuration and measure honestly
  how much transferred. Inputs required before starting: tool-call logs from
  a strong performer on 10–30 tasks in one domain, and ≥20 held-out tasks
  with automated graders. Without both, do not start — stages 5–6 are the
  product, and they need the held-out set.
knobs: >
  {{domain}} — one line naming the task domain under study (static).
  {{trajectory_dir}} — absolute path to the collected trajectory logs.
  {{holdout_manifest}} — absolute path to the held-out task list with graders.
  Arm count in stage 5 is fixed at three; do not drop the strong arm — without
  it gap_closure has no denominator and the result is unfalsifiable.
---

You are running the methodology-extraction protocol for the domain:
{{domain}}. The goal is to distill a strong performer's observable workflow
into a checklist-driven system prompt for a weaker or cheaper configuration,
and to measure — not assert — how much of the performance gap that closes.

<banned_anti_pattern>
Do not ask any model to describe its own process, strategy, or "operating
manual", and do not treat any model's self-description as data about how it
works. Models detect their own internal states at roughly a 20% rate under
favorable experimental conditions and confabulate fluent explanations the
rest of the time (Anthropic introspection research); stated reasoning
routinely diverges from the factors that actually drove behavior (Turpin et
al. 2023). A pasted "operating manual" transfers writing style, not
capability (Gudibande et al. 2023). Everything in this protocol works from
what the strong performer *did* — tool calls, arguments, orderings, outputs,
retries — never from what any model *says* it does. If a stage tempts you to
interview a model about itself, you are off-protocol.
</banned_anti_pattern>

<protocol>
Execute the seven stages in order. Each stage names its artifact; write every
artifact to disk before moving on.

Stage 1 — COLLECT.
Gather complete tool-call trajectories from the strong performer on 10 to 30
tasks in {{domain}}, from {{trajectory_dir}}. A trajectory is the full
observable record: every tool call with arguments, every result, every retry,
the final output, and the task's pass/fail. Exclude any thinking text or
self-narration from analysis — observable actions only. Artifact:
trajectories/manifest.json listing task id, outcome, and log path.

Stage 2 — SEGMENT.
Split each trajectory at decision points: places where the performer chose
among visibly available options — which tool first, when it stopped
searching, what it did immediately after an error, when it re-ran a check,
what it verified before finishing. Label each segment with the situation
(observable preconditions) and the action taken. Artifact:
segments/<task_id>.json.

Stage 3 — INDUCE.
Across segments, extract recurring situation→action regularities and write
them as workflow rules with four fields: precondition (observable), step
(concrete action), check (how the performer verified it), failure branch
(what it did when the check failed). Keep only rules supported by at least
three trajectories; record the support count and contradicting instances on
each rule. Discard anything requiring mind-reading to state — if you cannot
phrase the precondition as something a log grep could detect, drop the rule.
Artifact: workflow/rules.json.

Stage 4 — COMPILE.
Compile the surviving rules into (a) an ordered checklist system prompt in
the style of prompts/executor.md — literal, scoped, no exhortations; (b)
tool constraints (orderings, always/never pairings, verification-before-done
requirements) expressible as tool descriptions or harness gates; (c) phase
gates for any hard sequencing, enforced by the harness rather than the
prompt where possible. Artifact: compiled/system_prompt.md,
compiled/constraints.md.

Stage 5 — A/B VERIFY.
Run three arms on the ≥20 held-out tasks in {{holdout_manifest}}, identical
budgets and graders, k≥4 runs per task per arm:
  weak       — the target configuration, unmodified
  scaffolded — the target configuration plus the stage-4 compilation
  strong     — the original strong performer
Compute pass@1 and pass^k per arm and report
gap_closure = (scaffolded − weak) / (strong − weak) via fable.evals.gap_closure.
Publish the number whatever it is. A low gap_closure is not a failed
experiment — it is the honest measurement working, and it is the finding.
Artifact: eval/report.json.

Stage 6 — ABLATE.
Remove compiled items one at a time (or in small groups when the budget
requires) and re-run the scaffolded arm on the held-out set. Keep only items
whose removal degrades the metric; delete the rest from the compilation —
inert checklist lines are context cost and false confidence. Update the
prompt frontmatter ablation_status with what survived. Artifact:
eval/ablation.json and the pruned compiled/system_prompt.md.

Stage 7 — TRIAGE.
Classify every remaining scaffolded-arm failure on the held-out set as either
procedural (the workflow was not followed — fixable by compilation changes,
tool constraints, or gates) or competence (the workflow was followed and the
step was still wrong — not fixable by any prompt; needs a stronger model or
a tighter verifier-retry loop). Report the split. The competence fraction is
the honest boundary of this method for {{domain}}; write it down next to
gap_closure, not in a footnote. Artifact: eval/triage.json.
</protocol>

<reporting>
Your final report states: rule count by stage (induced → compiled →
surviving ablation), pass@1 and pass^k for all three arms, gap_closure,
the procedural/competence failure split, and the artifact paths. Every
number cites its artifact file. No qualitative claims about the scaffolded
configuration being "smarter" — the entire claim vocabulary is the metrics.
</reporting>
