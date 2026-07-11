"""FABLE evals: pass@1 / pass^k and the three-arm gap_closure honesty metric.

Two questions this module answers with numbers instead of anecdotes:

1. **Can I stop watching it?** ``run_eval`` runs each task k times through a
   fresh agent and reports pass@1 (can it ever do this?) alongside pass^k
   (does it do this every time?). On tau-bench the two diverge by 35+ points
   on the same tasks -- report pass^k, k >= 4, for anything unattended
   (docs/04-verification.md section 7.2).
2. **Did the scaffolding actually close the gap?** ``gap_closure`` runs three
   arms -- bare weak model, weak model + FABLE, strong model -- and reports
   what fraction of the weak-to-strong gap the harness closed
   (docs/00-philosophy.md section 7). Publish the number even when it is
   low: a measured boundary is the product working.

Grading is mechanical by contract: each :class:`EvalTask` carries a grader
check (prefer ``check.command``) that the harness executes in a fresh
process. Model-judged graders inherit every judge bias in docs/04 section
4.3 -- calibrate before trusting them here.

Honesty note: nothing here improves the agent. Evals *measure* the harness;
the harness moves pass^k; per-step correctness p never moves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from fable.client import FableClient, UsageLedger
from fable.config import FableConfig
from fable.loop import Agent, RunResult
from fable.verify import Check, Evidence, GateContext

__all__ = ["EvalReport", "EvalTask", "GapClosureReport", "gap_closure", "run_eval"]


@dataclass(frozen=True)
class EvalTask:
    """One task in an eval suite.

    ``grader`` decides pass/fail for each attempt and is executed by the
    HARNESS after the run, in a fresh process where applicable -- the agent's
    own exit status is not the grade, because a gamed gate would grade
    itself. Use a mechanical check (``check.command``, ``check.file_exists``,
    ``check.schema``) wherever one exists.
    """

    id: str
    task: str
    grader: Check | Callable[[GateContext], Evidence]

    def grade(self, workspace: Path, config: FableConfig) -> Evidence:
        """Run the grader against ``workspace``. Never raises: a grader crash
        is a failed attempt with the exception recorded as evidence."""
        ctx = GateContext(
            workspace=workspace,
            ledger=UsageLedger(),
            client=FableClient(config),
            final_report=None,
            blueprint=None,
        )
        runner = self.grader.run if isinstance(self.grader, Check) else self.grader
        try:
            return runner(ctx)
        except Exception as exc:  # noqa: BLE001 -- grade the crash, don't die on it
            return Evidence(
                name=f"grader:{self.id}", passed=False,
                detail=f"grader raised {type(exc).__name__}: {exc}",
            )


@dataclass(frozen=True)
class EvalReport:
    """The measured result of one eval arm.

    - ``pass_at_1``: mean per-attempt pass rate across all tasks and trials
      ("can it ever do this?").
    - ``pass_pow_k``: fraction of tasks where ALL k attempts passed ("can I
      stop watching it?"). Set release floors on this one.
    - ``per_task_flips``: task id -> pass/fail vector across trials. The
      diagnostic payload: ``[T, F, T, F]`` is a nondeterministic failure
      worth a detector; ``[F, F, F, F]`` is a capability wall no harness
      will fix.
    - ``total_cost_usd``: measured spend across every attempt, because an
      eval that hides its cost is an eval you will stop running.
    """

    k: int
    pass_at_1: float
    pass_pow_k: float
    per_task_flips: Mapping[str, tuple[bool, ...]]
    total_cost_usd: float
    statuses: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def run_eval(
    tasks: Sequence[EvalTask],
    agent_factory: Callable[[], Agent],
    k: int = 4,
    *,
    config: FableConfig | None = None,
) -> EvalReport:
    """Run each task ``k`` times through a fresh agent; grade mechanically.

    ``agent_factory`` is called once per attempt and must return a FRESH
    :class:`Agent` in a pristine workspace -- pass^k over the same task
    measures the harness, so every attempt must start equal (see
    ``examples/03_coding_agent.py`` for a factory that rebuilds the
    workspace). Attempts that raise count as failures with zero recorded
    cost; they never abort the suite.
    """
    if k < 1:
        raise ValueError(f"run_eval requires k >= 1, got {k}")
    if not tasks:
        raise ValueError("run_eval requires at least one EvalTask")
    config = config or FableConfig()

    flips: dict[str, tuple[bool, ...]] = {}
    statuses: dict[str, tuple[str, ...]] = {}
    total_cost = 0.0

    for eval_task in tasks:
        attempt_passes: list[bool] = []
        attempt_statuses: list[str] = []
        for _ in range(k):
            try:
                agent = agent_factory()
                result: RunResult = agent.run(eval_task.task)
                total_cost += result.cost_usd
                attempt_statuses.append(result.status)
            except Exception as exc:  # noqa: BLE001 -- one crash != no data
                attempt_passes.append(False)
                attempt_statuses.append(f"crashed:{type(exc).__name__}")
                continue
            evidence = eval_task.grade(Path.cwd(), config)
            attempt_passes.append(bool(evidence.passed))
        flips[eval_task.id] = tuple(attempt_passes)
        statuses[eval_task.id] = tuple(attempt_statuses)

    all_attempts = [p for vector in flips.values() for p in vector]
    pass_at_1 = sum(all_attempts) / len(all_attempts)
    pass_pow_k = sum(1 for vector in flips.values() if all(vector)) / len(flips)
    return EvalReport(
        k=k,
        pass_at_1=round(pass_at_1, 4),
        pass_pow_k=round(pass_pow_k, 4),
        per_task_flips=flips,
        total_cost_usd=round(total_cost, 4),
        statuses=statuses,
    )


@dataclass(frozen=True)
class GapClosureReport:
    """Three measured arms and the one honest ratio between them.

    ``gap_closure = (scaffolded_weak - bare_weak) / (strong - bare_weak)``,
    computed on pass^k. 1.0 means the harness closed the whole weak-to-strong
    gap on these tasks; 0.0 means it closed none of it; ``None`` means the
    weak and strong arms tied, so the ratio has no denominator and the
    comparison is uninformative -- say that, rather than fabricating a
    number.
    """

    bare_weak: EvalReport
    scaffolded_weak: EvalReport
    strong: EvalReport
    gap_closure: float | None


def gap_closure(
    tasks: Sequence[EvalTask],
    *,
    bare_weak_factory: Callable[[], Agent],
    scaffolded_weak_factory: Callable[[], Agent],
    strong_factory: Callable[[], Agent],
    k: int = 4,
    config: FableConfig | None = None,
) -> GapClosureReport:
    """The honesty metric: how much of the weak-to-strong gap did FABLE close?

    Three arms, same tasks, same graders, k runs each (k >= 4 recommended):

    - ``bare_weak_factory``: the weak model with no scaffolding (minimal
      gate-less agent).
    - ``scaffolded_weak_factory``: the same weak model under the full FABLE
      treatment -- gates, retries, routing, memory.
    - ``strong_factory``: the strong model, as the ceiling being chased.

    Publish the resulting number whatever it is (docs/00-philosophy.md
    section 7): a gap_closure of 0.30 on reasoning-heavy tasks is not a
    failed framework, it is the measured boundary between harness and
    weights -- exactly the thing the viral tweet never measured.
    """
    bare = run_eval(tasks, bare_weak_factory, k, config=config)
    scaffolded = run_eval(tasks, scaffolded_weak_factory, k, config=config)
    strong = run_eval(tasks, strong_factory, k, config=config)
    denominator = strong.pass_pow_k - bare.pass_pow_k
    ratio = (
        None
        if abs(denominator) < 1e-9
        else round((scaffolded.pass_pow_k - bare.pass_pow_k) / denominator, 4)
    )
    return GapClosureReport(
        bare_weak=bare,
        scaffolded_weak=scaffolded,
        strong=strong,
        gap_closure=ratio,
    )
