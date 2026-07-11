"""FABLE's single choke point for the Anthropic API.

This module is the ONLY place in FABLE that touches the ``anthropic`` SDK.
Everything the rest of the framework needs -- streaming, adaptive thinking,
effort, prompt-cache breakpoints, structured output, refusal/pause_turn
handling, retries, usage accounting -- lives behind :class:`FableClient`.

Design commitments (see docs/01-architecture.md):

- **Frozen prefix.** Tool schemas + system prompt are compiled once into a
  :class:`FrozenPrefix`, hashed, and asserted byte-stable per role for the
  whole run (:class:`PrefixMutationError` on drift). A mutated prefix is the
  canonical cause of a cache-hit ratio under 80%, which at 0.1x cache reads
  is the single biggest cost lever there is.
- **Always stream.** ``client.messages.stream(...)`` as a context manager +
  ``stream.get_final_message()``. Required above ~16K output tokens; harmless
  below it.
- **stop_reason before content.** A refusal may carry an empty content list;
  reading ``content[0]`` first is a crash waiting to happen.
- **One retry implementation.** Typed exceptions most-specific-first
  (RateLimitError -> APIStatusError -> APIConnectionError), jittered backoff,
  one layer above the SDK's own auto-retry of 429/5xx.

The module imports cleanly with no API key present: ``anthropic.Anthropic()``
is constructed lazily on the first actual call.
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from fable.config import FableConfig, ModelTier, RolePolicy, Router

if TYPE_CHECKING:  # annotation-only; keeps the import DAG acyclic
    from fable.tools import Tool

# Client-level retry posture (client.py may hold its own literals; loop.py may not).
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 2.0
_CACHE_HIT_WARN_RATIO = 0.80


class PrefixMutationError(RuntimeError):
    """The frozen prefix changed mid-run.

    Any byte change to tools or system prompt invalidates the entire prompt
    cache (prefix match), silently multiplying input cost by ~10x. FABLE
    treats that as a bug, not a billing surprise.
    """


def _lazy_anthropic():
    """Import the anthropic SDK on demand so bare imports need no API key."""
    import anthropic  # local import: the one sanctioned SDK import in FABLE

    return anthropic


def _canonical_json(obj: Any) -> str:
    """Deterministic serialization -- unsorted keys are a silent cache killer."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _strictify(schema: dict) -> dict:
    """Return a copy of ``schema`` with ``additionalProperties: false`` on every
    object node. Structured output requires it; forgetting it is a 400."""
    if not isinstance(schema, dict):
        return schema
    out: dict = {}
    for key, value in schema.items():
        if isinstance(value, dict):
            out[key] = _strictify(value)
        elif isinstance(value, list):
            out[key] = [_strictify(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    if out.get("type") == "object":
        out.setdefault("additionalProperties", False)
    return out


@dataclass(frozen=True)
class FrozenPrefix:
    """The byte-stable cacheable prefix: tool schemas + system blocks.

    ``cache_control={"type": "ephemeral"}`` sits on the LAST system text block,
    which caches tools + system together (render order is tools -> system ->
    messages). ALL dynamic content -- task, date, budget status, memory index --
    belongs in the first user message, never here.
    """

    system_blocks: tuple[dict, ...]
    tool_schemas: tuple[dict, ...]
    digest: str  # sha256 of canonical JSON, computed at construction

    @classmethod
    def build(cls, system: str, tools: Sequence["Tool"]) -> "FrozenPrefix":
        """Compile a prefix. ``system`` must already be fully resolved --
        no per-turn interpolation, no timestamps, no task text."""
        system_blocks = (
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            },
        )
        # Sort by name: a reordered tool list is byte-different and cache-fatal.
        tool_schemas = tuple(
            sorted((t.to_api() for t in tools), key=lambda s: s["name"])
        )
        digest = hashlib.sha256(
            _canonical_json({"system": system_blocks, "tools": tool_schemas}).encode()
        ).hexdigest()
        return cls(system_blocks=system_blocks, tool_schemas=tool_schemas, digest=digest)


@dataclass(frozen=True)
class ModelTurn:
    """One completed model turn, safe to consume.

    ``content`` holds raw content blocks already shaped for re-appending as an
    assistant turn. ``usage`` always carries all four fields. ``stop_details``
    is non-null only on refusal.
    """

    stop_reason: str          # end_turn | tool_use | max_tokens | pause_turn | refusal
    content: tuple[dict, ...]
    usage: dict
    cost_usd: float
    stop_details: Any | None = None

    @property
    def text(self) -> str:
        """Concatenated text blocks. Safe on refusals: checks stop_reason
        before touching content, and tolerates an empty content list."""
        if self.stop_reason == "refusal":
            return ""
        return "".join(
            block.get("text", "")
            for block in self.content
            if block.get("type") == "text"
        )

    @property
    def tool_calls(self) -> list[dict]:
        """tool_use blocks with ``input`` already json-parsed. Never
        string-match serialized inputs -- escaping differs across models."""
        calls: list[dict] = []
        for block in self.content:
            if block.get("type") != "tool_use":
                continue
            raw = block.get("input")
            if isinstance(raw, str):
                raw = json.loads(raw)
            calls.append({"id": block.get("id"), "name": block.get("name"), "input": raw})
        return calls


@dataclass
class _ToolEvidence:
    """Ground-truth record of one tool execution, captured pre-shaping."""

    tool_use_id: str
    name: str
    args_hash: str
    result_hash: str
    exit_code: int | None
    is_error: bool
    raw_output_path: Path | None


class UsageLedger:
    """Token, cost, and evidence accounting for one run.

    Two jobs:

    1. **Usage** -- every model call records all four usage fields plus USD,
       split by role, so ``result.cost_usd`` and cost-by-role reports are
       measured facts, not estimates.
    2. **Evidence Ledger** -- every tool execution records its ground truth
       (hashes, exit code, raw-output path) BEFORE truncation or shaping.
       The model sees shaped output; the claim audit sees this. That split is
       what makes "done/passing/fixed" claims deterministically checkable.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._totals: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        self._cost_usd = 0.0
        self._cost_by_role: dict[str, float] = {}
        self._evidence: dict[str, _ToolEvidence] = {}
        self._cache_hit_series: list[float] = []

    def record(self, usage: dict, tier: ModelTier, role: str) -> None:
        """Record one model call's usage. Called by FableClient only."""
        cost = tier.cost_usd(usage)
        with self._lock:
            for key in self._totals:
                self._totals[key] += int(usage.get(key) or 0)
            self._cost_usd += cost
            self._cost_by_role[role] = self._cost_by_role.get(role, 0.0) + cost
            self._cache_hit_series.append(self.cache_hit_ratio)

    def record_tool(
        self,
        tool_use_id: str,
        name: str,
        args_hash: str,
        result_hash: str,
        exit_code: int | None,
        is_error: bool,
        raw_output_path: Path | None,
    ) -> None:
        """Evidence Ledger: ground truth captured BEFORE truncation/shaping."""
        with self._lock:
            self._evidence[tool_use_id] = _ToolEvidence(
                tool_use_id=tool_use_id,
                name=name,
                args_hash=args_hash,
                result_hash=result_hash,
                exit_code=exit_code,
                is_error=is_error,
                raw_output_path=raw_output_path,
            )

    def evidence_for(self, tool_use_id: str) -> dict | None:
        """Look up ground truth for one tool call (claim audit's data source)."""
        ev = self._evidence.get(tool_use_id)
        if ev is None:
            return None
        return {
            "tool_use_id": ev.tool_use_id,
            "name": ev.name,
            "args_hash": ev.args_hash,
            "result_hash": ev.result_hash,
            "exit_code": ev.exit_code,
            "is_error": ev.is_error,
            "raw_output_path": str(ev.raw_output_path) if ev.raw_output_path else None,
        }

    @property
    def totals(self) -> dict:
        """All four usage fields, summed across the run."""
        with self._lock:
            return dict(self._totals)

    @property
    def cost_usd(self) -> float:
        return self._cost_usd

    @property
    def cache_hit_ratio(self) -> float:
        """read / (read + creation + input). Below 0.80 means something in the
        prefix is mutating -- go find the timestamp."""
        read = self._totals["cache_read_input_tokens"]
        denom = (
            read
            + self._totals["cache_creation_input_tokens"]
            + self._totals["input_tokens"]
        )
        return (read / denom) if denom else 0.0

    def by_role(self) -> dict[str, float]:
        """USD spent per role -- makes the ~15x multi-agent multiplier visible."""
        with self._lock:
            return dict(self._cost_by_role)

    def project_next_call_usd(
        self, context_tokens: int, tier: ModelTier, output_allowance: int
    ) -> float:
        """Estimate the next call's cost so the budget rail can trip BEFORE
        spending, not after.

        Assumes the observed cache-hit ratio holds: the cached share of the
        context bills at read price, the rest at input price, plus the
        ``output_allowance`` tokens the caller assumes for output (the loop
        passes ``FableConfig.projection_output_allowance`` -- constants live
        in config, not here). An estimate, deliberately slightly pessimistic.
        """
        million = 1_000_000
        ratio = self.cache_hit_ratio
        cached = context_tokens * ratio
        uncached = context_tokens - cached
        return (
            cached * tier.cache_read_per_mtok / million
            + uncached * tier.input_per_mtok / million
            + output_allowance * tier.output_per_mtok / million
        )


class FableClient:
    """Thin, honest wrapper over ``anthropic.Anthropic()``.

    The ONLY place the anthropic SDK is imported. Lazy ``Anthropic()`` on the
    first call, so every FABLE module imports with no API key present.

    Behaviors (non-negotiable, per the FABLE spec):

    - asserts prefix byte-stability per role (PrefixMutationError on drift)
    - always streams; ``thinking={"type": "adaptive"}`` when the policy says
      so, omitted otherwise (omitting DISABLES thinking on Opus 4.8);
      ``output_config={"effort": ...}``; no temperature/top_p/top_k, no
      prefill, no budget_tokens -- all three are 400s on Opus 4.8
    - handles ``pause_turn`` internally (append assistant content, re-send)
    - exception ladder most-specific first with jittered backoff
    - records every call to the UsageLedger
    """

    def __init__(self, config: FableConfig, ledger: UsageLedger | None = None):
        self._config = config
        self._router = Router(config)
        self._ledger = ledger if ledger is not None else UsageLedger()
        self._sdk_client: Any = None
        self._pinned_digests: dict[str, str] = {}  # role -> prefix digest
        self._lock = threading.Lock()

    @property
    def ledger(self) -> UsageLedger:
        return self._ledger

    @property
    def config(self) -> FableConfig:
        return self._config

    # ------------------------------------------------------------------ #

    def call(
        self,
        *,
        prefix: FrozenPrefix,
        messages: list[dict],
        role: str = "executor",
        policy: RolePolicy | None = None,
        output_schema: dict | None = None,
    ) -> ModelTurn:
        """One model turn: stream, absorb pause_turns, account, return.

        ``messages`` may legally contain mid-conversation system messages
        (``{"role": "system", ...}`` after a user turn, never messages[0]) --
        that is the operator channel that preserves the cached prefix on
        Opus 4.8.
        """
        self._assert_prefix(role, prefix)
        policy = policy or self._router.policy(role)
        tier = self._config.tiers[policy.tier]

        params: dict[str, Any] = {
            "model": tier.model_id,
            "max_tokens": min(policy.max_tokens, tier.max_output),
            "system": list(prefix.system_blocks),
            "messages": messages,
            "output_config": {"effort": policy.effort},
        }
        if prefix.tool_schemas:
            params["tools"] = list(prefix.tool_schemas)
        if policy.thinking:
            # Explicit: omitting `thinking` runs WITHOUT thinking on Opus 4.8.
            params["thinking"] = {"type": "adaptive"}
        if output_schema is not None:
            params["output_config"]["format"] = {
                "type": "json_schema",
                "schema": _strictify(output_schema),
            }
        if self._config.use_task_budget:
            # Opt-in beta (task-budgets-2026-03-13): the model sees a token
            # countdown and self-moderates. A courtesy to the model only --
            # the host-side Budget rails remain authoritative. Requires the
            # beta messages surface (_stream_with_retry switches on "betas").
            params["output_config"]["task_budget"] = {
                "type": "tokens",
                "total": self._config.task_budget_tokens,
            }
            params["betas"] = ["task-budgets-2026-03-13"]

        content_acc: list[dict] = []
        usage_acc = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        cost_acc = 0.0
        working_messages = messages

        while True:
            message = self._stream_with_retry(params | {"messages": working_messages})
            usage = self._usage_dict(message)
            self._ledger.record(usage, tier, role)
            for key in usage_acc:
                usage_acc[key] += usage[key]
            cost_acc += tier.cost_usd(usage)
            blocks = [self._block_to_dict(b) for b in message.content]
            content_acc.extend(blocks)

            if message.stop_reason == "pause_turn":
                # Server-side pause: append the assistant content and re-send.
                # The loop above us never sees this stop reason.
                working_messages = list(working_messages) + [
                    {"role": "assistant", "content": blocks}
                ]
                continue
            break

        stop_details = getattr(message, "stop_details", None)
        if message.stop_reason != "refusal":
            stop_details = None  # stop_details is null unless refusal
        return ModelTurn(
            stop_reason=message.stop_reason,
            content=tuple(content_acc),
            usage=usage_acc,
            cost_usd=cost_acc,
            stop_details=stop_details,
        )

    def structured(
        self,
        *,
        prefix: FrozenPrefix,
        messages: list[dict],
        role: str,
        schema: dict,
        policy: RolePolicy | None = None,
    ) -> Any:
        """Structured output with the one and only repair ladder:

        native json_schema output -> ``json.loads`` -> single re-ask carrying
        the validation error. No regex tier -- if strict schema output plus
        one explicit correction cannot produce parseable JSON, that is a
        signal worth surfacing, not papering over.
        """
        turn = self.call(
            prefix=prefix, messages=messages, role=role, policy=policy,
            output_schema=schema,
        )
        if turn.stop_reason == "refusal":
            raise RuntimeError(
                f"Structured call refused (stop_details={turn.stop_details!r})"
            )
        try:
            return json.loads(turn.text)
        except (json.JSONDecodeError, ValueError) as first_error:
            repair_messages = list(messages) + [
                {"role": "assistant", "content": list(turn.content)},
                {
                    "role": "user",
                    "content": (
                        "Your previous output did not parse as JSON matching the "
                        f"required schema. Parse error: {first_error}. "
                        "Respond again with ONLY the corrected JSON object."
                    ),
                },
            ]
            retry = self.call(
                prefix=prefix, messages=repair_messages, role=role, policy=policy,
                output_schema=schema,
            )
            if retry.stop_reason == "refusal":
                # stop_reason before content, on the retry too: a refused
                # repair has empty text, and json.loads("") would report a
                # misleading parse error instead of the refusal signal.
                raise RuntimeError(
                    f"Structured repair call refused "
                    f"(stop_details={retry.stop_details!r})"
                ) from first_error
            return json.loads(retry.text)  # let a second failure raise loudly

    def count_tokens(self, messages: list[dict], model: str) -> int:
        """Server-side token counting. Never tiktoken -- it is the wrong
        tokenizer and undercounts Claude by 15-20%."""
        client = self._client()
        response = client.messages.count_tokens(model=model, messages=messages)
        return int(response.input_tokens)

    # ------------------------------------------------------------------ #

    def _assert_prefix(self, role: str, prefix: FrozenPrefix) -> None:
        with self._lock:
            pinned = self._pinned_digests.get(role)
            if pinned is None:
                self._pinned_digests[role] = prefix.digest
            elif pinned != prefix.digest:
                raise PrefixMutationError(
                    f"Prefix for role {role!r} changed mid-run "
                    f"(pinned {pinned[:12]}, got {prefix.digest[:12]}). "
                    "Dynamic content belongs in user messages, not the prefix."
                )

    def _client(self):
        if self._sdk_client is None:
            anthropic = _lazy_anthropic()
            self._sdk_client = anthropic.Anthropic()
        return self._sdk_client

    def _stream_with_retry(self, params: dict) -> Any:
        """Streamed request with the single retry ladder.

        Most-specific first: RateLimitError -> APIStatusError ->
        APIConnectionError. One layer above the SDK's own auto-retry (which
        already handles 429/5xx twice), so total attempts stay bounded.
        """
        anthropic = _lazy_anthropic()
        client = self._client()
        last_error: Exception | None = None
        # Beta params (e.g. the opt-in task budget) route through the beta
        # messages surface; the core loop otherwise stays on the GA API.
        surface = client.beta.messages if "betas" in params else client.messages
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with surface.stream(**params) as stream:
                    return stream.get_final_message()
            except anthropic.RateLimitError as error:  # 429: always retryable
                last_error = error
            except anthropic.APIStatusError as error:
                # Non-2xx other than 429. 4xx are our bug -- fail fast.
                if error.status_code is not None and error.status_code < 500:
                    raise
                last_error = error
            except anthropic.APIConnectionError as error:  # network layer
                last_error = error
            if attempt + 1 < _MAX_ATTEMPTS:
                delay = _BACKOFF_BASE_SECONDS * (2 ** attempt)
                time.sleep(delay + random.uniform(0.0, delay / 2))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _usage_dict(message: Any) -> dict:
        usage = getattr(message, "usage", None)
        return {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "cache_creation_input_tokens": int(
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            ),
            "cache_read_input_tokens": int(
                getattr(usage, "cache_read_input_tokens", 0) or 0
            ),
        }

    @staticmethod
    def _block_to_dict(block: Any) -> dict:
        """Convert an SDK content block to a plain dict that can be appended
        back verbatim as assistant content (thinking blocks included)."""
        if isinstance(block, dict):
            return block
        if hasattr(block, "model_dump"):
            return block.model_dump(exclude_none=True)
        return dict(block)  # pragma: no cover -- future SDK shapes
