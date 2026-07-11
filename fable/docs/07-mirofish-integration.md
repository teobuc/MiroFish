# 07 · Adopting FABLE inside MiroFish

This chapter is specific to the repository FABLE lives in. It maps MiroFish's
existing agentic surface to FABLE's modules, shows one worked migration, and
gives a phased adoption plan that never bets the product on a big-bang rewrite.

The honest headline first, because it shapes everything below:

> **FABLE's `client.py` speaks the Anthropic SDK. MiroFish speaks OpenAI-format
> across multiple providers.** So FABLE's *runtime client* is not a drop-in
> replacement for `LLMClient`. What transfers cleanly is FABLE's **harness** —
> the verification gate, the evidence ledger, the JSONL trace with failure
> detectors, the tool registry, and the file-based memory. Those are
> provider-agnostic Python and can wrap MiroFish's existing client as-is.

Keep that split in mind: **adopt the harness, not (yet) the client.**

---

## 1. MiroFish's agentic surface today

| Concern | Where it lives | What it does |
|---|---|---|
| Model calls | `backend/app/utils/llm_client.py` → `LLMClient.chat` / `chat_json` | Wraps the OpenAI SDK; provider chosen by `base_url` + `model` from `Config`. |
| Reasoning-model cleanup | `LLMClient.chat` | Strips `<think>…</think>` blocks; `chat_json` strips ```` ```json ```` fences before `json.loads`, raising `ValueError` on malformed output. |
| Retries | `backend/app/utils/retry.py` → `retry_with_backoff` | Hand-rolled exponential backoff + jitter decorator around API calls. |
| The agent | `backend/app/services/report_agent.py` | A LangChain + Zep ReAct loop: plan a report outline, then generate each section over multiple think/reflect turns, calling retrieval tools. |
| Run trace | `report_agent.py` → `ReportLogger` | Writes `agent_log.jsonl` (one JSON object per action: timestamp, action type, details) into the report folder. |
| Tools | `backend/app/services/zep_tools.py` → `ZepToolsService` | Search / InsightForge / Panorama / Interview retrieval against the Zep graph, returning typed result objects. |

Two things stand out. First, MiroFish already independently invented two of
FABLE's load-bearing ideas — a **JSONL action trace** (`ReportLogger` ≈
`fable.trace.TraceWriter`) and **retry-with-backoff** (`retry_with_backoff` ≈
the client's typed-exception retry). Second, the parts that are hand-rolled and
brittle — response validation, "is the agent actually done?", and tool-call
plumbing — are exactly the parts FABLE hardens.

---

## 2. What FABLE absorbs, and where it stops

| MiroFish pain point | FABLE module | Honest boundary |
|---|---|---|
| Response validation scattered in `LLMClient` (`<think>` strip, fence strip, `json.loads` that raises) | `fable.client` typed `ModelTurn` + `output_config.format` structured outputs | FABLE's version only runs against Anthropic. For MiroFish's other providers, keep `LLMClient` and lift only the *pattern* (validate → typed result, never raw-string-match). |
| "The model said it finished" trusted implicitly in `report_agent` | `fable.verify` — claim audit + `check.command` + judged rubric | Fully provider-agnostic. The gate calls *your* code (a shell command, a rubric grader), not a specific model API. Use it today. |
| `ReportLogger` is bespoke and read by nothing | `fable.trace` — same JSONL shape **plus** `detect_failures()` (7 detectors) | The wire keys already line up (`timestamp`, `action`, `details`). Point MiroFish's writer at `TraceWriter` and you get fabricated-status / premature-completion / loop-stall detection for free. |
| Ad-hoc `retry_with_backoff` around calls | `fable.loop` gate-retry + `Router.escalate` | Retrying on *failure* is what MiroFish has; retrying on *unverified* (gate said no) is what it lacks. |
| Tool-call parsing inside the ReAct loop | `fable.tools` — `@tool`, `execute`, evidence capture | Provider-agnostic for execution; the *dispatch* (how the model asks for a tool) differs between OpenAI and Anthropic formats, so this composes best once a flow is Anthropic-backed. |
| No cross-section memory beyond the transcript | `fable.memory` — checkpoints, lessons, resume | Fully provider-agnostic. A long report run can checkpoint and resume. |

**Rule of thumb:** anything in FABLE that calls *your* tools or checks *your*
artifacts (verify, trace, memory, tools) transfers to MiroFish untouched.
Anything that calls *the model* (client) waits for an Anthropic-backed flow or
a thin adapter.

---

## 3. Worked example — a verified report section

Today `report_agent` generates a section and moves on; nothing re-checks that
the output is valid or complete. Here is the same intent expressed as a FABLE
run whose gate refuses to accept an empty or malformed section — for an
Anthropic-backed report flow (`Config.LLM_BASE_URL` pointing at Anthropic):

```python
# backend/app/services/report_agent_fable.py  (illustrative)
from pathlib import Path
from fable import run, tool, check

@tool(parallel_safe=True)
def zep_search(query: str) -> str:
    """Search the Zep graph for evidence. Call before writing any claim that
    needs a source; returns matching facts, never invented ones."""
    from .zep_tools import ZepToolsService
    result = ZepToolsService().search(query)          # existing MiroFish tool
    return result.to_text()

def generate_section(report_dir: str, section_title: str) -> str:
    out = Path(report_dir) / f"{section_title}.md"
    result = run(
        f"Write the '{section_title}' section of the simulation report. "
        f"Ground every claim in a zep_search result. Write it to {out}.",
        tools=[zep_search],
        verify=check.file_exists(str(out)),           # gate: file must exist
        budget_usd=1.50,
    )
    if result.status != "ok":                         # not self-declared — gated
        raise RuntimeError(f"section failed: {result.status}\n{result.output}")
    return result.trace_path.as_posix()               # audit trail, for free
```

The behavioural change is small but total: `report_agent`'s current flow can
return a section the model *claimed* to write; this flow cannot return `"ok"`
unless the file is actually on disk. Swap `check.file_exists` for
`check.command("your_section_linter.py " + str(out))` to enforce structure.

For MiroFish's **non-Anthropic** providers, you don't get `fable.run` yet — but
you can still wrap the existing call in FABLE's trace + verify:

```python
from fable.trace import TraceEvent, TraceWriter, detect_failures
from fable.verify import check

writer = TraceWriter(Path(report_dir) / "agent_log.jsonl")   # replaces ReportLogger
# ... your existing LLMClient loop, but emit TraceEvents instead of bespoke logs ...
section = llm_client.chat(messages)                          # unchanged
gate = check.command(f"python section_linter.py {out}").run(...)  # NEW: verify
findings = detect_failures(TraceReader(writer.path))         # NEW: catch fabrication
```

---

## 4. Phased adoption — lowest risk first

Each phase is independently shippable and reversible. Stop at any phase.

1. **Observe only (zero behaviour change).** Replace `ReportLogger` with
   `fable.trace.TraceWriter` (the JSON keys already match) and run
   `detect_failures()` over completed report traces in a nightly job. You learn
   how often the current agent fabricates status or stalls — with no runtime
   risk. This is the highest-information, lowest-cost first step.
2. **Verify (catch bad output).** Add a `fable.verify` gate after each report
   section — start with `check.file_exists`, then a structural
   `check.command`. Log gate failures without blocking, then promote to
   blocking once the false-positive rate is understood.
3. **Adopt the tool registry.** Re-express `ZepToolsService` methods as `@tool`
   functions and let `fable.tools.execute` handle dispatch and evidence
   capture. This is most natural for any flow you move onto an Anthropic model.
4. **Adopt the loop.** For Anthropic-backed report flows, replace the bespoke
   ReAct loop with `fable.run` / `Agent`, keeping Zep tools and the gate from
   phases 2–3. Long runs gain checkpoint/resume via `fable.memory`.

---

## 5. What NOT to migrate

- **`LLMClient` for non-Anthropic providers.** It works and it's multi-provider;
  FABLE's client isn't. Leave it. Lift patterns, not the class.
- **The OpenAI-format `response_format={"type":"json_object"}` path.** That's
  correct for MiroFish's providers. FABLE's `output_config.format` is the
  Anthropic equivalent, not a replacement for the OpenAI one.
- **Zep integration and the graph itself.** FABLE has no opinion on retrieval;
  Zep tools become FABLE tools, but the graph, paging (`zep_paging.py`), and
  memory-updater services stay exactly as they are.
- **Simulation orchestration** (`simulation_manager`, `simulation_runner`,
  IPC). That's process/lifecycle management, not agent-loop work — out of
  FABLE's scope.

---

The safe path is phase 1: point one report run's trace at `TraceWriter`, run
`detect_failures()` over it, and see what the current agent has quietly been
doing. Everything else follows from what that shows.
