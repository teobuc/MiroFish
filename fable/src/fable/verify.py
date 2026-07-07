"""FABLE verification: the gate that decides "done", because the model cannot.

The load-bearing idea (docs/04-verification.md): ``end_turn`` is a *claim*.
Every check here converts a claim into :class:`Evidence` -- a named, typed
record with a command, an exit code, and an output tail -- or into a concrete
failure the loop feeds back verbatim. Booleans hide too much; evidence argues.

The gate ladder runs cheapest-first and short-circuits between rungs:

1. **Claim audit** (zero tokens, deterministic): every "done/passing/created/
   fixed" claim in the completion report must cite a ``tool_use_id`` whose
   Evidence-Ledger record supports it.
2. **Mechanical** (subprocess tokens only): decisive commands re-executed in a
   FRESH process. The agent's transcript of a test run is a claim, not proof.
3. **Judged** (model tokens, mid tier): single-call multi-dimension rubric via
   structured output, fresh context, artifacts only -- never the generator's
   rationale. Conjunctive must-pass; no weighted averages.
4. **Adversarial** (model tokens, strong tier, optional): a refuter charged
   with producing ONE concrete counterexample, never asked "do you agree?".

Honesty note: verification raises pass^k (the odds that what ships is what
was claimed), never per-step correctness p. A gate cannot make the model
smarter; it can only stop the model's mistakes from becoming your outputs.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Sequence

from fable.client import FableClient, FrozenPrefix, UsageLedger

if TYPE_CHECKING:  # loop imports verify; verify must never import loop at runtime
    from fable.loop import Blueprint

_COMMAND_TIMEOUT_S = 600
_OUTPUT_TAIL_CHARS = 2_000
_ARTIFACT_CAP_CHARS = 8_000
_MAX_ARTIFACTS_SHOWN = 12
_RUBRIC_THRESHOLD = 0.7

# Status verbs that make a sentence a *completion claim* requiring evidence.
_CLAIM_VERBS = re.compile(
    r"(?i)\b(done|complete[d]?|passing|passed|created|fixed|implemented|"
    r"verified|resolved|works|working|green|deployed|installed)\b"
)

# Test/CI files the implementer role must never touch (structural block --
# instruction-level bans do not survive reward hacking).
_PROTECTED_FILE = re.compile(
    r"(^|/)(tests?/|test_[^/]+$|[^/]+_test\.[^/]+$|conftest\.py$|pytest\.ini$|"
    r"tox\.ini$|\.github/|\.gitlab-ci\.yml$|\.circleci/)"
)

# One frozen system string per verifier role. Frozen because FableClient pins
# a prefix digest per role -- and because a byte-stable judge prefix caches.
_JUDGE_SYSTEM = (
    "You are an independent quality judge. You see artifacts and captured tool "
    "outputs ONLY -- never the generating agent's reasoning, so you cannot be "
    "argued into a score. Grade each criterion from 0.0 (absent/wrong) to 1.0 "
    "(fully satisfied) with a one-sentence justification grounded in what you "
    "can literally see. Do not average away a failed criterion: score what is "
    "there, not what was probably intended."
)
_REFUTER_SYSTEM = (
    "You are an adversarial refuter. Your ONLY job is to produce one concrete "
    "counterexample or failing input for the claim in front of you: a specific "
    "input, command, or scenario, plus the incorrect behavior it provokes. "
    "You are not asked whether you agree, and general concerns do not count. "
    "If you cannot construct a concrete counterexample, say so plainly -- a "
    "forced refutation is worse than none."
)


# --------------------------------------------------------------------------- #
# Core types


@dataclass(frozen=True)
class Evidence:
    """One verifiable fact about the run. Checks return these, never bools.

    ``output_tail`` and ``artifact_path`` exist so a FAILED gate can feed the
    model concrete facts to retry against -- "the gate failed" teaches
    nothing; "pytest exited 1, last 20 lines attached" teaches everything.
    """

    name: str
    passed: bool
    command: str | None = None
    exit_code: int | None = None
    output_tail: str = ""
    artifact_path: Path | None = None
    detail: str = ""


@dataclass(frozen=True)
class Check:
    """One named check in a gate.

    ``kind`` places it on the ladder (mechanical -> judged -> adversarial);
    ``must_pass=False`` demotes a failure to a recorded warning -- useful for
    advisory rubrics that are not yet calibrated (see :meth:`_CheckNS.rubric`).
    """

    name: str
    kind: Literal["mechanical", "judged", "adversarial"]
    must_pass: bool = True
    run: Callable[["GateContext"], Evidence] = None  # type: ignore[assignment]


@dataclass
class GateContext:
    """Everything a Check is allowed to see. Deliberately narrow: no message
    history, no model rationale -- artifacts and ledger ground truth only."""

    workspace: Path
    ledger: UsageLedger
    client: FableClient
    final_report: dict | None
    blueprint: "Blueprint | None"


@dataclass(frozen=True)
class GateResult:
    passed: bool
    evidence: tuple[Evidence, ...]
    failures: tuple[Evidence, ...]


# --------------------------------------------------------------------------- #
# Helpers


def _tail(text: str, limit: int = _OUTPUT_TAIL_CHARS) -> str:
    return text if len(text) <= limit else "...(truncated)...\n" + text[-limit:]


def _run_fresh(cmd: str, cwd: Path) -> tuple[int, str]:
    """Run a command in a FRESH subprocess. The whole point: not the agent's
    shell, not the agent's transcript -- an independent measurement."""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_S,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {_COMMAND_TIMEOUT_S}s: {cmd}"


def _read_artifacts(paths: Sequence[Path | str], workspace: Path | None = None) -> str:
    parts: list[str] = []
    for raw in list(paths)[:_MAX_ARTIFACTS_SHOWN]:
        path = Path(raw)
        if not path.is_absolute() and workspace is not None:
            path = workspace / path
        if path.exists() and path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            if len(text) > _ARTIFACT_CAP_CHARS:
                text = text[:_ARTIFACT_CAP_CHARS] + "\n...(truncated)..."
            parts.append(f"## Artifact: {raw}\n```\n{text}\n```")
        else:
            parts.append(f"## Artifact: {raw}\n(MISSING on disk -- treat as a defect)")
    return "\n\n".join(parts)


def _validate_json(instance: Any, schema: dict, where: str = "$") -> list[str]:
    """Minimal JSON-Schema validator (type/properties/required/items/enum/
    additionalProperties). Stdlib-only on purpose; covers what FABLE emits."""
    errors: list[str] = []
    expected = schema.get("type")
    type_map = {
        "object": dict, "array": list, "string": str,
        "integer": int, "number": (int, float), "boolean": bool,
    }
    if expected in type_map:
        ok = isinstance(instance, type_map[expected])  # type: ignore[arg-type]
        if expected == "number" and isinstance(instance, bool):
            ok = False
        if expected == "integer" and isinstance(instance, bool):
            ok = False
        if not ok:
            return [f"{where}: expected {expected}, got {type(instance).__name__}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{where}: {instance!r} not in enum {schema['enum']!r}")
    if expected == "object" and isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{where}: missing required key {key!r}")
        props = schema.get("properties", {})
        for key, value in instance.items():
            if key in props:
                errors.extend(_validate_json(value, props[key], f"{where}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{where}: unexpected key {key!r}")
    if expected == "array" and isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errors.extend(_validate_json(item, schema["items"], f"{where}[{i}]"))
    return errors


# --------------------------------------------------------------------------- #
# check.* -- the constructor namespace


class _CheckNS:
    """Constructors for the built-in checks; exported as the singleton
    ``check`` so user code reads ``verify=check.command("pytest -q")``."""

    def command(
        self, cmd: str, expect_exit: int = 0, expect_stdout: str | None = None
    ) -> Check:
        """The workhorse mechanical check: re-run a decisive command in a
        FRESH subprocess and compare exit code (and optionally a stdout
        substring). If your task has a command that proves success, this
        check is worth more than every judge in this file."""

        def run(ctx: GateContext) -> Evidence:
            code, output = _run_fresh(cmd, ctx.workspace)
            passed = code == expect_exit
            detail = ""
            if passed and expect_stdout is not None and expect_stdout not in output:
                passed = False
                detail = f"exit code matched but stdout lacks {expect_stdout!r}"
            return Evidence(
                name=f"command:{cmd}", passed=passed, command=cmd,
                exit_code=code, output_tail=_tail(output), detail=detail,
            )

        return Check(name=f"command:{cmd}", kind="mechanical", run=run)

    def file_exists(self, path: str) -> Check:
        """Assert an artifact exists and is non-empty. Cheapest possible
        antidote to 'I created the file' claims."""

        def run(ctx: GateContext) -> Evidence:
            p = Path(path)
            if not p.is_absolute():
                p = ctx.workspace / p
            ok = p.exists() and p.is_file() and p.stat().st_size > 0
            return Evidence(
                name=f"file_exists:{path}", passed=ok, artifact_path=p,
                detail="" if ok else f"{p} missing or empty",
            )

        return Check(name=f"file_exists:{path}", kind="mechanical", run=run)

    def schema(self, schema: dict, path: str | None = None) -> Check:
        """Validate a JSON artifact (or the completion report when ``path``
        is None) against a schema. Structure lies less than prose."""

        def run(ctx: GateContext) -> Evidence:
            name = f"schema:{path or 'final_report'}"
            if path is None:
                instance = ctx.final_report
                if instance is None:
                    return Evidence(name=name, passed=False,
                                    detail="no completion report to validate")
            else:
                p = Path(path)
                if not p.is_absolute():
                    p = ctx.workspace / p
                if not p.exists():
                    return Evidence(name=name, passed=False, artifact_path=p,
                                    detail=f"{p} does not exist")
                try:
                    instance = json.loads(p.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    return Evidence(name=name, passed=False, artifact_path=p,
                                    detail=f"invalid JSON: {exc}")
            errors = _validate_json(instance, schema)
            return Evidence(name=name, passed=not errors,
                            detail="; ".join(errors[:10]))

        return Check(name=f"schema:{path or 'final_report'}", kind="mechanical", run=run)

    def diff_scope(self, allowed_globs: Sequence[str]) -> Check:
        """Fail on out-of-scope file touches; flag ANY test/CI-file change.

        This is the test-tampering detector, enforced structurally: an
        implementer that edits tests to make them pass fails this check no
        matter what the tests now say. Requires a git repo (uses
        ``git diff --name-only HEAD`` plus untracked files)."""

        globs = tuple(allowed_globs)

        def run(ctx: GateContext) -> Evidence:
            code1, tracked = _run_fresh("git diff --name-only HEAD", ctx.workspace)
            code2, untracked = _run_fresh(
                "git ls-files --others --exclude-standard", ctx.workspace
            )
            if code1 != 0:
                return Evidence(
                    name="diff_scope", passed=False, exit_code=code1,
                    detail="git diff failed -- diff_scope needs a git repo with "
                           "an initial commit", output_tail=_tail(tracked),
                )
            touched = sorted(
                {f.strip() for f in (tracked + "\n" + (untracked if code2 == 0 else ""))
                 .splitlines() if f.strip()}
            )
            out_of_scope = [
                f for f in touched if not any(fnmatch.fnmatch(f, g) for g in globs)
            ]
            tampered = [f for f in touched if _PROTECTED_FILE.search(f)]
            problems = []
            if out_of_scope:
                problems.append(f"out-of-scope touches: {', '.join(out_of_scope)}")
            if tampered:
                problems.append(
                    f"test/CI files modified by implementer (forbidden): "
                    f"{', '.join(tampered)}"
                )
            return Evidence(
                name="diff_scope", passed=not problems,
                detail="; ".join(problems),
                output_tail=f"touched files: {', '.join(touched) or '(none)'}",
            )

        return Check(name="diff_scope", kind="mechanical", run=run)

    def callable(
        self, fn: Callable[[GateContext], bool | Evidence], name: str = ""
    ) -> Check:
        """Wrap an arbitrary predicate. Return Evidence for a rich record, or
        a bare bool for convenience."""

        check_name = name or getattr(fn, "__name__", "callable")

        def run(ctx: GateContext) -> Evidence:
            result = fn(ctx)
            if isinstance(result, Evidence):
                return result
            return Evidence(name=check_name, passed=bool(result))

        return Check(name=check_name, kind="mechanical", run=run)

    def rubric(
        self,
        criteria: Sequence[str],
        role: str = "judge",
        threshold: float = _RUBRIC_THRESHOLD,
        calibration_ref: str | None = None,
        artifacts: Sequence[str] = (),
    ) -> Check:
        """LLM-judged rubric: ONE structured call scoring every criterion
        0.0-1.0, fresh context, different prompt lineage from the generator.

        The judge sees artifacts and the completion report only -- never the
        generator's rationale, which is how self-preference sneaks in. The
        verdict is conjunctive: every criterion must clear ``threshold``; a
        weighted average is how one failed criterion hides behind four easy
        ones.

        Until ``calibration_ref`` names a labeled set where this rubric agrees
        with a human >=90% of the time, the check WARNS at construction:
        an uncalibrated judge used as a hard gate is theater
        (docs/04-verification.md section 4 has the calibration protocol).
        """

        if calibration_ref is None:
            warnings.warn(
                "check.rubric: uncalibrated judge. Collect 50-200 labeled "
                "examples and set calibration_ref before trusting this as a "
                "hard gate; consider must_pass=False until then.",
                stacklevel=2,
            )
        criteria = tuple(criteria)
        extra_artifacts = tuple(artifacts)
        schema = {
            "type": "object",
            "properties": {
                "scores": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "criterion": {"type": "string"},
                            "score": {"type": "number"},
                            "justification": {"type": "string"},
                        },
                        "required": ["criterion", "score", "justification"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["scores"],
            "additionalProperties": False,
        }

        def run(ctx: GateContext) -> Evidence:
            material: list[str] = []
            if ctx.final_report is not None:
                material.append(
                    "## Completion report (claims are UNVERIFIED)\n"
                    + json.dumps(ctx.final_report, indent=2)
                )
                report_artifacts = ctx.final_report.get("artifacts", [])
            else:
                report_artifacts = []
            shown = list(dict.fromkeys([*extra_artifacts, *report_artifacts]))
            if shown:
                material.append(_read_artifacts(shown, ctx.workspace))
            body = "\n\n".join(material) or "(no report or artifacts were supplied)"
            prompt = (
                "Grade the following work against each criterion. Score 0.0-1.0 "
                "per criterion with a one-sentence justification.\n\n"
                "Criteria:\n"
                + "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria))
                + "\n\n" + body
            )
            prefix = FrozenPrefix.build(_JUDGE_SYSTEM, ())
            data = ctx.client.structured(
                prefix=prefix,
                messages=[{"role": "user", "content": prompt}],
                role=role,
                schema=schema,
            )
            scores = {
                s["criterion"]: max(0.0, min(1.0, float(s["score"])))
                for s in data.get("scores", [])
            }
            missing = [c for c in criteria if c not in scores]
            failing = {c: v for c, v in scores.items() if v < threshold}
            passed = not missing and not failing
            return Evidence(
                name="rubric", passed=passed,
                detail=json.dumps(
                    {"threshold": threshold, "scores": scores,
                     "missing": missing,
                     "judgments": {s["criterion"]: s["justification"]
                                   for s in data.get("scores", [])}},
                    indent=2,
                ),
            )

        return Check(name="rubric", kind="judged", run=run)

    def refuter(
        self,
        brief: str,
        artifact_globs: Sequence[str] = ("**/*.py",),
        role: str = "refuter",
    ) -> Check:
        """Adversarial rung: a strong-tier refuter tries to produce ONE
        concrete counterexample against ``brief`` given the matched artifacts.
        Reserve for high-stakes gates -- it is the most expensive rung."""

        globs = tuple(artifact_globs)

        def run(ctx: GateContext) -> Evidence:
            paths: list[Path] = []
            for g in globs:
                paths.extend(p for p in sorted(ctx.workspace.glob(g)) if p.is_file())
            found = refute(paths[:_MAX_ARTIFACTS_SHOWN], brief, client=ctx.client,
                           role=role)
            if found is not None:
                return found
            return Evidence(name="refuter", passed=True,
                            detail="refuter found no concrete counterexample")

        return Check(name="refuter", kind="adversarial", run=run)


check = _CheckNS()


# --------------------------------------------------------------------------- #
# Gate


@dataclass
class Gate:
    """An ordered ladder of checks. ``run`` executes ONE pass; the loop owns
    the retry counter (``max_retries``) and the escalation decision
    (``on_exhaust``) so retries stay bounded and typed.

    Cost note: on high-stakes tasks expect verification to consume 20-50% of
    tokens. That is not overhead -- it is the purchase price of pass^k.
    """

    checks: Sequence[Check]
    max_retries: int = 3
    on_exhaust: Literal["fail", "escalate"] = "fail"

    def run(self, ctx: GateContext) -> GateResult:
        evidence: list[Evidence] = []
        failures: list[Evidence] = []

        def absorb(items: Sequence[Evidence], must_pass: bool = True) -> None:
            for ev in items:
                evidence.append(ev)
                if not ev.passed and must_pass:
                    failures.append(ev)

        # Rung 1: claim audit -- zero tokens, runs whenever a report exists.
        if ctx.final_report is not None:
            audit = audit_claims(ctx.final_report, ctx.ledger)
            absorb(audit.evidence)
            if failures:
                return GateResult(False, tuple(evidence), tuple(failures))

        # Rungs 2-4: short-circuit BETWEEN rungs (never spend judge tokens on
        # work that already fails mechanically), run everything WITHIN a rung
        # (the retry needs the full failure list, not the first hit).
        for rung in ("mechanical", "judged", "adversarial"):
            for chk in self.checks:
                if chk.kind != rung:
                    continue
                try:
                    ev = chk.run(ctx)
                except Exception as exc:  # noqa: BLE001 -- a crashed check fails closed
                    ev = Evidence(name=chk.name, passed=False,
                                  detail=f"check raised {exc!r}")
                absorb([ev], must_pass=chk.must_pass)
            if failures:
                return GateResult(False, tuple(evidence), tuple(failures))

        return GateResult(True, tuple(evidence), tuple(failures))


# --------------------------------------------------------------------------- #
# Standalone verifiers


def audit_claims(report: dict, ledger: UsageLedger) -> GateResult:
    """Deterministic claim-evidence audit. Zero tokens.

    Every claim whose text asserts completion ("done", "passing", "created",
    "fixed", ...) must carry at least one ``tool_use_id`` whose Evidence-Ledger
    record exists, is not an error, and (when an exit code was captured)
    exited 0. The ledger recorded ground truth BEFORE truncation/shaping, so
    the model cannot cite output the harness never saw.
    """
    evidence: list[Evidence] = []
    failures: list[Evidence] = []
    for claim in report.get("claims", []):
        text = str(claim.get("text", ""))
        ids = list(claim.get("tool_use_ids", []))
        label = f"claim:{text[:60]}"
        if not _CLAIM_VERBS.search(text):
            evidence.append(Evidence(name=label, passed=True,
                                     detail="not a completion claim; not audited"))
            continue
        if not ids:
            ev = Evidence(name=label, passed=False,
                          detail="UNGROUNDED: completion claim cites no tool_use_id")
            evidence.append(ev)
            failures.append(ev)
            continue
        bad: list[str] = []
        for tool_use_id in ids:
            record = ledger.evidence_for(tool_use_id)
            if record is None:
                bad.append(f"{tool_use_id}: no ledger record")
            elif record["is_error"]:
                bad.append(f"{tool_use_id}: tool call errored")
            elif record["exit_code"] not in (0, None):
                bad.append(f"{tool_use_id}: exit code {record['exit_code']}")
        ev = Evidence(name=label, passed=not bad, detail="; ".join(bad))
        evidence.append(ev)
        if bad:
            failures.append(ev)
    return GateResult(not failures, tuple(evidence), tuple(failures))


_REFUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "refutation_found": {"type": "boolean"},
        "counterexample": {"type": "string"},
        "expected_vs_actual": {"type": "string"},
    },
    "required": ["refutation_found", "counterexample", "expected_vs_actual"],
    "additionalProperties": False,
}


def refute(
    artifact_paths: Sequence[Path],
    brief: str,
    *,
    client: FableClient,
    role: str = "refuter",
) -> Evidence | None:
    """Asymmetric falsification: 'produce one concrete counterexample or
    failing input'. Returns None when no refutation was found.

    Fresh context, artifacts only. The asymmetry is the point: agreement is
    cheap and correlated; a concrete counterexample is checkable. When the
    counterexample is executable, run it before believing it -- refuters can
    confabulate failures just as generators confabulate successes.
    """
    material = _read_artifacts(list(artifact_paths))
    prompt = (
        f"Claim under attack:\n{brief}\n\n"
        f"{material or '(no artifacts supplied)'}\n\n"
        "Produce ONE concrete counterexample or failing input, with the "
        "expected-vs-actual behavior. If you cannot construct one, set "
        "refutation_found to false."
    )
    prefix = FrozenPrefix.build(_REFUTER_SYSTEM, ())
    data = client.structured(
        prefix=prefix,
        messages=[{"role": "user", "content": prompt}],
        role=role,
        schema=_REFUTE_SCHEMA,
    )
    if not data.get("refutation_found"):
        return None
    return Evidence(
        name="refutation",
        passed=False,
        detail=(
            f"counterexample: {data.get('counterexample', '')}\n"
            f"expected vs actual: {data.get('expected_vs_actual', '')}"
        ),
    )


def refute_vote(
    artifact_paths: Sequence[Path],
    brief: str,
    *,
    client: FableClient,
    k: int = 3,
    role: str = "refuter",
) -> tuple[bool, tuple[Evidence, ...]]:
    """Refute-then-vote: k independent refuters; the artifact survives only
    if a MAJORITY find no counterexample.

    Returns ``(survived, refutations)`` -- every concrete counterexample is
    returned even when the vote survives, because a single *verified*
    counterexample should override the vote (check executable ones
    mechanically). Caveat from docs/04: same-model refuters share failure
    modes, so k refuters are less independent than k looks; this is a
    confidence widener, not a proof.
    """
    with ThreadPoolExecutor(max_workers=k) as pool:
        results = list(
            pool.map(
                lambda _: refute(artifact_paths, brief, client=client, role=role),
                range(k),
            )
        )
    refutations = tuple(r for r in results if r is not None)
    survived = len(refutations) <= k // 2
    return survived, refutations


def judge_panel(
    criteria: Sequence[str],
    artifact_paths: Sequence[Path],
    *,
    client: FableClient,
    k: int = 3,
    role: str = "judge",
    threshold: float = _RUBRIC_THRESHOLD,
) -> dict:
    """k independent single-call rubric judges; aggregate by per-criterion
    MINIMUM (conservative), verdict conjunctive over the minima.

    Not a default gate rung on purpose: panel members drawn from the same
    model family make correlated errors, so a panel mostly buys variance
    reduction, not independence. Prefer one calibrated judge plus a refuter.
    Returns ``{"passed", "per_judge", "aggregate"}``.
    """
    material = _read_artifacts(list(artifact_paths))
    prompt = (
        "Grade the following work against each criterion. Score 0.0-1.0 per "
        "criterion with a one-sentence justification.\n\nCriteria:\n"
        + "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria))
        + "\n\n" + (material or "(no artifacts supplied)")
    )
    schema = {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "criterion": {"type": "string"},
                        "score": {"type": "number"},
                        "justification": {"type": "string"},
                    },
                    "required": ["criterion", "score", "justification"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["scores"],
        "additionalProperties": False,
    }
    prefix = FrozenPrefix.build(_JUDGE_SYSTEM, ())

    def one(_: int) -> dict[str, float]:
        data = client.structured(
            prefix=prefix,
            messages=[{"role": "user", "content": prompt}],
            role=role,
            schema=schema,
        )
        return {
            s["criterion"]: max(0.0, min(1.0, float(s["score"])))
            for s in data.get("scores", [])
        }

    with ThreadPoolExecutor(max_workers=k) as pool:
        per_judge = list(pool.map(one, range(k)))
    aggregate = {
        c: min((scores.get(c, 0.0) for scores in per_judge), default=0.0)
        for c in criteria
    }
    return {
        "passed": all(v >= threshold for v in aggregate.values()),
        "per_judge": per_judge,
        "aggregate": aggregate,
    }


def self_consistent(
    prompt: str, k: int = 3, *, client: FableClient, role: str = "judge"
) -> tuple[str, float]:
    """Self-consistency vote for DISCRETE answers only (a label, a number, a
    yes/no). Returns ``(modal_answer, agreement)``; escalate below 2/3.

    Never use this for free-form output -- k paraphrases of the same wrong
    essay agree with each other. For prose, use refute-then-vote instead.
    """
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    prefix = FrozenPrefix.build(_JUDGE_SYSTEM, ())

    def one(_: int) -> str:
        data = client.structured(
            prefix=prefix,
            messages=[{"role": "user", "content": prompt}],
            role=role,
            schema=schema,
        )
        return str(data.get("answer", "")).strip()

    with ThreadPoolExecutor(max_workers=k) as pool:
        answers = list(pool.map(one, range(k)))
    modal, count = Counter(answers).most_common(1)[0]
    return modal, count / k


def assert_red(test_cmd: str, workspace: Path) -> Evidence:
    """TDD guard: the new tests must FAIL before implementation.

    A test that passes against the unimplemented code is vacuous -- it will
    also pass against a wrong implementation. Runs in a fresh subprocess.
    For pytest, exit 5 ("no tests collected") is treated as vacuous too.
    """
    code, output = _run_fresh(test_cmd, workspace)
    vacuous = code == 0 or ("pytest" in test_cmd and code == 5)
    return Evidence(
        name="assert_red",
        passed=not vacuous,
        command=test_cmd,
        exit_code=code,
        output_tail=_tail(output),
        detail=(
            "tests are RED pre-implementation (good)" if not vacuous else
            "VACUOUS: tests pass (or collect nothing) before implementation -- "
            "they cannot verify anything; fix the tests first"
        ),
    )
