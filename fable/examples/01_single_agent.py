#!/usr/bin/env python3
"""FABLE example 01 -- one agent, real tools, a verified exit. (~80 lines.)

The whole Tier-0 surface is three concepts: ``run`` / ``@tool`` / ``check``.
This script builds a tiny workspace with harness-owned tests, asks one agent
to make them pass, and lets the GATE -- not the model -- decide "done" by
re-running pytest in a fresh subprocess. Then it reads its own trace, because
the trace is a product surface, not a debug log.

Cost honesty: a single agent burns roughly 4x the tokens of a chat answer
(it re-sends its history every turn). Expect a few cents on this task.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # or: pip install -e .

from fable import run, check                 # Tier-0: the whole beginner API
from fable.tools import fs_tools
from fable.trace import TraceReader

if not os.environ.get("ANTHROPIC_API_KEY"):
    print("This example calls the Claude API and needs ANTHROPIC_API_KEY set:")
    print("    export ANTHROPIC_API_KEY=sk-ant-...")
    print("No key found; exiting without making any calls.")
    raise SystemExit(0)

if subprocess.run([sys.executable, "-m", "pytest", "--version"],
                  capture_output=True).returncode != 0:
    print("This example verifies with pytest:  pip install pytest")
    raise SystemExit(0)

# --- workspace: the TESTS are harness-owned ground truth, written before the
# agent exists. The agent gets a stub to edit (fs_tools edits, never creates).
ws = Path(tempfile.mkdtemp(prefix="fable-ex01-"))
(ws / "tests").mkdir()
(ws / "tests" / "test_slugify.py").write_text(
    "from slugify import slugify\n\n"
    "def test_basic():\n    assert slugify('Hello, World!') == 'hello-world'\n\n"
    "def test_collapse():\n    assert slugify('  a   b  ') == 'a-b'\n\n"
    "def test_symbols_only():\n    assert slugify('!!! ???') == ''\n",
    encoding="utf-8",
)
(ws / "slugify.py").write_text(
    "def slugify(text: str) -> str:\n"
    '    """Lowercase, alphanumerics kept, everything else collapses to one hyphen."""\n'
    "    raise NotImplementedError\n",
    encoding="utf-8",
)
os.chdir(ws)  # gate checks and the trace both anchor on the cwd

PYTEST = f"{sys.executable} -m pytest -q"
task = f"""Implement slugify() in {ws}/slugify.py so `{PYTEST}` passes in {ws}.
The requirements ARE the tests -- read {ws}/tests/test_slugify.py first.
Do not modify the tests. Use absolute paths in every tool call."""

result = run(
    task,
    tools=fs_tools(root=ws),                  # read/glob/grep/edit, jailed to ws
    verify=check.command(PYTEST),             # fresh-process pytest decides "done"
    budget_usd=2.00,                          # hard rail, projected pre-call
    max_turns=15,
)

# --- a RunResult is typed and honest: "ok" here means the gate re-ran pytest
# itself and saw exit 0 -- not that the model said "all tests pass".
print(f"\nstatus            {result.status}")
print(f"turns             {result.turns}")
print(f"cost_usd          {result.cost_usd:.4f}")
print(f"cache_hit_ratio   {result.cache_hit_ratio:.0%}   (below 80% => prefix is mutating)")
for ev in result.evidence:
    print(f"evidence          {ev.name}: passed={ev.passed} exit={ev.exit_code}")

# --- read our own trace: append-only JSONL, one event per loop action.
reader = TraceReader(result.trace_path)
counts: dict[str, int] = {}
for event in reader.events():
    counts[event.event] = counts.get(event.event, 0) + 1
print(f"\ntrace             {result.trace_path}")
print(f"events            {counts}")
print(f"cost by role      {reader.cost_by_role()}")
