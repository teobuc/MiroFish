"""FABLE prompt loader: ``prompts/*.md`` -> byte-stable system strings.

Every shipped prompt lives in ``prompts/<name>.md`` with YAML frontmatter
(``id``, ``version``, ``model_notes``, ``source_citation``, ``ablation_status``,
``when_to_use``, ``knobs``). :func:`load` strips the frontmatter and resolves
``{{slots}}`` **exactly once, at load time, from static values only** -- never
per turn. That discipline is load-bearing: the loaded text becomes part of the
:class:`fable.client.FrozenPrefix`, and a single volatile byte (a timestamp, a
task string) in the prefix silently zeroes the prompt-cache hit ratio
(docs/02-prompting.md section 10, docs/05-memory-context.md section 1).

Slot contract:

- Callers may pass static values as keyword arguments:
  ``load("executor", workspace_root="/srv/job-42")``.
- Slots left unfilled fall back to :data:`DEFAULT_SLOTS` -- static-per-process
  values such as the working directory at load time. This is what makes
  ``prompts.load("executor")`` with no kwargs (the Tier-0 path in ``loop.py``)
  well-defined: ``{{workspace_root}}`` resolves to the current working
  directory, captured once.
- A slot with neither a caller value nor a default raises :class:`ValueError`
  loudly. A ``{{placeholder}}`` reaching the model is a silent prompt bug;
  FABLE prefers the typed, visible failure.

This module imports nothing but the standard library and sits at the bottom
of the import DAG (see docs/01-architecture.md section 3).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Mapping

_SLOT_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)

# Static defaults for slots the shipped prompts declare in their frontmatter
# ``knobs``. Each default is a zero-argument callable evaluated once per
# load() call -- static for the run, never per-turn.
DEFAULT_SLOTS: dict[str, Callable[[], str]] = {
    # executor.md -- absolute path of the working directory (static per run).
    "workspace_root": lambda: str(Path(os.getcwd()).resolve()),
    # planner.md -- plan-depth ceiling; mirrors FableConfig.blueprint_max_steps.
    "max_steps": lambda: "7",
    # verifier.md -- what is under review, when the caller does not say.
    "artifact_kinds": lambda: "the artifact files provided for review",
    # researcher.md -- mirrors FableConfig.subagent_digest_max_tokens.
    "digest_max_tokens": lambda: "2000",
    # researcher.md -- mirrors FableConfig.scratch_dir.
    "scratch_dir": lambda: ".fable/scratch",
}


def prompts_dir() -> Path:
    """Locate the ``prompts/`` directory that ships with FABLE.

    Checked in order: ``prompts/`` next to this module (packaged layout),
    then ``prompts/`` at the repository root two levels up (the
    ``src/fable`` source layout this repo uses).
    """
    here = Path(__file__).resolve()
    candidates = (
        here.parent / "prompts",       # packaged: fable/prompts/
        here.parents[2] / "prompts",   # repo: fable/src/fable/../../prompts/
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "Cannot locate the FABLE prompts directory; looked in: "
        + ", ".join(str(c) for c in candidates)
    )


def strip_frontmatter(text: str) -> str:
    """Remove the leading YAML frontmatter block (``--- ... ---``), if any.

    The frontmatter is documentation for humans and tooling (version,
    citations, knobs); it never reaches the model.
    """
    return _FRONTMATTER_RE.sub("", text, count=1)


def resolve_slots(text: str, values: Mapping[str, str]) -> str:
    """Resolve every ``{{slot}}`` in ``text`` exactly once.

    ``values`` wins over :data:`DEFAULT_SLOTS`; a slot with neither raises
    ValueError (a placeholder reaching the model is a silent prompt bug).
    """
    missing: list[str] = []

    def _sub(match: re.Match) -> str:
        slot = match.group(1)
        if slot in values:
            return str(values[slot])
        default = DEFAULT_SLOTS.get(slot)
        if default is not None:
            return default()
        missing.append(slot)
        return match.group(0)

    resolved = _SLOT_RE.sub(_sub, text)
    if missing:
        raise ValueError(
            f"Unresolved prompt slot(s) {sorted(set(missing))}: pass static "
            "values as keyword arguments to prompts.load(), or add a default "
            "to prompts.DEFAULT_SLOTS."
        )
    return resolved


def load(name: str, **static_slots: str) -> str:
    """Load ``prompts/<name>.md``: strip frontmatter, resolve slots ONCE.

    ``static_slots`` must be static for the whole run (a workspace path, a
    step ceiling) -- never per-turn values. The returned string is meant to be
    compiled into a :class:`fable.client.FrozenPrefix` and stay byte-stable
    for the run; dynamic content (task, date, budget status) belongs in the
    first user message instead (docs/01-architecture.md, INIT step 3).

    Example::

        from fable import prompts
        system = prompts.load("executor", workspace_root="/srv/job-42")
    """
    path = prompts_dir() / f"{name}.md"
    if not path.is_file():
        available = sorted(p.stem for p in prompts_dir().glob("*.md"))
        raise FileNotFoundError(
            f"No prompt named {name!r} in {prompts_dir()}; available: {available}"
        )
    text = path.read_text(encoding="utf-8")
    return resolve_slots(strip_frontmatter(text), static_slots).strip() + "\n"
