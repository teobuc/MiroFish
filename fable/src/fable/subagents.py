"""FABLE subagents: fresh context windows as a resource, not an architecture.

There is exactly ONE engine in FABLE -- :class:`fable.loop.Agent`. A subagent
is that same engine started with a fresh context, a scoped tool list, and a
role-routed model+effort policy. Orchestration is therefore *data*: the
orchestrator gets multi-agent capability by registering the tool returned by
:func:`spawn_subagent_tool`, and ``loop.py`` never imports this module.

What a subagent buys (docs/03-orchestration.md):

- **Context isolation.** A researcher can burn 200K tokens reading sources;
  only its <=2K-token digest crosses back to the orchestrator.
- **Routing.** ``Brief.role`` picks the tier and effort per subtask -- a
  mechanical extraction runs on the cheap tier at low effort; judgment stays
  on the strong tier.
- **Structural attribution.** One responsibility per subagent means a failed
  digest names its owner. (Post-hoc LLM blame assignment is banned in FABLE:
  53.5% agent-level / 14.2% step-level accuracy is not attribution.)

What it costs: roughly 15x the tokens of answering inline, because the
subagent re-reads its own growing context every turn. The delegation
heuristics live in the :func:`spawn_subagent_tool` docstring, where the
orchestrating model can actually read them.
"""

from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from fable import prompts
from fable.client import FableClient, FrozenPrefix
from fable.config import FableConfig
from fable.loop import Agent
from fable.tools import Tool, fs_tools, tool

if TYPE_CHECKING:
    from fable.memory import Memory

# Approximate chars-per-token used ONLY to detect digest overflow cheaply.
# The authoritative counter is client.messages.count_tokens (never tiktoken);
# a server round-trip per spawn is not worth it for a soft cap, so we
# estimate conservatively and enforce the cap with a re-summarize.
_CHARS_PER_TOKEN = 4

# Role -> prompts/*.md file. Unknown roles fall back to the executor prompt.
_ROLE_PROMPTS: dict[str, str] = {
    "researcher": "researcher",
    "executor": "executor",
    "orchestrator": "orchestrator",
    "refuter": "verifier",
    "judge": "verifier",
    "mechanical": "executor",
}

_FALLBACK_SYSTEM = (
    "You are a focused subagent. Work strictly within the brief: objective, "
    "output format, tool guidance, boundaries. Ground every status claim in a "
    "tool result. Your final message is your digest -- keep it within the "
    "stated budget, list artifact file paths rather than pasting contents, "
    "include a 'Confidence: 0.x' line and an 'Open questions:' list."
)

_CONDENSE_SYSTEM = (
    "You compress agent digests. Preserve every fact, number, file path, the "
    "Confidence line, and the Open questions list; drop narration. Output "
    "only the compressed digest."
)

_CONFIDENCE_RE = re.compile(r"(?i)\bconfidence\b\s*[:=]?\s*([01](?:\.\d+)?)")
_PATHISH_RE = re.compile(r"(?<![\w.])((?:/|\./|~/)[\w.\-/]+)")


@dataclass(frozen=True)
class Brief:
    """The delegation contract. All four fields are REQUIRED and specific.

    Anthropic's multi-agent research system found vague briefs to be the
    dominant failure mode -- subagents duplicate work, wander out of scope,
    and return overlapping digests. The four fields force the orchestrator
    to pre-make the decisions the subagent should not be making:

    - ``objective``: what to find or build, with success visible from outside
    - ``output_format``: the exact shape of the digest coming back
    - ``tool_guidance``: which tools, and what to try first
    - ``boundaries``: what is explicitly out of scope

    ``role`` routes model+effort via the Router's role table (see
    ``config.DEFAULT_ROLES``): researcher -> mid/medium, mechanical ->
    cheap/low, executor -> strong/xhigh.
    """

    objective: str
    output_format: str
    tool_guidance: str
    boundaries: str
    role: str = "researcher"
    tools: Sequence[Tool] = ()

    def __post_init__(self) -> None:
        for name in ("objective", "output_format", "tool_guidance", "boundaries"):
            if not str(getattr(self, name)).strip():
                raise ValueError(
                    f"Brief.{name} is required and must be non-empty. Vague "
                    "briefs duplicate work -- pre-make this decision."
                )

    def render(self, digest_max_tokens: int) -> str:
        return (
            f"## Objective\n{self.objective}\n\n"
            f"## Output format\n{self.output_format}\n\n"
            f"## Tool guidance\n{self.tool_guidance}\n\n"
            f"## Boundaries (out of scope)\n{self.boundaries}\n\n"
            "## Digest contract (hard)\n"
            f"Your final message is the digest: at most {digest_max_tokens} "
            "tokens. Write full findings to files and list their ABSOLUTE "
            "paths under an 'Artifacts:' heading; never paste file contents "
            "into the digest. End with a 'Confidence: 0.x' line (0.0-1.0) and "
            "an 'Open questions:' bullet list (empty list is fine)."
        )


@dataclass(frozen=True)
class SubagentReport:
    """What crosses the context boundary: a digest and pointers, never the
    transcript. The sidechain transcript lives in its own JSONL at
    ``trace_path`` for audit."""

    digest: str
    artifact_paths: tuple[Path, ...]
    confidence: float
    open_questions: tuple[str, ...]
    cost_usd: float
    trace_path: Path


def _system_for(role: str) -> str:
    try:
        return prompts.load(_ROLE_PROMPTS.get(role, "executor"))
    except Exception:  # noqa: BLE001 -- missing prompt file must not kill a spawn
        return _FALLBACK_SYSTEM


def _parse_confidence(digest: str) -> float:
    match = _CONFIDENCE_RE.search(digest)
    if match is None:
        return 0.5  # unstated confidence is medium confidence, flagged as such
    return max(0.0, min(1.0, float(match.group(1))))


def _parse_open_questions(digest: str) -> tuple[str, ...]:
    questions: list[str] = []
    in_section = False
    for line in digest.splitlines():
        stripped = line.strip()
        if re.match(r"(?i)^#*\s*open questions?\b", stripped):
            in_section = True
            continue
        if in_section:
            if stripped.startswith(("-", "*")):
                item = stripped.lstrip("-* ").strip()
                if item and item.lower() not in ("none", "(none)", "n/a"):
                    questions.append(item)
            elif stripped and not stripped.startswith(("-", "*")):
                break  # section ended
    return tuple(questions)


def _parse_artifacts(digest: str) -> tuple[Path, ...]:
    """Collect path-shaped strings that actually exist on disk. Existence is
    the filter: a digest can claim any path; the report only carries real
    ones."""
    seen: dict[Path, None] = {}
    for raw in _PATHISH_RE.findall(digest):
        candidate = Path(raw.rstrip(".,;:)")).expanduser()
        if candidate.exists() and candidate.is_file():
            seen[candidate.resolve()] = None
    return tuple(seen)


def _condense(digest: str, max_tokens: int, config: FableConfig) -> tuple[str, float]:
    """One cheap-tier re-summarize -- the single enforcement mechanism for the
    digest cap. Returns (condensed_digest, cost_usd)."""
    client = FableClient(config)
    prefix = FrozenPrefix.build(_CONDENSE_SYSTEM, ())
    turn = client.call(
        prefix=prefix,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Compress this digest to at most {max_tokens} tokens. Keep "
                    "all paths, the Confidence line, and Open questions.\n\n"
                    + digest
                ),
            }
        ],
        role="mechanical",
    )
    if turn.stop_reason == "refusal" or not turn.text.strip():
        # Fail safe: hard-truncate rather than ship an over-budget digest.
        return digest[: max_tokens * _CHARS_PER_TOKEN], turn.cost_usd
    return turn.text, turn.cost_usd


def spawn(
    brief: Brief,
    *,
    config: FableConfig | None = None,
    memory: "Memory | None" = None,
) -> SubagentReport:
    """Run ONE subagent: the same ``loop.Agent``, fresh context, scoped tools,
    role-routed model+effort. Blocking; use :func:`fan_out` for parallelism.

    Cost honesty: expect roughly **15x the tokens** of answering the same
    question inline, because the subagent re-reads its whole (fresh) context
    every turn. Delegate when the subtask needs its own context budget or its
    own tier -- not to make the transcript look organized.

    The subagent's full transcript goes to its own JSONL (``trace_path``);
    full findings go to files; only ``digest`` (<=
    ``config.subagent_digest_max_tokens``, enforced by one cheap-tier
    re-summarize on overflow) plus artifact PATHS cross the boundary.
    """
    config = config or FableConfig()
    agent = Agent(
        system=_system_for(brief.role),
        tools=tuple(brief.tools),
        verify=None,  # the ORCHESTRATOR's gate judges the merged result
        config=config,
        role=brief.role,
        memory=memory,
    )
    result = agent.run(brief.render(config.subagent_digest_max_tokens))

    digest = result.output
    cost = result.cost_usd
    if result.status not in ("ok", "ok_unverified"):
        digest = (
            f"SUBAGENT TERMINATED with status={result.status} before a clean "
            f"digest. Partial output follows.\n\n{digest}"
        )
    if len(digest) > config.subagent_digest_max_tokens * _CHARS_PER_TOKEN:
        digest, condense_cost = _condense(
            digest, config.subagent_digest_max_tokens, config
        )
        cost += condense_cost

    return SubagentReport(
        digest=digest,
        artifact_paths=_parse_artifacts(digest),
        confidence=_parse_confidence(digest) if result.status in ("ok", "ok_unverified") else 0.0,
        open_questions=_parse_open_questions(digest),
        cost_usd=cost,
        trace_path=result.trace_path,
    )


def fan_out(
    briefs: Sequence[Brief],
    *,
    max_parallel: int = 4,
    config: FableConfig | None = None,
) -> list[SubagentReport]:
    """Spawn several subagents concurrently (threads; sync everywhere else).

    Order of results matches order of briefs. A subagent that raises becomes
    a zero-confidence error report instead of killing its siblings -- the
    orchestrator decides whether a missing digest is fatal.

    ``max_parallel`` caps concurrency: each live subagent holds a streaming
    connection and burns rate limit; 4 is a sane default for one API key.
    """
    config = config or FableConfig()

    def safe(brief: Brief) -> SubagentReport:
        try:
            return spawn(brief, config=config)
        except Exception as exc:  # noqa: BLE001 -- isolate sibling failures
            return SubagentReport(
                digest=f"SUBAGENT FAILED before completion: {exc!r}",
                artifact_paths=(),
                confidence=0.0,
                open_questions=(f"re-run this brief: {brief.objective[:100]}",),
                cost_usd=0.0,
                trace_path=Path(os.devnull),
            )

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        return list(pool.map(safe, briefs))


def spawn_subagent_tool(
    config: FableConfig | None = None,
    *,
    reports: list[SubagentReport] | None = None,
    subagent_tools: Sequence[Tool] | None = None,
) -> Tool:
    """Package :func:`spawn` as a Tool so the ORCHESTRATING MODEL delegates.

    This is how FABLE does multi-agent: the user registers this tool on an
    orchestrator Agent explicitly (``loop.py`` never imports this module),
    and orchestration becomes ordinary tool use -- parallel-safe, so the
    model can spawn several subagents in one assistant turn and they run
    concurrently.

    ``reports`` (optional): pass a list to collect every SubagentReport for
    host-side cost accounting -- the honest way to show the ~15x multiplier.
    ``subagent_tools``: tool list given to every spawned subagent (defaults
    to read-only ``fs_tools()``).
    """
    resolved_config = config or FableConfig()
    collected = reports if reports is not None else []
    lock = threading.Lock()
    default_tools = tuple(subagent_tools) if subagent_tools is not None else None

    @tool(parallel_safe=True, name="spawn_subagent")
    def spawn_subagent(
        objective: str,
        output_format: str,
        tool_guidance: str,
        boundaries: str,
        role: str = "researcher",
    ) -> str:
        """Delegate one scoped subtask to a fresh subagent with its own context window.

        Cost: a subagent costs roughly 15x the tokens of doing the work inline. Delegate only when the subtask needs its own context budget or its own model tier. Calibration: use 1 subagent with 3-10 tool calls for simple fact-finding; 2-4 subagents for comparisons; 10+ only for genuinely decomposable research. Issue multiple spawn_subagent calls in ONE turn to run them in parallel.

        All four brief fields are mandatory and must be specific -- vague briefs duplicate work:
        objective: what to find or build, with success observable from outside.
        output_format: the exact shape of the digest you want back.
        tool_guidance: which tools to prefer and what to try first.
        boundaries: what is explicitly OUT of scope.
        role: researcher (mid tier, default) | mechanical (cheap tier, extraction/formatting) | executor (strong tier, judgment work).

        Returns the subagent's digest (<=2000 tokens) with artifact file paths, a confidence score, and open questions. Read artifacts with your file tools if you need details.
        """
        brief = Brief(
            objective=objective,
            output_format=output_format,
            tool_guidance=tool_guidance,
            boundaries=boundaries,
            role=role,
            tools=default_tools if default_tools is not None else tuple(fs_tools()),
        )
        report = spawn(brief, config=resolved_config)
        with lock:
            collected.append(report)
        artifact_lines = "\n".join(f"- {p}" for p in report.artifact_paths) or "- (none)"
        question_lines = (
            "\n".join(f"- {q}" for q in report.open_questions) or "- (none)"
        )
        return (
            f"[subagent role={role} cost_usd={report.cost_usd:.4f} "
            f"confidence={report.confidence:.2f} trace={report.trace_path}]\n\n"
            f"{report.digest}\n\n"
            f"Artifacts:\n{artifact_lines}\n\n"
            f"Open questions:\n{question_lines}"
        )

    return spawn_subagent
