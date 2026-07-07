"""FABLE tool registry: @tool decorator, schema generation, safe execution.

Tools are where an agent touches the world, which makes tool code the place
where honesty is enforced mechanically:

- **Ground truth first.** Every execution is recorded to the Evidence Ledger
  (hash of args, hash of raw output, exit code) BEFORE the output is shaped
  for the model. The model sees a capped, spilled, hint-annotated view; the
  claim audit sees the truth.
- **Failures are observations, not exceptions.** A tool that throws becomes a
  ``tool_result`` with ``is_error: true``. The loop never crashes on a tool.
- **One user message.** All tool_result blocks for one assistant turn go back
  in a SINGLE user message -- splitting them across messages silently trains
  the model to stop parallelizing.
- **Descriptions are prompt engineering.** The docstring IS the description
  the model reads. Write it for a junior dev: what it does, when to call it,
  what it returns. One description-quality pass has been measured to cut
  completion time by ~40% (Anthropic, "Writing tools for agents").

Import DAG: this module depends on config only; UsageLedger and TraceWriter
appear as type annotations exclusively (no runtime import), keeping the DAG
acyclic.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import inspect
import json
import re
import subprocess
import time
import types
import typing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence, get_args, get_origin

if TYPE_CHECKING:  # annotation-only: respects the import DAG
    from fable.client import UsageLedger
    from fable.trace import TraceWriter

_EMPTY_OUTPUT_SENTINEL = "Command ran successfully with no output"
_SPILL_HEAD_CHARS = 2_000
_SPILL_TAIL_CHARS = 2_000
_GREP_MATCH_CAP = 200
_READ_DEFAULT_LIMIT = 100

# Parameter names too ambiguous for a model to fill reliably.
_AMBIGUOUS_PARAM_NAMES = frozenset({"data", "value", "input", "obj", "arg", "args"})


@dataclass(frozen=True)
class Tool:
    """One callable tool with its API-ready schema.

    ``parallel_safe=True`` means the tool has no side effects that another
    concurrent tool could observe (reads, searches). Mutating tools stay
    False and are serialized in block order.
    """

    name: str
    description: str          # the docstring, verbatim
    input_schema: dict        # from type hints; additionalProperties: false
    fn: Callable[..., str]
    parallel_safe: bool = False
    cap_chars: int | None = None

    def to_api(self) -> dict:
        """API wire format. ``strict: true`` on the TOOL definition (not on
        tool_choice) guarantees schema-valid input from the model."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


_JSON_TYPES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _hint_to_schema(hint: Any) -> dict:
    """Translate a Python type hint to a JSON-schema fragment.

    Supports the primitives, ``list[X]``, ``dict``, and optionals in both
    spellings -- ``Optional[X]`` and the PEP 604 ``X | None`` (origin
    ``types.UnionType``), which is the idiomatic form under this project's
    own 3.10+ style rule. Anything unrecognized falls back to string --
    loud simplicity beats a schema compiler nobody can debug.
    """
    origin = get_origin(hint)
    if origin is typing.Union or origin is types.UnionType:
        non_none = [a for a in get_args(hint) if a is not type(None)]
        if len(non_none) == 1:
            return _hint_to_schema(non_none[0])
        return {"type": "string"}
    if origin in (list, tuple):
        args = get_args(hint)
        items = _hint_to_schema(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": items}
    if origin is dict or hint is dict:
        return {"type": "object", "additionalProperties": False}
    if hint in _JSON_TYPES:
        return {"type": _JSON_TYPES[hint]}
    return {"type": "string"}


def tool(
    fn=None,
    *,
    parallel_safe: bool = False,
    cap_chars: int | None = None,
    name: str | None = None,
) -> Tool | Callable[[Callable], Tool]:
    """Turn a typed, docstringed function into a :class:`Tool`.

    Usage::

        @tool(parallel_safe=True)
        def word_count(path: str) -> str:
            \"\"\"Count words in a file. Call when the user asks about length.\"\"\"
            return str(len(Path(path).read_text().split()))

    The schema comes from the type hints; the description is the docstring,
    verbatim. A missing docstring is a hard error: an undescribed tool is a
    tool the model will misuse.
    """

    def wrap(func: Callable[..., str]) -> Tool:
        doc = inspect.getdoc(func)
        if not doc:
            raise ValueError(
                f"Tool {func.__name__!r} has no docstring. The docstring is the "
                "description the model reads -- write it for a junior dev."
            )
        signature = inspect.signature(func)
        hints = typing.get_type_hints(func)
        properties: dict[str, dict] = {}
        required: list[str] = []
        for param_name, param in signature.parameters.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                raise ValueError(
                    f"Tool {func.__name__!r} uses *args/**kwargs; tools need an "
                    "explicit, model-describable parameter list."
                )
            properties[param_name] = _hint_to_schema(hints.get(param_name, str))
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
        schema = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        return Tool(
            name=name or func.__name__,
            description=doc,
            input_schema=schema,
            fn=func,
            parallel_safe=parallel_safe,
            cap_chars=cap_chars,
        )

    if fn is not None:  # bare @tool usage
        return wrap(fn)
    return wrap


class ToolRegistry:
    """Holds a run's tool set; freezes into byte-stable schemas.

    Freezing matters because the tool schemas are part of the cached prefix:
    registering a tool after freeze would silently invalidate the cache, so
    it raises instead.
    """

    def __init__(self, tools: Sequence[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        self._frozen = False
        for t in tools:
            self.register(t)

    def register(self, t: Tool) -> None:
        """Add a tool. Rejects after freeze(); lints name and parameters."""
        if self._frozen:
            raise RuntimeError(
                "ToolRegistry is frozen; registering now would mutate the "
                "cached prefix. Build a new registry for a new run."
            )
        self._lint(t)
        if t.name in self._tools:
            raise ValueError(f"Duplicate tool name {t.name!r}")
        self._tools[t.name] = t

    def freeze(self) -> tuple[dict, ...]:
        """Lock the registry and return byte-stable schemas for FrozenPrefix
        (sorted by name -- ordering is part of the cache key)."""
        self._frozen = True
        return tuple(
            self._tools[key].to_api() for key in sorted(self._tools)
        )

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(
                f"Unknown tool {name!r}; registered: {sorted(self._tools)}"
            ) from None

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    @staticmethod
    def _lint(t: Tool) -> None:
        if not t.description.strip():
            raise ValueError(f"Tool {t.name!r} has an empty description.")
        params = set(t.input_schema.get("properties", {}))
        bad = params & _AMBIGUOUS_PARAM_NAMES
        if bad:
            raise ValueError(
                f"Tool {t.name!r} has ambiguous parameter name(s) {sorted(bad)}; "
                "rename to something the model cannot misread (e.g. 'query', "
                "'file_path', 'user_id')."
            )
        for p in params:
            if p + "_id" in params:
                raise ValueError(
                    f"Tool {t.name!r} has both {p!r} and {p + '_id'!r}; the model "
                    "will confuse them. Keep one."
                )


# --------------------------------------------------------------------------- #
# Execution


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _shape_output(raw: str, cap: int, scratch: Path, tool_use_id: str) -> str:
    """Cap what the model sees; spill the full output to scratch.

    Overflow is replaced by path + head/tail + a grep hint, because a model
    that wants the middle of a 400KB log should grep for it, not carry it in
    context forever.
    """
    if not raw.strip():
        return _EMPTY_OUTPUT_SENTINEL
    if len(raw) <= cap:
        return raw
    scratch.mkdir(parents=True, exist_ok=True)
    spill_path = scratch / f"tool-{tool_use_id}.txt"
    spill_path.write_text(raw, encoding="utf-8", errors="replace")
    return (
        f"[output: {len(raw)} chars, exceeds {cap}-char cap; "
        f"full output spilled to {spill_path}]\n"
        f"--- head ---\n{raw[:_SPILL_HEAD_CHARS]}\n"
        f"--- tail ---\n{raw[-_SPILL_TAIL_CHARS:]}\n"
        f"[to inspect the middle: grep_search(pattern, search_path={str(spill_path)!r})]"
    )


def _run_one(t: Tool, args: dict) -> tuple[str, bool, int | None]:
    """Execute one tool. Returns (raw_output, is_error, exit_code).

    Exceptions become error observations here -- the loop never crashes on a
    tool, because a crash teaches the harness nothing and costs the run.
    """
    try:
        raw = t.fn(**args)
        raw = "" if raw is None else str(raw)
        # shell_tool encodes its exit code on the last line; recover it for
        # the Evidence Ledger so mechanical checks can read ground truth.
        match = re.search(r"\[exit code: (-?\d+)\]\s*$", raw)
        exit_code: int | None = int(match.group(1)) if match else None
        is_error = exit_code is not None and exit_code != 0
        return raw, is_error, exit_code
    except Exception as error:  # noqa: BLE001 -- deliberate: observation, not crash
        return f"{type(error).__name__}: {error}", True, None


def execute(
    tool_uses: list[dict],
    registry: ToolRegistry,
    *,
    scratch: Path,
    ledger: "UsageLedger",
    trace: "TraceWriter | None" = None,
    cap_chars: int = 25_000,
) -> list[dict]:
    """Execute one assistant turn's tool_use blocks.

    Returns tool_result blocks for ONE user message, in the original block
    order. Concurrency: parallel_safe tools run concurrently in a thread
    pool; mutating tools serialize in block order. Ledger records ground
    truth pre-shaping. Overflow -> scratch spill + path/head/tail/grep-hint.
    Empty -> the explicit no-output sentinel. Exceptions -> is_error:true.
    """
    calls: list[tuple[int, dict, Tool | None, dict]] = []
    for index, block in enumerate(tool_uses):
        raw_input = block.get("input", {})
        args = json.loads(raw_input) if isinstance(raw_input, str) else dict(raw_input)
        try:
            t: Tool | None = registry.get(block["name"])
        except KeyError:
            t = None
        calls.append((index, block, t, args))

    results: dict[int, tuple[str, bool, int | None]] = {}

    parallel = [c for c in calls if c[2] is not None and c[2].parallel_safe]
    serial = [c for c in calls if c[2] is None or not c[2].parallel_safe]

    if parallel:
        with ThreadPoolExecutor(max_workers=min(8, len(parallel))) as pool:
            futures = {
                pool.submit(_run_one, t, args): index
                for index, _block, t, args in parallel
            }
            for future, index in futures.items():
                results[index] = future.result()
    for index, block, t, args in serial:  # block order preserved for mutators
        if t is None:
            results[index] = (
                f"Unknown tool {block['name']!r}. Available: "
                f"{sorted(x.name for x in registry)}",
                True,
                None,
            )
        else:
            results[index] = _run_one(t, args)

    blocks_out: list[dict] = []
    for index, block, t, args in calls:
        raw, is_error, exit_code = results[index]
        tool_use_id = block.get("id", f"toolu_missing_{index}")
        args_hash = _hash(json.dumps(args, sort_keys=True, default=str))
        result_hash = _hash(raw)
        cap = cap_chars if t is None or t.cap_chars is None else t.cap_chars
        # Ground truth BEFORE shaping: spill anything over cap so the
        # evidence path always points at the full output.
        raw_path: Path | None = None
        if len(raw) > cap:
            scratch.mkdir(parents=True, exist_ok=True)
            raw_path = scratch / f"tool-{tool_use_id}.txt"
        ledger.record_tool(
            tool_use_id=tool_use_id,
            name=block.get("name", "?"),
            args_hash=args_hash,
            result_hash=result_hash,
            exit_code=exit_code,
            is_error=is_error,
            raw_output_path=raw_path,
        )
        shaped = _shape_output(raw, cap, scratch, tool_use_id)
        if trace is not None:
            _emit_tool_event(
                trace,
                tool=block.get("name", "?"),
                tool_use_id=tool_use_id,
                args_hash=args_hash,
                result_hash=result_hash,
                exit_code=exit_code,
                is_error=is_error,
            )
        blocks_out.append(
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": shaped,
                "is_error": is_error,
            }
        )
    return blocks_out


def _emit_tool_event(trace: "TraceWriter", **detail: Any) -> None:
    """Best-effort tool_result trace event (never lets tracing kill a run)."""
    try:
        from fable.trace import TraceEvent  # runtime import only when tracing

        trace.emit(
            TraceEvent(
                ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                elapsed_seconds=0.0,
                turn=0,
                event="tool_result",
                role="",
                detail=detail,
            )
        )
    except Exception:  # noqa: BLE001 -- tracing must never crash the loop
        pass


# --------------------------------------------------------------------------- #
# Built-in tools (kept under 8 total)


def _contained(path_str: str, root: Path | None) -> Path:
    """Resolve a path, requiring absoluteness and (optionally) containment."""
    path = Path(path_str)
    if not path.is_absolute():
        raise ValueError(
            f"Path must be ABSOLUTE, got {path_str!r}. Relative paths break when "
            "the working directory changes between calls -- re-issue with the "
            "full path."
        )
    resolved = path.resolve()
    if root is not None:
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            raise ValueError(
                f"Path {resolved} escapes the workspace root {root}."
            ) from None
    return resolved


def fs_tools(root: Path | None = None) -> list[Tool]:
    """Filesystem tool set: read_file, glob, grep_search, edit_file.

    read_file is windowed (~100 lines by default) and demands absolute paths;
    grep_search returns match lists, never file dumps; edit_file does exact
    old_str/new_str replacement and REJECTS a Python edit that breaks the
    syntax -- the broken state never enters the workspace.
    """
    anchor = root.resolve() if root is not None else None

    @tool(parallel_safe=True)
    def read_file(file_path: str, offset: int = 1, limit: int = _READ_DEFAULT_LIMIT) -> str:
        """Read a window of a text file. Call before editing any file.

        file_path must be ABSOLUTE (relative paths are rejected). offset is
        the 1-based first line; limit is how many lines to return (default
        100). Output lines are prefixed with their line numbers. For large
        files, read windows -- do not page through the whole file.
        """
        path = _contained(file_path, anchor)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        window = lines[max(offset - 1, 0): max(offset - 1, 0) + limit]
        numbered = "\n".join(
            f"{i}\t{line}" for i, line in enumerate(window, start=max(offset, 1))
        )
        return numbered or f"[file has {len(lines)} lines; window was empty]"

    @tool(parallel_safe=True)
    def glob(pattern: str, base_dir: str = ".") -> str:
        """Find files by glob pattern (e.g. '**/*.py'). Call to discover file
        layout before reading. Returns one matching path per line, capped;
        never returns file contents.
        """
        base = Path(base_dir)
        matches = sorted(str(p) for p in base.glob(pattern) if p.is_file())
        if not matches:
            return f"No files match {pattern!r} under {base}."
        capped = matches[:_GREP_MATCH_CAP]
        suffix = "" if len(matches) <= _GREP_MATCH_CAP else f"\n[... {len(matches)} total]"
        return "\n".join(capped) + suffix

    @tool(parallel_safe=True)
    def grep_search(pattern: str, search_path: str = ".", file_glob: str = "**/*") -> str:
        """Search file contents with a regular expression. Call to locate code
        or text instead of reading whole files. Returns 'path:line: text'
        match lines (capped), never full file dumps.
        """
        regex = re.compile(pattern)
        base = Path(search_path)
        hits: list[str] = []
        candidates = [base] if base.is_file() else sorted(base.glob(file_glob))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{candidate}:{line_no}: {line.strip()[:200]}")
                    if len(hits) >= _GREP_MATCH_CAP:
                        return "\n".join(hits) + "\n[match cap reached; narrow the pattern]"
        return "\n".join(hits) if hits else f"No matches for {pattern!r}."

    @tool()
    def edit_file(file_path: str, old_str: str, new_str: str) -> str:
        """Replace an exact string in a file. Call after read_file so old_str
        matches byte-for-byte. old_str must occur exactly once. Edits that
        break Python syntax are rejected with the parser error and never
        written -- fix the edit, not the file.
        """
        path = _contained(file_path, anchor)
        text = path.read_text(encoding="utf-8")
        count = text.count(old_str)
        if count == 0:
            raise ValueError(
                "old_str not found. Re-read the file; the exact text (including "
                "whitespace) must match."
            )
        if count > 1:
            raise ValueError(
                f"old_str occurs {count} times; add surrounding context so it is unique."
            )
        new_text = text.replace(old_str, new_str, 1)
        if path.suffix == ".py":
            try:
                ast.parse(new_text)
            except SyntaxError as error:
                raise ValueError(
                    f"Edit rejected: it would break Python syntax "
                    f"(line {error.lineno}: {error.msg}). The file was NOT modified."
                ) from None
        path.write_text(new_text, encoding="utf-8")
        return f"Edited {path} (1 replacement)."

    return [read_file, glob, grep_search, edit_file]


def shell_tool(allowlist: Sequence[str] | None = None, timeout_s: int = 120) -> Tool:
    """A shell tool with optional executable allowlist and a hard timeout.

    Output always ends with an ``[exit code: N]`` line -- that line is how the
    Evidence Ledger recovers ground truth, and how the model learns a command
    failed without any prose from the harness.
    """
    allowed = tuple(allowlist) if allowlist else None

    @tool()
    def shell(command: str) -> str:
        """Run a shell command and return combined stdout+stderr plus an
        '[exit code: N]' trailer. Call for builds, tests, git, and anything
        without a dedicated tool. Long or destructive commands: prefer the
        smallest command that answers the question.
        """
        if allowed is not None:
            head = command.strip().split()[0] if command.strip() else ""
            if head not in allowed:
                raise ValueError(
                    f"Command {head!r} is not in the allowlist {list(allowed)}."
                )
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        body = (completed.stdout or "") + (completed.stderr or "")
        return f"{body}\n[exit code: {completed.returncode}]"

    return shell


def think_tool() -> Tool:
    """A side-effect-free scratchpad tool.

    Gives the model an explicit place to reason mid-loop without emitting
    user-facing text or fake tool activity. The result is a bare
    acknowledgement -- the value is the thought landing in the transcript.
    """

    @tool(parallel_safe=True)
    def think(thought: str) -> str:
        """Write down a private reasoning step. Call when you need to plan,
        compare options, or record an intermediate conclusion before acting.
        Has no side effects and returns only an acknowledgement.
        """
        del thought  # recorded in the transcript by virtue of the tool call
        return "Noted."

    return think
