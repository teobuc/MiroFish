"""FABLE trace: append-only JSONL of every loop action, plus the readers.

The trace is a product surface, not a debug log (docs/01-architecture.md
section 6). One :class:`TraceEvent` per loop action, written host-side at
zero context cost. The file is append-only by contract: context clearing and
compaction swap the *projection* the model sees; the JSONL keeps every byte,
which is what makes resume, fork, and audit possible.

Wire format -- one JSON object per line:

    {"timestamp": "...", "elapsed_seconds": 1.23, "turn": 4,
     "action": "model_turn", "role": "executor", "details": {...}}

The key names ``timestamp`` / ``elapsed_seconds`` / ``action`` / ``details``
deliberately match MiroFish's ``ReportLogger`` JSONL so that tooling built
for those fields can read FABLE traces too; ``turn`` and ``role`` are the
FABLE additions. (If you rely on a specific viewer, verify it against one
real trace -- shared field names are a design goal, not a certification.)

Event vocabulary (emitted by ``loop.py`` and ``tools.py``):

    run_start | model_turn | tool_call | tool_result | gate_check |
    rail_trip | pressure | checkpoint | escalation | run_end

:func:`detect_failures` runs seven deterministic detectors over a trace --
no LLM attribution, on purpose: post-hoc model blame assignment measures
53.5% agent-level / 14.2% step-level accuracy (Zhang et al. 2024), which is
unusable. Each detector documents the exact trace signal it fires on.

This module imports nothing but the standard library.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

__all__ = [
    "TraceEvent",
    "TraceWriter",
    "TraceReader",
    "detect_failures",
]

_OK_STATUSES = ("ok", "ok_unverified")
_STALL_REPEAT_THRESHOLD = 3  # identical (tool, args_hash) pairs => loop/stall


@dataclass(frozen=True)
class TraceEvent:
    """One loop action. ``detail`` carries the event-specific payload.

    ``event`` is one of the vocabulary strings in the module docstring;
    ``role`` is the agent role that produced the action (empty for host-only
    events emitted outside a role context, e.g. tool_result).
    """

    ts: str                      # ISO-8601 UTC timestamp
    elapsed_seconds: float       # seconds since run start
    turn: int                    # tool-turn counter at emit time
    event: str                   # action name (see module docstring)
    role: str = ""
    detail: dict = field(default_factory=dict)

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "timestamp": self.ts,
                "elapsed_seconds": self.elapsed_seconds,
                "turn": self.turn,
                "action": self.event,
                "role": self.role,
                "details": self.detail,
            },
            ensure_ascii=False,
            default=str,
        )

    @classmethod
    def from_json_line(cls, line: str) -> "TraceEvent":
        data = json.loads(line)
        return cls(
            ts=str(data.get("timestamp") or data.get("ts") or ""),
            elapsed_seconds=float(data.get("elapsed_seconds") or 0.0),
            turn=int(data.get("turn") or 0),
            event=str(data.get("action") or data.get("event") or ""),
            role=str(data.get("role") or ""),
            detail=dict(data.get("details") or data.get("detail") or {}),
        )


class TraceWriter:
    """Append-only JSONL writer. Never rewrites; never crashes the loop.

    ``on_event`` (optional) is called with every event after it is written --
    the hook the ``run(on_event=...)`` surface exposes for live dashboards.
    A failing filesystem or hook downgrades tracing, it never kills a run:
    observability must not be a new failure mode.
    """

    def __init__(
        self,
        path: Path | str,
        on_event: Callable[[TraceEvent], None] | None = None,
    ):
        self.path = Path(path)
        self._on_event = on_event
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: TraceEvent) -> None:
        """Append one event (one line, flushed) and invoke the hook."""
        line = event.to_json_line()
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass  # tracing is best-effort by contract
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001 -- a bad hook must not kill the run
                pass


class TraceReader:
    """Turns a trace JSONL into the reports operations actually needs.

    Reads are lazy and tolerant: unparseable lines are skipped rather than
    raised, because a reader that dies on one corrupt line cannot audit the
    crash that produced it.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def events(self) -> Iterator[TraceEvent]:
        """Yield every parseable TraceEvent, in file (= emission) order."""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield TraceEvent.from_json_line(line)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue

    # ------------------------------------------------------------------ #
    # reports

    def cost_by_role(self) -> dict[str, float]:
        """USD per role, summed from ``model_turn`` events -- where the money
        went, and the number that makes the ~15x multi-agent multiplier
        visible."""
        costs: dict[str, float] = {}
        for event in self.events():
            if event.event != "model_turn":
                continue
            cost = event.detail.get("cost_usd")
            if cost is None:
                continue
            role = event.role or "?"
            costs[role] = round(costs.get(role, 0.0) + float(cost), 6)
        return costs

    def cache_hit_series(self) -> list[float]:
        """Rolling cache-hit ratio after each model call. A dip marks the
        exact call where the prefix mutated -- go find the timestamp."""
        series: list[float] = []
        for event in self.events():
            if event.event != "model_turn":
                continue
            ratio = event.detail.get("cache_hit_ratio")
            if ratio is not None:
                series.append(float(ratio))
        return series

    def evidence_for_claim(self, tool_use_id: str) -> dict | None:
        """Ground truth for one tool call -- the interactive claim audit.

        Returns the ``tool_result`` event detail (tool, args_hash,
        result_hash, exit_code, is_error) for ``tool_use_id``, or None if
        the trace never recorded that call.
        """
        for event in self.events():
            if (
                event.event == "tool_result"
                and event.detail.get("tool_use_id") == tool_use_id
            ):
                return dict(event.detail)
        return None


# --------------------------------------------------------------------------- #
# Failure-mode detectors (docs/04-verification.md section 7.1)


def detect_failures(reader: TraceReader) -> list[dict]:
    """Run the seven deterministic failure-mode detectors over one trace.

    Returns a list of findings, each ``{"mode": <name>, "detail": <str>}``,
    empty when nothing fires. Detectors key on observable trace patterns
    only -- exact signals below; no model is consulted:

    1.  ``fabricated_status`` -- a gate_check failed a ``claim:*`` audit
        entry: a done/passing/created/fixed claim whose cited evidence shows
        an error, a nonzero exit, or no ledger record at all.
    2.  ``premature_completion`` -- the run ended ``ok_unverified`` after a
        plan gate ran: blueprint steps were planned, but no completion gate
        ever verified them.
    3.  ``scope_drift`` -- a ``diff_scope`` gate failure reporting
        out-of-scope file touches.
    4.  ``test_tampering`` -- a ``diff_scope`` gate failure reporting
        implementer-role changes to test/CI files.
    5.  ``verification_skipping`` -- the run ended in an ok status without a
        single tool_result carrying an exit code: no decisive command ran
        before the completion claim was accepted.
    6.  ``loop_stall`` -- a ``stall`` rail trip, or the same
        ``(tool, args_hash)`` pair executed 3+ times.
    7.  ``error_laundering`` -- an ``is_error: true`` tool_result whose
        ``(tool, args_hash)`` never later succeeded, in a run that still
        ended with an ok status.
    """
    events = list(reader.events())
    findings: list[dict] = []

    def flag(mode: str, detail: str) -> None:
        findings.append({"mode": mode, "detail": detail})

    end_status = next(
        (e.detail.get("status") for e in reversed(events) if e.event == "run_end"),
        None,
    )
    gate_checks = [e for e in events if e.event == "gate_check"]
    tool_results = [e for e in events if e.event == "tool_result"]

    # 1. fabricated status ------------------------------------------------- #
    for event in gate_checks:
        claim_failures = [
            name for name in event.detail.get("failures", [])
            if str(name).startswith("claim:")
        ]
        if claim_failures:
            flag(
                "fabricated_status",
                "claim audit failed: " + "; ".join(map(str, claim_failures)),
            )

    # 2. premature completion ---------------------------------------------- #
    plan_gated = any(
        event.detail.get("checks") == ["plan_gate"] for event in gate_checks
    )
    completion_gated = any(
        event.detail.get("checks") != ["plan_gate"] for event in gate_checks
    )
    if end_status == "ok_unverified" and plan_gated and not completion_gated:
        flag(
            "premature_completion",
            "blueprint was planned (plan_gate passed) but the run ended "
            "ok_unverified with no completion gate over the steps",
        )

    # 3 + 4. scope drift / test tampering (diff_scope failure details) ------ #
    for event in gate_checks:
        for failure in event.detail.get("failure_details", []):
            if failure.get("name") != "diff_scope":
                continue
            detail = str(failure.get("detail", ""))
            if "out-of-scope" in detail:
                flag("scope_drift", detail)
            if "test/CI files modified" in detail:
                flag("test_tampering", detail)

    # 5. verification skipping ---------------------------------------------- #
    ran_decisive_command = any(
        event.detail.get("exit_code") is not None for event in tool_results
    )
    # An ok run with ZERO tool results is the strongest form of verification
    # skipping (nothing was exercised at all), so it must flag too -- do NOT
    # gate this on ``tool_results`` being non-empty.
    if end_status in _OK_STATUSES and not ran_decisive_command:
        flag(
            "verification_skipping",
            f"run ended {end_status!r} but no command with an exit code ran; "
            "the completion claim was never mechanically exercised",
        )

    # 6. loop / stall -------------------------------------------------------- #
    for event in events:
        if event.event == "rail_trip" and event.detail.get("rail") == "stall":
            flag("loop_stall", "stall rail tripped: identical tool calls repeating")
    pair_counts: dict[tuple[str, str], int] = {}
    for event in tool_results:
        pair = (str(event.detail.get("tool")), str(event.detail.get("args_hash")))
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
    for (tool_name, args_hash), count in pair_counts.items():
        if count >= _STALL_REPEAT_THRESHOLD:
            flag(
                "loop_stall",
                f"{tool_name} executed {count}x with identical args "
                f"(args_hash {args_hash[:12]})",
            )

    # 7. error laundering ------------------------------------------------------ #
    if end_status in _OK_STATUSES:
        last_outcome: dict[tuple[str, str], TraceEvent] = {}
        for event in tool_results:
            pair = (str(event.detail.get("tool")), str(event.detail.get("args_hash")))
            last_outcome[pair] = event
        unlaundered = [
            event for event in last_outcome.values() if event.detail.get("is_error")
        ]
        for event in unlaundered:
            flag(
                "error_laundering",
                f"{event.detail.get('tool')} (tool_use_id "
                f"{event.detail.get('tool_use_id')}) last failed with "
                f"is_error=true, was never retried to success, and the run "
                f"still ended {end_status!r}",
            )

    return findings
