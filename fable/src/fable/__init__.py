"""FABLE -- Framework for Autonomous Blueprinted Long-horizon Execution.

Three concepts get you a verified, budget-capped, traced agent:

    from fable import run, tool, check

    @tool
    def word_count(file_path: str) -> str:
        \"\"\"Count words in a file. Call when asked about document length.\"\"\"
        from pathlib import Path
        return str(len(Path(file_path).read_text().split()))

    result = run(
        "Fix the failing test in tests/test_parser.py",
        tools=[word_count],
        verify=check.command("pytest -q"),
    )
    print(result.status, result.cost_usd)

Everything else is progressive disclosure, imported from its submodule:
``fable.subagents.spawn``, ``fable.memory.Memory``, ``fable.config.Router``,
``fable.trace.TraceReader``, ``fable.evals.run_eval``, and so on. The top
level exports exactly the beginner surface, nothing more -- the namespace is
the on-ramp.

Honest framing: this package is harness engineering. It raises pass^k (the
odds a whole run survives verification), never per-step correctness. See
docs/00-philosophy.md.
"""

from fable.loop import run, Agent, RunResult
from fable.tools import tool, Tool
from fable.verify import check, Gate, Check, Evidence
from fable.config import FableConfig, Budget

__all__ = ["Agent", "Budget", "Check", "Evidence", "FableConfig", "Gate",
           "RunResult", "Tool", "check", "run", "tool"]
