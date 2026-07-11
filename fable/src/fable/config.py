"""FABLE configuration: model tiers, role policies, budgets, and the Router.

Everything tunable in FABLE flows through :class:`FableConfig`. The config is
frozen (immutable) on purpose: an immutable config means the compiled prompt
prefix is byte-stable for the whole run, which is what makes prompt caching
(0.1x reads) actually work. If you need different settings, build a new config
with :meth:`FableConfig.with_overrides` -- never mutate.

This module imports nothing but the standard library. It sits at the bottom of
the import DAG so every other module can depend on it without cycles.

Honesty note: nothing in this file makes the model smarter. Tiers, efforts,
and routing change *cost*, *latency*, and *reliability* (pass^k via
verify-and-retry) -- never per-step correctness.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal, Mapping

Effort = Literal["low", "medium", "high", "xhigh", "max"]

# Effort ladder, weakest to strongest. Used by Router.escalate.
_EFFORT_ORDER: tuple[Effort, ...] = ("low", "medium", "high", "xhigh", "max")

# Tier ladder, cheapest to strongest. Used by Router.escalate.
_TIER_ORDER: tuple[str, ...] = ("cheap", "mid", "strong")


class EscalationExhausted(RuntimeError):
    """Raised when Router.escalate is asked to climb past strong/max.

    This is a *feature*: escalation is gate-failure-driven and bounded. When
    the strongest configuration still fails the gate, the honest outcome is a
    typed failure the caller can see -- not an infinite retry loop.
    """


@dataclass(frozen=True)
class ModelTier:
    """One model tier with its pricing and window facts.

    Prices are USD per million tokens. ``cache_write_per_mtok`` is the 5-minute
    TTL write price (1.25x input); ``cache_read_per_mtok`` is 0.1x input. These
    two numbers drive the loop's context-pressure arithmetic: clearing a
    projection costs a cache re-write, so the loop only clears when the
    reclaimed tokens are worth it.
    """

    model_id: str
    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float  # 5m TTL = 1.25x input
    cache_read_per_mtok: float   # 0.1x input
    context_window: int
    max_output: int

    def cost_usd(self, usage: Mapping[str, int]) -> float:
        """Price a usage dict (the four Anthropic usage fields) in USD."""
        million = 1_000_000
        return (
            usage.get("input_tokens", 0) * self.input_per_mtok / million
            + usage.get("output_tokens", 0) * self.output_per_mtok / million
            + usage.get("cache_creation_input_tokens", 0) * self.cache_write_per_mtok / million
            + usage.get("cache_read_input_tokens", 0) * self.cache_read_per_mtok / million
        )


# NEVER invent date-suffixed model ids.
TIERS: dict[str, ModelTier] = {
    "strong": ModelTier("claude-opus-4-8",  5.0, 25.0, 6.25, 0.50, 1_000_000, 128_000),
    "mid":    ModelTier("claude-sonnet-5",  3.0, 15.0, 3.75, 0.30, 1_000_000, 128_000),
    "cheap":  ModelTier("claude-haiku-4-5", 1.0,  5.0, 1.25, 0.10,   200_000,  64_000),
}


@dataclass(frozen=True)
class RolePolicy:
    """How one agent role calls the model.

    ``thinking=True`` compiles to ``thinking={"type": "adaptive"}`` (the only
    supported on-mode on Opus 4.8). ``thinking=False`` omits the parameter,
    which on Opus 4.8 runs *without* thinking -- there is no budget_tokens
    anymore, and sending one is a 400.

    Effort is the primary intelligence/cost lever: it scales output tokens,
    which at $25/MTok on the strong tier is where money goes. Mechanical roles
    run ``low`` with thinking off; agentic roles run ``xhigh``.
    """

    tier: str                 # key into FableConfig.tiers
    effort: Effort
    max_tokens: int = 64_000
    thinking: bool = True


DEFAULT_ROLES: dict[str, RolePolicy] = {
    "orchestrator": RolePolicy("strong", "xhigh"),
    "executor":     RolePolicy("strong", "xhigh"),
    "researcher":   RolePolicy("mid",    "medium"),
    "judge":        RolePolicy("mid",    "medium", max_tokens=16_000),
    "refuter":      RolePolicy("strong", "high"),
    "mechanical":   RolePolicy("cheap",  "low",    max_tokens=16_000, thinking=False),
}


@dataclass(frozen=True)
class Budget:
    """Hard rails for one run. All enforced host-side (zero context cost).

    The three ``*_pct`` thresholds are the context-pressure ladder measured
    against the model's context window using *measured* usage
    (input + cache_read + cache_creation), not guesses:

    - ``warn_pct``:       inject a mid-conversation system message (terse mode)
    - ``edit_pct``:       clear oldest tool results from the projection
    - ``checkpoint_pct``: write a Checkpoint and hand back to the caller
    """

    max_usd: float = 5.0
    max_turns: int = 40
    max_wall_seconds: float = 3600.0
    warn_pct: float = 0.60
    edit_pct: float = 0.75
    checkpoint_pct: float = 0.85

    def __post_init__(self) -> None:
        if self.max_usd <= 0:
            raise ValueError(f"Budget.max_usd must be positive, got {self.max_usd}")
        if self.max_turns <= 0:
            raise ValueError(f"Budget.max_turns must be positive, got {self.max_turns}")
        if self.max_wall_seconds <= 0:
            raise ValueError(
                f"Budget.max_wall_seconds must be positive, got {self.max_wall_seconds}"
            )
        for name in ("warn_pct", "edit_pct", "checkpoint_pct"):
            value = getattr(self, name)
            if not 0.0 < value <= 1.0:
                raise ValueError(f"Budget.{name} must be in (0, 1], got {value}")
        if not self.warn_pct < self.edit_pct < self.checkpoint_pct:
            raise ValueError(
                "Budget pressure ladder must be ordered: "
                f"warn_pct ({self.warn_pct}) < edit_pct ({self.edit_pct}) "
                f"< checkpoint_pct ({self.checkpoint_pct})"
            )


@dataclass(frozen=True)
class FableConfig:
    """The one place every FABLE constant lives.

    ``loop.py`` contains zero bare tunable literals -- a unit test greps for
    them. If you find yourself wanting a magic number in the loop, it belongs
    here instead (that is the MiroFish config-drift fix).

    All betas are opt-in flags; the core loop runs on the GA API surface.
    """

    tiers: Mapping[str, ModelTier] = field(default_factory=lambda: dict(TIERS))
    roles: Mapping[str, RolePolicy] = field(default_factory=lambda: dict(DEFAULT_ROLES))
    budget: Budget = field(default_factory=Budget)
    gate_max_retries: int = 3
    tool_result_cap_chars: int = 25_000
    subagent_digest_max_tokens: int = 2_000
    clear_keep_last: int = 5
    clear_min_reclaim_tokens: int = 10_000
    memory_root: str = ".fable/memory"
    scratch_dir: str = ".fable/scratch"
    use_context_editing: bool = False   # beta clear_tool_uses_20250919, opt-in
    use_task_budget: bool = False       # beta task-budgets-2026-03-13, opt-in
    task_budget_tokens: int = 150_000   # model-visible countdown total (beta min 20_000)
    # --- loop mechanics (sourced here so loop.py stays literal-free) ---
    max_output_recoveries: int = 3      # max_tokens truncation recoveries per run
    stall_window: int = 8               # recent (tool, args_hash) pairs inspected
    stall_repeat_threshold: int = 3     # identical pairs in window => stalled
    blueprint_max_steps: int = 7        # plan gate: reject longer blueprints
    report_max_tokens: int = 8_000      # completion-report call output cap
    projection_output_allowance: int = 8_000  # output tokens assumed when projecting cost

    def __post_init__(self) -> None:
        for role, policy in self.roles.items():
            if policy.tier not in self.tiers:
                raise ValueError(
                    f"Role {role!r} references unknown tier {policy.tier!r}; "
                    f"known tiers: {sorted(self.tiers)}"
                )
        for name in (
            "gate_max_retries", "tool_result_cap_chars", "subagent_digest_max_tokens",
            "clear_keep_last", "clear_min_reclaim_tokens", "max_output_recoveries",
            "stall_window", "stall_repeat_threshold", "blueprint_max_steps",
            "report_max_tokens", "projection_output_allowance",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"FableConfig.{name} must be positive")
        if self.task_budget_tokens < 20_000:
            raise ValueError(
                "FableConfig.task_budget_tokens must be >= 20_000 "
                "(the task-budgets beta minimum), got "
                f"{self.task_budget_tokens}"
            )

    def with_overrides(self, **kw) -> "FableConfig":
        """Return a new config with the given fields replaced.

        The original is untouched -- frozen-ness is load-bearing (immutable
        config => byte-stable prefix => cache hits).
        """
        return replace(self, **kw)

    def tier_for(self, role: str) -> ModelTier:
        """Resolve a role name to its ModelTier."""
        return self.tiers[self.roles[role].tier]


class Router:
    """Model + effort routing. Control plane: costs zero context tokens.

    The litmus test as code: *if you can write an automated checker for a
    step, a scaffolded cheap model can probably do it; if you can only check
    it by being smart, you need the smart model.* A step whose ``verifier``
    is a shell command routes mid-tier (verify-and-retry makes cheap safe); a
    step whose verifier is the literal string ``"judgment"`` pins to strong.

    Escalation is gate-failure-driven only -- there is no learned quality
    estimator here, deliberately. FrugalGPT-style estimators are the
    documented weak link of cascades and un-calibratable by a solo dev.
    """

    def __init__(self, config: FableConfig):
        self._config = config

    def policy(self, role: str) -> RolePolicy:
        """Look up the RolePolicy for a role name. Raises KeyError if unknown."""
        try:
            return self._config.roles[role]
        except KeyError:
            raise KeyError(
                f"Unknown role {role!r}; known roles: {sorted(self._config.roles)}"
            ) from None

    def route_step(self, verifier: str) -> RolePolicy:
        """Route a Blueprint step by its verifier field.

        verifier is a shell command -> 'mid' tier (verify-and-retry makes
        cheap safe); verifier == 'judgment' -> 'strong'.
        """
        if verifier.strip() == "judgment":
            return RolePolicy(tier="strong", effort="xhigh")
        return RolePolicy(tier="mid", effort="medium")

    def escalate(self, policy: RolePolicy) -> RolePolicy:
        """Climb effort one notch, then tier. Raises EscalationExhausted at strong/max.

        The single sanctioned escalation path: a gate failed
        ``gate_max_retries`` times and the caller opted into escalation. Each
        call climbs exactly one rung so cost grows predictably.
        """
        effort_idx = _EFFORT_ORDER.index(policy.effort)
        if effort_idx + 1 < len(_EFFORT_ORDER):
            return replace(policy, effort=_EFFORT_ORDER[effort_idx + 1])
        try:
            tier_idx = _TIER_ORDER.index(policy.tier)
        except ValueError:
            raise EscalationExhausted(
                f"Cannot escalate custom tier {policy.tier!r} past effort "
                f"{policy.effort!r}"
            ) from None
        if tier_idx + 1 < len(_TIER_ORDER):
            return replace(policy, tier=_TIER_ORDER[tier_idx + 1])
        raise EscalationExhausted(
            "Already at the strongest configuration (strong tier, max effort); "
            "the honest outcome is a typed failure, not another retry."
        )
