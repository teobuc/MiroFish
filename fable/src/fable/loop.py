"""The FABLE core agent loop -- the only engine in the framework.

State machine (docs/01-architecture.md):

    INIT -> [ ASSEMBLE -> CALL -> DISPATCH -> (TOOLS -> ASSEMBLE) | GATE ]
         -> terminal RunResult

The loop is deliberately trivial. Every production harness studied keeps the
loop dumb and puts the intelligence in the model and the discipline in the
control plane: stop rails, gates, the router, the ledger, and the trace all
run host-side and cost zero context tokens.

The load-bearing difference from a chat loop: ``end_turn`` is a CLAIM, not
completion. When the model says it is done, the harness runs the gate --
claim audit, fresh-process mechanical checks, judged rubrics, optional
refuter -- and only the harness sets done. With ``verify=None`` the run can
only ever finish as ``"ok_unverified"``: the type system nags, and the nag is
the pedagogy.

Orchestration is data, not a second engine: subagents re-enter this same loop
via a registered tool (``fable.subagents.spawn_subagent_tool()``); this module
never imports ``subagents``.
"""

from __future__ import annotations

import datetime as _dt
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal, Sequence

from fable import prompts
from fable.client import FableClient, FrozenPrefix, ModelTurn, UsageLedger
from fable.config import Budget, EscalationExhausted, FableConfig, RolePolicy, Router
from fable.memory import Checkpoint, Memory, redact
from fable.tools import Tool, ToolRegistry, execute
from fable.trace import TraceEvent, TraceWriter
from fable.verify import Check, Evidence, Gate, GateContext

Status = Literal[
    "ok", "ok_unverified", "failed_gate", "refusal", "max_turns",
    "budget_exceeded", "timeout", "stalled", "overflow",
    "checkpointed", "escalated",
]

_REPORT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "tool_use_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "tool_use_ids"],
                "additionalProperties": False,
            },
        },
        "artifacts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "claims", "artifacts"],
    "additionalProperties": False,
}


@dataclass
class Step:
    """One Blueprint step. ``status`` is HARNESS-owned: the model proposes
    steps once; it cannot re-plan executed work (the AutoGPT fix)."""

    id: str
    action: str
    verifier: str                     # shell command, or the literal "judgment"
    status: Literal["pending", "done", "failed"] = "pending"
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class Blueprint:
    """An optional, typed plan. Its per-step ``verifier`` field doubles as the
    routing policy (command -> mid tier + verify-and-retry; "judgment" ->
    strong tier). One-step blueprints are legal and encouraged for trivial
    tasks; more than ``config.blueprint_max_steps`` steps fails the plan gate.
    """

    steps: list[Step]

    JSON_SCHEMA: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "action": {"type": "string"},
                        "verifier": {"type": "string"},
                    },
                    "required": ["id", "action", "verifier"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["steps"],
        "additionalProperties": False,
    }

    def to_markdown(self) -> str:
        lines = ["# Blueprint", ""]
        for step in self.steps:
            lines.append(
                f"- [{'x' if step.status == 'done' else ' '}] "
                f"**{step.id}**: {step.action}  \n"
                f"  verifier: `{step.verifier}`"
            )
        return "\n".join(lines)

    @classmethod
    def from_json(cls, data: dict) -> "Blueprint":
        steps = [
            Step(id=str(s["id"]), action=str(s["action"]), verifier=str(s["verifier"]))
            for s in data.get("steps", [])
        ]
        return cls(steps=steps)


@dataclass(frozen=True)
class RunResult:
    """The typed terminal of every run. No run exits without one.

    ``status == "ok"`` means the gate passed with evidence attached.
    ``"ok_unverified"`` means nobody checked -- an honest label, not a pass.
    Every other status names the rail or failure that ended the run.
    """

    status: Status
    output: str
    evidence: tuple[Evidence, ...]
    cost_usd: float
    usage: dict
    cache_hit_ratio: float
    turns: int
    trace_path: Path
    checkpoint_path: Path | None
    session_id: str


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


class Agent:
    """A configured agent: frozen prefix + tools + gate + policy.

    Tier-2 surface. Beginners never construct one -- :func:`run` does.

    Hooks (all optional constructor kwargs, all host-side):

    - ``pre_call(messages) -> messages``: last look before each model call
    - ``post_tool(name, args, result) -> result``: reshape one tool result
    - ``on_checkpoint(cp) -> None``: observe checkpoints as they are written
    """

    def __init__(
        self,
        *,
        system: str | None = None,
        tools: Sequence[Tool] = (),
        verify: Gate | Check | Sequence[Check] | None = None,
        config: FableConfig | None = None,
        role: str = "orchestrator",
        memory: Memory | None = None,
        pre_call: Callable[[list[dict]], list[dict]] | None = None,
        post_tool: Callable[[str, dict, str], str] | None = None,
        on_checkpoint: Callable[[Checkpoint], None] | None = None,
    ):
        self._config = config or FableConfig()
        self._system = system
        self._tools = tuple(tools)
        self._gate = _coerce_gate(verify, self._config)
        self._role = role
        self._memory = memory
        self._pre_call = pre_call
        self._post_tool = post_tool
        self._on_checkpoint = on_checkpoint

    # ------------------------------------------------------------------ #

    def run(
        self,
        task: str,
        *,
        budget: Budget | None = None,
        plan: bool = False,
        blueprint: Blueprint | None = None,
        on_event: Callable[[TraceEvent], None] | None = None,
    ) -> RunResult:
        """Execute the state machine to a typed terminal RunResult."""
        config = (
            self._config if budget is None else self._config.with_overrides(budget=budget)
        )
        rails = config.budget
        router = Router(config)
        policy = router.policy(self._role)
        tier = config.tiers[policy.tier]

        session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        scratch = Path(config.scratch_dir)
        trace_path = Path(config.scratch_dir).parent / "traces" / f"{session_id}.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        writer = TraceWriter(trace_path, on_event)
        ledger = UsageLedger()
        client = FableClient(config, ledger)
        start = time.monotonic()
        turn_count = 0

        def emit(event: str, detail: dict | None = None, role: str = "") -> None:
            writer.emit(
                TraceEvent(
                    ts=_now_iso(),
                    elapsed_seconds=round(time.monotonic() - start, 3),
                    turn=turn_count,
                    event=event,  # type: ignore[arg-type]
                    role=role or self._role,
                    detail=detail or {},
                )
            )

        def finish(
            status: Status,
            output: str,
            evidence: tuple[Evidence, ...] = (),
            checkpoint_path: Path | None = None,
        ) -> RunResult:
            emit("run_end", {"status": status, "cost_usd": ledger.cost_usd})
            return RunResult(
                status=status,
                output=output,
                evidence=evidence,
                cost_usd=ledger.cost_usd,
                usage=ledger.totals,
                cache_hit_ratio=ledger.cache_hit_ratio,
                turns=turn_count,
                trace_path=trace_path,
                checkpoint_path=checkpoint_path,
                session_id=session_id,
            )

        # ----- INIT ---------------------------------------------------- #
        registry = ToolRegistry(self._tools)
        system = self._system if self._system is not None else prompts.load("executor")
        prefix = FrozenPrefix.build(system, list(registry))
        registry.freeze()
        emit("run_start", {"task": task[:500], "prefix_digest": prefix.digest})

        if plan and blueprint is None:
            blueprint = self._author_blueprint(task, client, config, emit)
        if blueprint is not None:
            self._persist_blueprint(blueprint, config)

        first_message = self._first_user_message(task, config, rails, blueprint)
        messages: list[dict] = [{"role": "user", "content": first_message}]

        # ----- run-scoped mutable loop state --------------------------- #
        overflow_recoveries = 0
        gate_retries = 0
        escalated = False
        warned_pressure = False
        context_tokens = 0
        recent_calls: deque[tuple[str, str]] = deque(maxlen=config.stall_window)

        # ----- ASSEMBLE / CALL / DISPATCH loop -------------------------- #
        while True:
            # RAILS (host-side, every iteration, zero context cost)
            elapsed = time.monotonic() - start
            if turn_count >= rails.max_turns:
                emit("rail_trip", {"rail": "max_turns", "limit": rails.max_turns})
                return finish("max_turns", "Tool-turn limit reached.")
            if elapsed > rails.max_wall_seconds:
                emit("rail_trip", {"rail": "wall_clock", "elapsed": elapsed})
                return finish("timeout", "Wall-clock limit reached.")
            projected = ledger.cost_usd + ledger.project_next_call_usd(
                context_tokens, tier, config.projection_output_allowance
            )
            if projected > rails.max_usd:
                emit(
                    "rail_trip",
                    {"rail": "budget_usd", "spent": ledger.cost_usd, "projected": projected},
                )
                return finish(
                    "budget_exceeded",
                    f"Budget rail: ${ledger.cost_usd:.2f} spent, next call projected "
                    f"to exceed ${rails.max_usd:.2f}.",
                )
            if _is_stalled(recent_calls, config):
                emit("rail_trip", {"rail": "stall", "window": list(recent_calls)})
                return finish(
                    "stalled",
                    "Stall detector: the same tool call is repeating without progress.",
                )

            # CONTEXT PRESSURE (measured, not guessed)
            pressure = context_tokens / tier.context_window
            if pressure >= rails.checkpoint_pct and self._memory is not None:
                cp_path = self._write_checkpoint(task, ledger, emit)
                if cp_path is not None:
                    return finish(
                        "checkpointed",
                        "Context pressure reached checkpoint threshold; state "
                        "saved. Respawn a fresh session from "
                        "memory.resume_prompt().",
                        checkpoint_path=cp_path,
                    )
                # Write failed: do NOT claim the state was saved. There is no
                # checkpoint to resume from, so end on a non-success status.
                return finish(
                    "overflow",
                    "Context pressure reached the checkpoint threshold but the "
                    "checkpoint write FAILED; state was NOT saved and the run "
                    "cannot be safely resumed from memory.",
                )
            if pressure >= rails.edit_pct:
                reclaimed = _clear_old_tool_results(messages, config, scratch)
                if reclaimed:
                    emit("pressure", {"action": "cleared_tool_results", "approx_chars": reclaimed})
            elif pressure >= rails.warn_pct and not warned_pressure:
                # Mid-conversation system message: operator channel that
                # preserves the cached prefix. Follows a user turn by
                # construction (we only reach CALL with a trailing user msg).
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Context is filling up. Prefer terse output, avoid "
                            "re-reading files already in context, and spill large "
                            "results to scratch files instead of printing them."
                        ),
                    }
                )
                warned_pressure = True
                emit("pressure", {"action": "operator_warning", "pressure": round(pressure, 3)})

            # CALL
            call_messages = self._pre_call(messages) if self._pre_call else messages
            turn = client.call(
                prefix=prefix, messages=call_messages, role=self._role, policy=policy
            )
            context_tokens = (
                turn.usage["input_tokens"]
                + turn.usage["cache_read_input_tokens"]
                + turn.usage["cache_creation_input_tokens"]
                + turn.usage["output_tokens"]
            )
            emit(
                "model_turn",
                {
                    "stop_reason": turn.stop_reason,
                    "tokens": dict(turn.usage),
                    "cost_usd": round(turn.cost_usd, 6),
                    "cache_hit_ratio": round(ledger.cache_hit_ratio, 4),
                },
            )

            # DISPATCH -- stop_reason checked BEFORE touching content:
            # refusal content may be empty, and content[0] would crash.
            if turn.stop_reason == "refusal":
                return finish(
                    "refusal",
                    f"Model refused. stop_details={turn.stop_details!r}",
                )

            if turn.stop_reason == "max_tokens":
                overflow_recoveries += 1
                if overflow_recoveries > config.max_output_recoveries:
                    return finish(
                        "overflow",
                        "Output repeatedly truncated at max_tokens; giving up.",
                    )
                messages.append({"role": "assistant", "content": list(turn.content)})
                messages.append(
                    {
                        "role": "user",
                        "content": "[operator] Output was truncated at the token "
                        "limit. Continue from where you stopped.",
                    }
                )
                continue

            if turn.stop_reason == "tool_use":
                # TOOLS: parallel-safe concurrently, mutators serialized;
                # ground truth to the Evidence Ledger pre-shaping; ALL
                # results in exactly ONE user message.
                turn_count += 1
                tool_calls = turn.tool_calls
                for call in tool_calls:
                    emit(
                        "tool_call",
                        {"tool": call["name"], "tool_use_id": call["id"]},
                    )
                    recent_calls.append(
                        (call["name"], _args_signature(call["input"]))
                    )
                results = execute(
                    tool_calls,
                    registry,
                    scratch=scratch,
                    ledger=ledger,
                    trace=writer,
                    cap_chars=config.tool_result_cap_chars,
                )
                if self._post_tool is not None:
                    by_id = {c["id"]: c for c in tool_calls}
                    for block in results:
                        call = by_id.get(block["tool_use_id"])
                        if call is not None:
                            block["content"] = self._post_tool(
                                call["name"], call["input"], block["content"]
                            )
                messages.append({"role": "assistant", "content": list(turn.content)})
                messages.append({"role": "user", "content": results})
                continue

            # end_turn: CANDIDATE-DONE. The model claims completion; the
            # gate decides.
            if self._gate is None:
                return finish("ok_unverified", turn.text)

            messages.append({"role": "assistant", "content": list(turn.content)})
            report = self._request_completion_report(
                client, prefix, messages, policy, config
            )
            ctx = GateContext(
                workspace=Path.cwd(),
                ledger=ledger,
                client=client,
                final_report=report,
                blueprint=blueprint,
            )
            gate_result = self._gate.run(ctx)
            emit(
                "gate_check",
                {
                    "passed": gate_result.passed,
                    "checks": [e.name for e in gate_result.evidence],
                    "failures": [e.name for e in gate_result.failures],
                    # Per-failure ground truth for trace.detect_failures
                    # (scope drift / test tampering read diff_scope details).
                    "failure_details": [
                        {
                            "name": e.name,
                            "detail": e.detail,
                            "exit_code": e.exit_code,
                        }
                        for e in gate_result.failures
                    ],
                },
            )
            if gate_result.passed:
                # Gate passed with evidence: the blueprint's steps are now
                # genuinely done. Flip harness-owned step status and persist
                # passes:true through the ONLY sanctioned path
                # (Memory.mark_passed) so feature_list.json stops lying. The
                # model never marks its own work done.
                if blueprint is not None:
                    self._mark_blueprint_passed(blueprint, gate_result.evidence)
                summary = (report or {}).get("summary") or turn.text
                status: Status = "ok"
                return finish(status, summary, evidence=gate_result.evidence)

            gate_retries += 1
            if gate_retries > self._gate.max_retries:
                if self._gate.on_exhaust == "escalate" and not escalated:
                    try:
                        policy = router.escalate(policy)
                        tier = config.tiers[policy.tier]
                        escalated = True
                        gate_retries = 0
                        emit(
                            "escalation",
                            {"tier": policy.tier, "effort": policy.effort},
                        )
                    except EscalationExhausted:
                        return finish(
                            "failed_gate",
                            _failure_text(gate_result.failures),
                            evidence=gate_result.evidence,
                        )
                else:
                    terminal: Status = "escalated" if escalated else "failed_gate"
                    return finish(
                        terminal,
                        _failure_text(gate_result.failures),
                        evidence=gate_result.evidence,
                    )
            # Concrete failure evidence fed back verbatim; the model retries
            # against facts, not vibes.
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "The completion gate FAILED. Concrete evidence:\n"
                        + _failure_text(gate_result.failures)
                        + "\nAddress every failure, then finish again."
                    ),
                }
            )

    def resume(self, checkpoint: Path | str) -> RunResult:
        """Boot a fresh session from a checkpoint file.

        Fresh window + filesystem rediscovery beats lossy summarization for
        very long runs: the checkpoint carries goal, decisions, and next
        steps; everything else reconstructs from disk and git.
        """
        cp_path = Path(checkpoint)
        cp_text = cp_path.read_text(encoding="utf-8")
        litany = ""
        if self._memory is not None:
            try:
                litany = self._memory.resume_prompt()
            except Exception:  # noqa: BLE001 -- resume must not die on memory quirks
                litany = ""
        task = (
            "Resume the run described by this checkpoint. Re-verify state from "
            "the filesystem before acting.\n\n"
            f"{litany}\n\n--- checkpoint ({cp_path}) ---\n{cp_text}"
        )
        return self.run(task)

    # ------------------------------------------------------------------ #

    def _first_user_message(
        self,
        task: str,
        config: FableConfig,
        rails: Budget,
        blueprint: Blueprint | None,
    ) -> str:
        """ALL dynamic content lives here -- never in the frozen prefix."""
        parts = [
            f"<task>\n{task}\n</task>",
            "<context>",
            f"date: {_dt.date.today().isoformat()}",
            f"budget: ${rails.max_usd:.2f}, {rails.max_turns} tool turns, "
            f"{int(rails.max_wall_seconds)}s wall clock",
            "</context>",
        ]
        if self._memory is not None:
            state_dir = Path(getattr(self._memory, "root", config.memory_root)) / "state"
            try:
                # Memory.__init__ always mkdir's state/, so its mere existence
                # proves nothing -- a fresh run would get the resume litany.
                # Gate resume on a prior session's actual persisted content: a
                # checkpoint file, or a non-empty progress journal. (plan.md /
                # feature_list.json are written at THIS run's start when a
                # blueprint is present, so they are not a resume signal.)
                progress = state_dir / "progress.md"
                resuming = any(state_dir.glob("checkpoint-*.md")) or (
                    progress.exists()
                    and progress.read_text(encoding="utf-8").strip() != ""
                )
                if resuming:
                    parts.append(self._memory.resume_prompt())
                else:
                    index = self._memory.index()
                    if index.strip():
                        parts.append(f"<memory_index>\n{index}\n</memory_index>")
            except Exception:  # noqa: BLE001 -- memory is an aid, not a dependency
                pass
        if blueprint is not None:
            parts.append(
                "<blueprint>\n"
                + blueprint.to_markdown()
                + "\n</blueprint>\n"
                "Step status is harness-owned; execute steps, do not re-plan them."
            )
        return "\n\n".join(parts)

    def _author_blueprint(
        self,
        task: str,
        client: FableClient,
        config: FableConfig,
        emit: Callable[..., None],
    ) -> Blueprint:
        """Tier-2 planning: one structured call emits a Blueprint, then a
        mechanical plan gate checks it (every step has a verifier, step count
        within bounds, observable actions only). Tier 0/1 never gets here."""
        planner_system = prompts.load("planner")
        planner_prefix = FrozenPrefix.build(planner_system, [])
        # Fresh client, shared ledger: the planning call is control-plane and
        # uses its own prefix, which must not pin this role's run digest.
        planner_client = FableClient(config, client.ledger)
        data = planner_client.structured(
            prefix=planner_prefix,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"<task>\n{task}\n</task>\n\nEmit the Blueprint JSON. "
                        "One step is fine for a trivial task; never more than "
                        f"{config.blueprint_max_steps}."
                    ),
                }
            ],
            role="orchestrator",
            schema=Blueprint.JSON_SCHEMA,
        )
        blueprint = Blueprint.from_json(data)
        problems = []
        if not blueprint.steps:
            problems.append("blueprint has no steps")
        if len(blueprint.steps) > config.blueprint_max_steps:
            problems.append(
                f"{len(blueprint.steps)} steps exceeds the "
                f"{config.blueprint_max_steps}-step cap"
            )
        for step in blueprint.steps:
            if not step.verifier.strip():
                problems.append(f"step {step.id!r} has no verifier")
            if not step.action.strip():
                problems.append(f"step {step.id!r} has no observable action")
        if problems:
            raise ValueError("Plan gate failed: " + "; ".join(problems))
        emit("gate_check", {"passed": True, "checks": ["plan_gate"], "failures": []})
        return blueprint

    def _persist_blueprint(self, blueprint: Blueprint, config: FableConfig) -> None:
        """Write state/plan.md + state/feature_list.json. Only the harness
        (memory.mark_passed) ever flips ``passes`` to true."""
        root = Path(
            getattr(self._memory, "root", config.memory_root)
            if self._memory is not None
            else config.memory_root
        )
        state = root / "state"
        try:
            state.mkdir(parents=True, exist_ok=True)
            # Redact before writing, mirroring Memory._write: plan.md and
            # feature_list.json get committed and pasted into prompts, so a
            # secret-shaped token in an action/verifier must not land raw.
            (state / "plan.md").write_text(
                redact(blueprint.to_markdown()), encoding="utf-8"
            )
            import json as _json

            features = [
                {
                    "category": "blueprint",
                    "description": step.action,
                    "steps": [step.verifier],
                    "passes": False,
                }
                for step in blueprint.steps
            ]
            (state / "feature_list.json").write_text(
                redact(_json.dumps(features, indent=2)), encoding="utf-8"
            )
        except OSError:
            pass  # plan persistence is an aid; the in-memory blueprint rules

    def _mark_blueprint_passed(
        self, blueprint: Blueprint, evidence: tuple[Evidence, ...]
    ) -> None:
        """Gate passed with a blueprint active: mark the plan complete.

        Two effects, both harness-owned: (1) flip each in-memory Step to
        ``done`` and attach the gate's evidence ids; (2) persist ``passes:
        true`` in feature_list.json via ``Memory.mark_passed`` -- the ONLY
        sanctioned path that flips that flag. Persistence is best-effort (an
        aid, like _persist_blueprint): a filesystem hiccup updates in-memory
        status and lets the honest ``ok`` return stand.
        """
        evidence_ids = [e.name for e in evidence]
        for step in blueprint.steps:
            if step.status != "done":
                step.status = "done"
                step.evidence_ids = list(evidence_ids)
        if self._memory is None:
            return
        for step in blueprint.steps:
            try:
                self._memory.mark_passed(step.action, evidence_ids)
            except Exception:  # noqa: BLE001 -- feature-list drift must not
                pass  # undo a genuinely-passing run; in-memory status stands

    def _request_completion_report(
        self,
        client: FableClient,
        prefix: FrozenPrefix,
        messages: list[dict],
        policy: RolePolicy,
        config: FableConfig,
    ) -> dict | None:
        """Ask for the structured completion report the claim audit consumes.

        Every done/passing/created/fixed claim must cite tool_use_ids whose
        ledger evidence supports it; ungrounded claims fail the gate at zero
        token cost.
        """
        report_messages = list(messages) + [
            {
                "role": "user",
                "content": (
                    "Submit your completion report now. Every claim of work "
                    "done must cite the tool_use_ids whose output proves it. "
                    "Unproven work belongs in the summary as 'not verified'."
                ),
            }
        ]
        report_policy = RolePolicy(
            tier=policy.tier,
            effort=policy.effort,
            max_tokens=config.report_max_tokens,
            thinking=policy.thinking,
        )
        try:
            data = client.structured(
                prefix=prefix,
                messages=report_messages,
                role=self._role,
                schema=_REPORT_SCHEMA,
                policy=report_policy,
            )
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 -- a missing report just skips the audit
            return None

    def _write_checkpoint(
        self,
        task: str,
        ledger: UsageLedger,
        emit: Callable[..., None],
    ) -> Path | None:
        cp = Checkpoint(
            goal=task,
            decisions=(),
            files_touched=(),
            verified_done=(),
            open_issues=("run checkpointed under context pressure",),
            next_steps=(
                "re-run one smoke check to confirm workspace state",
                "consult state/plan.md and state/feature_list.json",
                "continue the next unfinished feature",
            ),
            lessons=(),
        )
        try:
            path = self._memory.checkpoint(cp) if self._memory is not None else None
        except Exception:  # noqa: BLE001 -- checkpointing must not crash the exit path
            path = None
        if self._on_checkpoint is not None:
            self._on_checkpoint(cp)
        emit("checkpoint", {"path": str(path) if path else None, "cost_usd": ledger.cost_usd})
        return path


# --------------------------------------------------------------------------- #
# Module-level helpers (host-side, zero context cost)


def _coerce_gate(
    verify: Gate | Check | Sequence[Check] | None, config: FableConfig
) -> Gate | None:
    """Accept the ``verify=`` shorthand forms and normalize to a Gate."""
    if verify is None:
        return None
    if isinstance(verify, Gate):
        return verify
    if isinstance(verify, Check):
        return Gate(checks=(verify,), max_retries=config.gate_max_retries)
    return Gate(checks=tuple(verify), max_retries=config.gate_max_retries)


def _args_signature(args: Any) -> str:
    import hashlib
    import json as _json

    return hashlib.sha256(
        _json.dumps(args, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _is_stalled(recent: deque, config: FableConfig) -> bool:
    """n-gram-style repetition over recent (tool, args_hash) pairs. The same
    call repeating with identical arguments is motion without progress."""
    if len(recent) < config.stall_window:
        return False
    counts: dict[tuple[str, str], int] = {}
    for signature in recent:
        counts[signature] = counts.get(signature, 0) + 1
    return max(counts.values()) >= config.stall_repeat_threshold


def _clear_old_tool_results(
    messages: list[dict], config: FableConfig, scratch: Path
) -> int:
    """Client-side projection clearing at edit_pct pressure.

    Keeps the last ``clear_keep_last`` tool_result-bearing user messages;
    older tool results collapse to a placeholder naming the spill path. Only
    proceeds when the reclaim is worth the 1.25x cache re-write that any
    projection edit costs -- the loop does the arithmetic, per config
    (``clear_min_reclaim_tokens``, approximated at 4 chars/token).

    The trace JSONL is append-only and untouched: clearing swaps the
    projection, never the history.
    """
    result_indices = [
        i
        for i, message in enumerate(messages)
        if message.get("role") == "user"
        and isinstance(message.get("content"), list)
        and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in message["content"]
        )
    ]
    clearable = result_indices[: -config.clear_keep_last]
    if not clearable:
        return 0
    chars_per_token = 4  # coarse, documented approximation
    reclaim_chars = 0
    for i in clearable:
        for block in messages[i]["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                reclaim_chars += len(str(block.get("content", "")))
    if reclaim_chars // chars_per_token < config.clear_min_reclaim_tokens:
        return 0  # not worth the cache re-write
    for i in clearable:
        for block in messages[i]["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                block["content"] = (
                    "[tool result cleared under context pressure; full output "
                    f"remains under {scratch}/ if it was spilled]"
                )
                block.pop("is_error", None)
    return reclaim_chars


def _failure_text(failures: Sequence[Evidence]) -> str:
    if not failures:
        return "Gate failed with no itemized evidence."
    lines = []
    for e in failures:
        line = f"- {e.name}: FAILED"
        if e.command:
            line += f" (command: {e.command}, exit {e.exit_code})"
        if e.output_tail:
            line += f"\n  output tail: {e.output_tail[-500:]}"
        if e.detail:
            line += f"\n  {e.detail}"
        lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Tier-0 front door


def run(
    task: str,
    *,
    tools: Sequence[Tool] = (),
    verify: Gate | Check | Sequence[Check] | None = None,
    config: FableConfig | None = None,
    budget_usd: float | None = None,
    max_turns: int | None = None,
    memory: Memory | None = None,
    system: str | None = None,
    on_event: Callable[[TraceEvent], None] | None = None,
) -> RunResult:
    """Tier-0 front door: a verified, budget-capped, traced agent in one call.

    Builds an :class:`Agent` and executes. The default system prompt is
    ``prompts/executor.md``. Pass ``verify=`` to get a real gate; without it
    the best possible status is ``"ok_unverified"`` -- deliberately.

    Example::

        from fable import run, check
        from fable.tools import fs_tools

        result = run(
            "Fix the failing test in tests/test_parser.py",
            tools=fs_tools(),
            verify=check.command("pytest -q"),
            budget_usd=2.0,
        )
        print(result.status, result.cost_usd, result.cache_hit_ratio)
    """
    config = config or FableConfig()
    overrides: dict[str, Any] = {}
    if budget_usd is not None:
        overrides["max_usd"] = budget_usd
    if max_turns is not None:
        overrides["max_turns"] = max_turns
    if overrides:
        base = config.budget
        config = config.with_overrides(
            budget=Budget(
                max_usd=overrides.get("max_usd", base.max_usd),
                max_turns=overrides.get("max_turns", base.max_turns),
                max_wall_seconds=base.max_wall_seconds,
                warn_pct=base.warn_pct,
                edit_pct=base.edit_pct,
                checkpoint_pct=base.checkpoint_pct,
            )
        )
    agent = Agent(
        system=system,
        tools=tools,
        verify=verify,
        config=config,
        role="executor",
        memory=memory,
    )
    return agent.run(task, on_event=on_event)
