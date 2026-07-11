#!/usr/bin/env python3
"""FABLE example 02 -- orchestrator + parallel researcher subagents + synthesis.

Pattern: orchestrator-workers (docs/03-orchestration.md section 2.2). The
orchestrator is a strong-tier Agent whose ONLY special power is one extra
tool -- ``spawn_subagent`` -- which re-enters the same loop with a fresh
context and a mid-tier researcher policy. Orchestration is data, not a
second engine.

The research corpus is this repository itself, so the example runs offline
except for the API: three researchers each read one area of FABLE and the
orchestrator synthesizes their digests into a report the gate then judges.

Cost honesty, printed at the end per role: multi-agent runs cost ~15x a chat
answer. This example exists to make that number visible, not to hide it.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # or: pip install -e .

from fable import Agent, Budget, FableConfig, check, tool
from fable.subagents import SubagentReport, spawn_subagent_tool
from fable.tools import fs_tools
from fable.trace import TraceReader

if not os.environ.get("ANTHROPIC_API_KEY"):
    print("This example calls the Claude API and needs ANTHROPIC_API_KEY set:")
    print("    export ANTHROPIC_API_KEY=sk-ant-...")
    print("No key found; exiting without making any calls.")
    raise SystemExit(0)

REPO = Path(__file__).resolve().parents[1]   # the FABLE tree: our research corpus
OUT = REPO.parent / "fable-ex02-out"
OUT.mkdir(exist_ok=True)
os.chdir(OUT)                                # gate + trace anchor here
# OUT persists between runs; a leftover report.md would satisfy the
# file_exists gate even if THIS run never wrote one. Clear it first so the
# gate certifies only what this run actually produces.
(OUT / "report.md").unlink(missing_ok=True)

config = FableConfig()  # researcher role defaults to claude-sonnet-5 @ medium

# Host-side collection of every SubagentReport: the honest way to account for
# subagent spend, which never appears in the orchestrator's own ledger.
reports: list[SubagentReport] = []
delegate = spawn_subagent_tool(
    config,
    reports=reports,
    subagent_tools=fs_tools(root=REPO),      # researchers read the repo, jailed
)


@tool
def write_report(content: str) -> str:
    """Write the final research report to report.md, replacing any previous
    version. Call exactly once, after all subagent digests are in."""
    path = OUT / "report.md"
    path.write_text(content, encoding="utf-8")
    return f"Wrote {path} ({len(content)} chars)."


SYSTEM = f"""You are a research orchestrator. You never read source files
yourself -- you delegate reading to researcher subagents and synthesize their
digests.

Plan of record (follow it literally):
1. In your FIRST tool-using turn, issue exactly THREE spawn_subagent calls in
   that ONE turn so they run in parallel:
   a. the Python modules under {REPO}/src/fable/ -- public API and import DAG
   b. the documents under {REPO}/docs/ -- one-sentence thesis per document
   c. the prompt templates under {REPO}/prompts/ -- role and key contract of each
2. Synthesize the three digests into a 500-900 word architecture briefing and
   write it with write_report. Cite concrete file paths. Where researchers
   disagree or report open questions, say so rather than smoothing it over.
3. In your completion report, list report.md as an artifact and cite the
   write_report tool call id for the "report written" claim."""

agent = Agent(
    system=SYSTEM,
    tools=[delegate, write_report],
    verify=[
        check.file_exists(str(OUT / "report.md")),
        # Uncalibrated judge => it WARNS at construction; docs/04 explains the
        # 50-200-label calibration protocol that would earn calibration_ref.
        check.rubric(
            criteria=[
                "Report describes all three areas (src modules, docs, prompts)",
                "Every section cites at least one concrete file path",
                "Open questions or disagreements are stated, not smoothed over",
            ],
            artifacts=(str(OUT / "report.md"),),
        ),
    ],
    config=config,
    role="orchestrator",
)

result = agent.run(
    "Produce the FABLE architecture briefing per your plan of record.",
    budget=Budget(max_usd=8.0, max_turns=12),
)

# --- per-role cost breakdown: the ~15x claim, itemized ------------------- #
subagent_cost = sum(r.cost_usd for r in reports)
print(f"\nstatus                {result.status}")
print(f"report                {OUT / 'report.md'}")
print(f"orchestrator cost     ${result.cost_usd:.4f}   (its own ledger)")
for i, r in enumerate(reports, 1):
    print(f"researcher {i} cost     ${r.cost_usd:.4f}   "
          f"confidence={r.confidence:.2f} trace={r.trace_path.name}")
print(f"TOTAL                 ${result.cost_usd + subagent_cost:.4f}")
print(f"cache_hit_ratio       {result.cache_hit_ratio:.0%} (orchestrator)")
print(f"cost by role (main)   {TraceReader(result.trace_path).cost_by_role()}")
for ev in result.evidence:
    print(f"evidence              {ev.name}: passed={ev.passed}")
if reports:
    print("\nOpen questions from researchers:")
    for r in reports:
        for q in r.open_questions:
            print(f"  - {q}")
