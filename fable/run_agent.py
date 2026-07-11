#!/usr/bin/env python3
"""FABLE one-command runner — the easiest way to run a verified agent.

This is the "just run it" entry point. It wraps :func:`fable.run` with sensible,
safe defaults so a human (or another automated/agentic process) can launch a
budget-capped, verified, fully-traced agent with a single shell command — no
framework knowledge required.

    # from a checkout, with src/ on the path:
    cd fable && export PYTHONPATH=src && export ANTHROPIC_API_KEY=sk-ant-...

    # simplest: give it a task
    python run_agent.py "Summarize every TODO in this directory into TODOS.md"

    # verify the result with a real command (the agent cannot self-declare done)
    python run_agent.py "Fix the failing test in tests/test_parser.py" \
        --verify "pytest -q" --budget 2.00

    # let the agent run shell commands, but only from an allowlist
    python run_agent.py "Run the linter and fix what it flags" \
        --allow-shell "ruff,python" --verify "ruff check ."

Everything the agent does is written to an append-only JSONL trace whose path is
printed at the end; nothing is ever "done" unless a gate verified it.

Another program can call this instead of the CLI::

    from run_agent import run_task
    result = run_task("Generate a report", verify="test -f report.md", budget=1.0)
    if result.status == "ok":
        ...

Exit code is 0 only when the run's status is "ok" (gate-verified) or
"ok_unverified" (no gate was requested); any rail trip, refusal, or failure
exits non-zero, so shell pipelines and CI can branch on it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make `fable` importable when run from a checkout without installation.
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Statuses that mean "the run reached a healthy terminal state".
_SUCCESS_STATUSES = frozenset({"ok", "ok_unverified"})


def run_task(
    task: str,
    *,
    workdir: str | Path = ".",
    verify: str | None = None,
    budget: float = 2.0,
    max_turns: int | None = None,
    allow_shell: str | None = None,
    effort: str | None = None,
):
    """Run one verified agent and return its :class:`fable.loop.RunResult`.

    Args:
        task: What the agent should accomplish, in plain language.
        workdir: Directory the agent's file tools are jailed to (default: cwd).
        verify: A shell command that must exit 0 for the run to be graded "ok"
            (e.g. ``"pytest -q"``). Omit to run without a gate — the best
            possible status is then ``"ok_unverified"``, an honest label.
        budget: Hard USD ceiling for the whole run. The loop projects the next
            call's cost and stops before crossing it.
        max_turns: Optional cap on tool-turns (a second stop rail).
        allow_shell: Comma-separated executables the agent may run via a shell
            tool (e.g. ``"pytest,python,git"``). Omit to give it no shell at all.
        effort: Optional effort override (``low``/``medium``/``high``/``xhigh``/
            ``max``) for the executor role. Omit to use the framework default.

    Returns:
        The typed ``RunResult`` — carries ``status``, ``output``, ``cost_usd``,
        ``cache_hit_ratio``, ``turns``, ``trace_path``, and gate ``evidence``.
    """
    from fable import FableConfig, check, run
    from fable.config import RolePolicy
    from fable.tools import fs_tools, shell_tool, think_tool

    root = Path(workdir).resolve()
    tools = [*fs_tools(root=root), think_tool()]
    if allow_shell:
        allowlist = [name.strip() for name in allow_shell.split(",") if name.strip()]
        tools.append(shell_tool(allowlist=allowlist))

    gate = check.command(verify) if verify else None

    config = None
    if effort:
        base = FableConfig()
        exec_policy = base.roles["executor"]
        config = base.with_overrides(
            roles={**base.roles, "executor": RolePolicy(
                tier=exec_policy.tier,
                effort=effort,  # type: ignore[arg-type]
                max_tokens=exec_policy.max_tokens,
                thinking=exec_policy.thinking,
            )},
        )

    return run(
        task,
        tools=tools,
        verify=gate,
        budget_usd=budget,
        max_turns=max_turns,
        config=config,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_agent.py",
        description="Run a verified, budget-capped, fully-traced FABLE agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("task", help="What the agent should do, in plain language.")
    parser.add_argument(
        "--dir", default=".", metavar="PATH",
        help="Directory the agent's file tools are jailed to (default: current).",
    )
    parser.add_argument(
        "--verify", metavar="CMD",
        help='Command that must exit 0 for the run to grade "ok" (e.g. "pytest -q").',
    )
    parser.add_argument(
        "--budget", type=float, default=2.0, metavar="USD",
        help="Hard USD ceiling for the whole run (default: 2.00).",
    )
    parser.add_argument(
        "--max-turns", type=int, default=None, metavar="N",
        help="Optional cap on tool-turns (a second stop rail).",
    )
    parser.add_argument(
        "--allow-shell", metavar="LIST",
        help='Comma-separated executables the agent may shell out to (e.g. "pytest,git").',
    )
    parser.add_argument(
        "--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None,
        help="Executor effort override (default: framework default).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set. Export your key first:\n"
            "    export ANTHROPIC_API_KEY=sk-ant-...",
            file=sys.stderr,
        )
        return 2

    print(f"▸ task    : {args.task}")
    print(f"▸ workdir : {Path(args.dir).resolve()}")
    print(f"▸ verify  : {args.verify or '(none — best status is ok_unverified)'}")
    print(f"▸ budget  : ${args.budget:.2f}")
    print("─" * 60)

    result = run_task(
        args.task,
        workdir=args.dir,
        verify=args.verify,
        budget=args.budget,
        max_turns=args.max_turns,
        allow_shell=args.allow_shell,
        effort=args.effort,
    )

    print("─" * 60)
    print(f"status         : {result.status}")
    print(f"cost           : ${result.cost_usd:.4f}")
    print(f"cache hit ratio: {result.cache_hit_ratio:.0%}")
    print(f"tool turns     : {result.turns}")
    print(f"trace          : {result.trace_path}")
    if result.evidence:
        print("evidence       :")
        for item in result.evidence:
            print(f"  - {item.name}: {item.detail}")
    print()
    print(result.output)

    return 0 if result.status in _SUCCESS_STATUSES else 1


if __name__ == "__main__":
    raise SystemExit(main())
