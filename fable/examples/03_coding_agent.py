#!/usr/bin/env python3
"""FABLE example 03 -- the full cascade on a coding task.

Everything docs/04-verification.md preaches, in one runnable script:

- **Harness-owned red test** (``assert_red``): the tests exist before the
  agent does, and they must FAIL first -- a test that passes against broken
  code verifies nothing.
- **Blueprint + plan gate** (``plan=True``): the first model call emits a
  typed Blueprint whose per-step ``verifier`` field is the routing policy;
  step status is harness-owned.
- **Structural test protection** (``check.diff_scope``): the implementer is
  mechanically blocked from test/CI files. Instruction-level bans do not
  survive reward hacking; a scope check does.
- **Fresh-process completion gate** (``check.command``): pytest re-run by the
  harness, not quoted from the transcript.
- **Adversarial refuter** (``check.refuter``): a strong-tier model is paid to
  find one concrete counterexample before we believe the green.
- **Checkpoint/resume**: if context pressure checkpoints the run, we resume
  from files -- fresh window, filesystem rediscovery.

Run with FABLE_EVAL_K=4 to measure pass@1 / pass^4 instead of a single run --
per-step flips are the honest reliability metric, not one lucky green.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # or: pip install -e .

from fable import Agent, Budget, FableConfig, Gate, check
from fable.memory import Memory
from fable.tools import fs_tools
from fable.trace import TraceReader, detect_failures
from fable.verify import assert_red

if not os.environ.get("ANTHROPIC_API_KEY"):
    print("This example calls the Claude API and needs ANTHROPIC_API_KEY set:")
    print("    export ANTHROPIC_API_KEY=sk-ant-...")
    print("No key found; exiting without making any calls.")
    raise SystemExit(0)

if subprocess.run([sys.executable, "-m", "pytest", "--version"],
                  capture_output=True).returncode != 0:
    print("This example verifies with pytest:  pip install pytest")
    raise SystemExit(0)
if shutil.which("git") is None:
    print("This example uses check.diff_scope, which needs git on PATH.")
    raise SystemExit(0)

PYTEST = f"{sys.executable} -m pytest -q"

# The planted defect: single-character counts break at runs >= 10, and decode
# assumes one count digit. Round-trip fails on "aaaaaaaaaaab".
BUGGY_RLE = '''\
"""Run-length encoding for ASCII text without digits in the payload."""


def encode(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        j = i
        while j < len(text) and text[j] == text[i]:
            j += 1
        out.append(text[i] + str(j - i)[-1])   # BUG: keeps only the last digit
        i = j
    return "".join(out)


def decode(data: str) -> str:
    out = []
    for k in range(0, len(data), 2):           # BUG: assumes 1-digit counts
        out.append(data[k] * int(data[k + 1]))
    return "".join(out)
'''

TESTS = '''\
from rle import decode, encode


def test_round_trip_short():
    assert decode(encode("aabbbc")) == "aabbbc"


def test_round_trip_long_run():
    s = "a" * 12 + "b"
    assert decode(encode(s)) == s


def test_empty():
    assert decode(encode("")) == ""
'''


def make_workspace() -> Path:
    """Pristine workspace: buggy module committed, harness-owned tests added.

    Committed BEFORE the tests exist would let diff_scope miss test edits;
    committing everything means any later change to tests/ shows in the diff.
    """
    ws = Path(tempfile.mkdtemp(prefix="fable-ex03-"))
    (ws / "rle.py").write_text(BUGGY_RLE, encoding="utf-8")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_rle.py").write_text(TESTS, encoding="utf-8")
    for cmd in (
        "git init -q",
        "git config user.email fable@example.invalid",
        "git config user.name fable",
        "git add -A",
        "git commit -qm baseline",
    ):
        subprocess.run(cmd, shell=True, cwd=ws, check=True, capture_output=True)
    return ws


ws = make_workspace()
os.chdir(ws)

# --- 1. the red gate: tests must fail BEFORE the agent runs --------------- #
red = assert_red(PYTEST, ws)
print(f"assert_red: passed={red.passed} exit={red.exit_code}")
print(f"            {red.detail}")
if not red.passed:
    raise SystemExit("Vacuous tests -- nothing to verify. Aborting.")

# --- 2. the gate ladder ---------------------------------------------------- #
gate = Gate(
    checks=(
        check.diff_scope(allowed_globs=("rle.py",)),   # tests are untouchable
        check.command(PYTEST),                          # fresh-process green
        check.refuter(
            brief=(
                "rle.py claims encode/decode round-trips any ASCII string "
                "without digits, including runs of length >= 10 and the "
                "empty string. Find one input where decode(encode(s)) != s."
            ),
            artifact_globs=("rle.py",),
        ),
    ),
    max_retries=2,
    on_exhaust="escalate",   # effort bump, then tier bump, then typed failure
)

# Memory (checkpoints + lessons) must live OUTSIDE ws. diff_scope allows only
# rle.py, so a .fable/memory tree written inside the git workspace would surface
# as an out-of-scope diff -- and the harness's own resume checkpoint would be
# rejected by its own gate. Keep it in a sibling tempdir.
memory = Memory(Path(tempfile.mkdtemp(prefix="fable-ex03-mem-")) / "memory")
config = FableConfig()


def build_agent() -> Agent:
    return Agent(
        system=None,                      # default executor.md
        tools=fs_tools(root=ws),
        verify=gate,
        config=config,
        role="executor",
        memory=memory,
    )


task = f"""Fix the bugs in {ws}/rle.py so `{PYTEST}` passes in {ws}.
Scope: you may modify ONLY {ws}/rle.py. Tests and everything else are
out of scope and mechanically enforced. Read the tests and the module
first; run nothing you have not read. Use absolute paths."""

# --- 3. run with a Blueprint (plan gate validates verifier fields) --------- #
agent = build_agent()
result = agent.run(task, plan=True, budget=Budget(max_usd=5.0, max_turns=25))

# --- 4. checkpoint/resume path: fresh window, filesystem rediscovery ------- #
if result.status == "checkpointed" and result.checkpoint_path is not None:
    print(f"\ncheckpointed at {result.checkpoint_path}; respawning fresh session")
    result = build_agent().resume(result.checkpoint_path)

print(f"\nstatus            {result.status}")
print(f"turns             {result.turns}")
print(f"cost_usd          {result.cost_usd:.4f}")
print(f"cache_hit_ratio   {result.cache_hit_ratio:.0%}")
for ev in result.evidence:
    print(f"evidence          {ev.name}: passed={ev.passed} exit={ev.exit_code}")

# --- 5. deterministic failure-mode detectors over our own trace ------------ #
findings = detect_failures(TraceReader(result.trace_path))
print(f"\ntrace             {result.trace_path}")
print(f"detectors         {findings if findings else 'no failure modes flagged'}")

# --- 6. keep the lesson if we earned one ------------------------------------ #
if result.status == "ok":
    memory.add_lesson(
        trigger="when RLE round-trip fails only on long runs, suspect count truncation",
        body="Single-character count encoding drops digits at run length >= 10.\n"
             "Verify with a 12-char run before trusting any RLE green.",
    )

# --- 7. optional: measure instead of anecdote ------------------------------- #
k = int(os.environ.get("FABLE_EVAL_K", "0") or 0)
if k > 1:
    from fable.evals import EvalTask, run_eval

    def factory() -> Agent:
        # Fresh pristine workspace per attempt -- pass^k over the SAME task
        # measures the harness, so every attempt must start equal.
        fresh = make_workspace()
        os.chdir(fresh)
        return Agent(
            system=None, tools=fs_tools(root=fresh),
            verify=Gate(checks=(check.command(PYTEST),), max_retries=2),
            config=config, role="executor",
        )

    # Each attempt runs in its OWN fresh workspace (factory chdir's into it and
    # jails tools there), so the eval task must NOT carry the original ws path --
    # that path is outside every fresh root and would be rejected by containment.
    # Reference the workspace generically; the default executor prompt fills
    # {{workspace_root}} from the current working directory, so each attempt's
    # agent resolves the concrete absolute path for its own fresh workspace.
    eval_task = (
        "Fix the bugs in rle.py in your workspace root so the pytest suite "
        "passes. Scope: you may modify ONLY rle.py; the tests and everything "
        "else are out of scope and mechanically enforced. Read the tests and "
        "the module first; run nothing you have not read. Build absolute paths "
        "by joining filenames onto your workspace root."
    )
    report = run_eval(
        [EvalTask(id="rle-fix", task=eval_task, grader=check.command(PYTEST))],
        factory,
        k=k,
    )
    print(f"\npass@1={report.pass_at_1:.2f}  pass^{report.k}={report.pass_pow_k:.2f}  "
          f"flips={report.per_task_flips}  cost=${report.total_cost_usd:.2f}")
