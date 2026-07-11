# 05 — Memory & Context: The Window Is a Budget, the Disk Is the Truth

| | |
|---|---|
| **What transfers** | All of it. Cache discipline, pressure ladders, spill files, checkpoint/resume, and context isolation are host-side engineering. They determine most of your cost line and most of your long-horizon reliability, on any Claude model. |
| **What doesn't** | Recall quality. A model reads what you put in the window with the comprehension its weights provide; context engineering decides *what it sees and what that costs*, not how well it understands. And no memory layout extends the horizon past the point where per-step correctness compounds away — files buy resumability, not correctness (see [00-philosophy.md](00-philosophy.md) on p^n). |

Two resources govern every long run: the context window (1M tokens on
`claude-opus-4-8`, 200K on `claude-haiku-4-5`) and the token bill. This
document is the discipline for both: keep the expensive prefix frozen and
cached, keep the window under pressure thresholds, and keep everything
durable on disk where it costs nothing and survives the process.

## 1. Prompt-cache mechanics, with real prices

Prompt caching is the single biggest cost lever in an agent loop, because
an agent re-sends its entire history every turn. The mechanics on the
Claude API ([docs](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching)):

- **Prefix match, in order: tools → system → messages.** The cache key is
  the byte-exact prefix up to a breakpoint. Change one byte anywhere in the
  tools or system blocks and everything after it re-writes — the
  invalidation cascades forward.
- **Breakpoint:** `cache_control={"type": "ephemeral"}` on the **last**
  system text block caches tools + system in one unit. Up to 4 breakpoints
  total; FABLE uses one by default (prefix) — additional breakpoints on the
  message tail are an optimization for very stable long histories.
- **Prices (Opus 4.8, per MTok):** input $5.00, cache **write** $6.25
  (1.25x — 5-minute TTL), cache **read** $0.50 (0.1x). Sonnet-5:
  $3.00 / $3.75 / $0.30. Haiku-4-5: $1.00 / $1.25 / $0.10.
  (Sonnet-5 is in an introductory window through 2026-08-31 — $2.00 input /
  $10.00 output per MTok, reverting to the standard $3.00 / $15.00 after; the
  1.25x/0.1x cache multipliers hold in both.)
- **Minimum cacheable prefix:** 4096 tokens on Opus 4.8. Shorter prefixes
  silently don't cache — pad-free below that line, there is nothing to win.
  (1024 tokens is the older Sonnet-4.5-era floor, not Opus 4.8's — do not
  conflate them.)
- **Cache reads do not count against rate limits** the way fresh input
  does — at scale this matters as much as the price.
- **Verify, don't assume:** `response.usage.cache_read_input_tokens` is
  the ground truth. The `UsageLedger` records it on every call and computes
  a rolling `cache_hit_ratio = read / (read + creation + input)`.

The arithmetic that justifies the discipline: a 40-turn run with a 20K-token
prefix and growing history averaging 60K tokens/call. Uncached:
40 × 80K × $5 ≈ **$16.00** of input. With a stable prefix and warm history,
turn N re-reads turns 1..N-1 at 0.1x: roughly **$2.40** — a 5–7x input-cost
cut from byte stability alone. This is why the numbers below treat a cache
miss as an incident, not a shrug.

## 2. The frozen prefix: byte stability as an invariant

`FrozenPrefix.build(system, tools)` compiles the tool schemas and system
prompt once at INIT, puts the `ephemeral` breakpoint on the last system
text block, and sha256-hashes the canonical JSON. `FableClient.call()`
asserts the hash on every call; a mismatch raises `PrefixMutationError`.
CI runs the same assertion: two consecutive prefix assemblies must be
byte-identical.

This hard line exists because prefix mutation is always an accident and
always expensive. The classic leaks, all of which FABLE routes elsewhere:

| Temptation | Where it goes instead |
|---|---|
| Today's date, task text, budget status in the system prompt | **First user message** — assembled once at INIT |
| `memory.index()`, resume litany | First user message |
| "Be terse now" / budget warnings mid-run | **Mid-conversation system message**: `{"role": "system", "content": ...}` appended after a user turn (never `messages[0]`) — the operator channel that steers without touching the cached prefix (Opus 4.8, no beta) |
| Per-turn `{{slot}}` interpolation in prompt templates | `prompts.load()` resolves slots **once** at load time, from static kwargs only |
| Registering a tool mid-run | Forbidden; `ToolRegistry.freeze()` rejects it |

**The canonical symptom:** rolling cache-hit ratio below 80% on a
multi-turn run. The ledger warns at that threshold, and the right response
is to diff two turns' request prefixes, find the mutating byte, and move it
into a user message. At $6.25/MTok writes versus $0.50/MTok reads, a
prefix that mutates every turn costs 12.5x what it should — a timestamp
in the system prompt is the most expensive clock you will ever run.

## 3. Context pressure: the 60 / 75 / 85 ladder

The loop measures used context every turn as
`input + cache_read + cache_creation` from actual usage (never estimated
client-side — `count_tokens` for planning, `usage` for truth) against the
tier's window, and acts on three thresholds from `Budget`:

**60% — steer (`warn_pct`).** Append a mid-conversation system message:
prefer terse output, spill large results to `scratch/` and reference by
path, stop quoting file contents back. Costs a few dozen tokens, preserves
the cached prefix, often defers the next threshold entirely.

**75% — clear (`edit_pct`).** Swap the message *projection*: drop the
oldest tool_result bodies, keep the last `clear_keep_last=5` intact,
replace each removed body with a one-line placeholder noting what was
removed and its `scratch/` spill path (so the model can re-fetch precisely
what it needs). Two hard rules:

- The trace is never rewritten. The append-only JSONL keeps everything;
  clearing changes only what the next API call sends
  (see [01-architecture.md](01-architecture.md) on trace-as-projection).
  Resume, fork, and audit all still work.
- **Only clear when it reclaims ≥ `clear_min_reclaim_tokens` (10K).** An
  edited projection is a new prefix from the edit point forward, so the
  next call re-*writes* that suffix at 1.25x. Clearing 3K tokens to
  trigger a 200K-token cache re-write is a net loss; the loop does this
  arithmetic and skips the clear if it doesn't pay.

Client-side by default. The API-native equivalent —
`edits=[{"type": "clear_tool_uses_20250919"}]` under beta
`context-management-2025-06-27` — is behind `use_context_editing=True`.
Same trade, server-side; the core loop stays on the GA surface.

**85% — checkpoint (`checkpoint_pct`).** Write a `Checkpoint` to memory,
then either continue compacted or return `status="checkpointed"` so the
caller respawns a fresh session. The escalation ladder, cheapest-first:
steer → clear tool results → beta compaction (`compact-2026-01-12`,
opt-in) → **fresh-session respawn**. For very long runs FABLE prefers the
respawn (§5.3): summarization is lossy in ways you can't audit, while
files on disk are lossless and a fresh model rediscovers state from them
reliably — `pwd`, `git log`, read `progress.md`, and it's oriented.

## 4. Scratchpads and the spill discipline

`scratch/` (default `.fable/scratch`, wiped at session start) is where
bulk data lives so context doesn't have to:

- Every tool result is capped at `tool_result_cap_chars` (25K). Overflow
  is spilled to a scratch file and the model sees: the path, head and tail
  excerpts, and a grep hint ("search this file with
  `grep_search(pattern, search_path=...)` rather than re-reading it whole").
- The Evidence Ledger captures the *uncapped* output's hash and exit code
  before shaping — so the claim audit ([04-verification.md](04-verification.md))
  judges against ground truth even when the model saw a truncated view.
- Empty output becomes the literal string
  `"Command ran successfully with no output"` — an empty tool_result reads
  as an error and triggers pointless retries.
- Subagents write full findings to scratch files and return only paths
  (§6). The `think_tool` gives the model a side-effect-free place to
  reason without narrating into the transcript.

The principle: **context is for deciding; disk is for storing.** Any token
that isn't needed for the next decision belongs in a file with a path.

## 5. File-based memory

`Memory` (root `.fable/memory/`) is deliberately primitive: markdown and
JSON on disk, greppable, diffable, human-editable, no embeddings, no
database. The layout is fixed — every FABLE component assumes these exact
paths:

```text
.fable/memory/
  MEMORY.md                      # the index — <=150 lines, always safe to load
  lessons/
    20260702-pytest-needs-e.md   # one lesson per file, <=30 lines
    20260705-api-rate-limit.md
  state/
    progress.md                  # append-only journal, newest first
    plan.md                      # current Blueprint, overwritten
    feature_list.json            # [{category, description, steps, passes: false}]
  scratch/                       # spill target; wiped on session start
```

### 5.1 One lesson per file, and the hygiene rules

A lesson is a hard-won fact worth paying tokens for in a future run:
"pytest in this repo requires `-p no:cacheprovider` or it flakes under
parallel runs." Rules that keep the store loadable instead of a landfill:

- **One lesson per file, ≤30 lines.** Loadable individually; monolithic
  `LESSONS.md` files force all-or-nothing loading and rot into
  contradictions nobody can safely edit.
- **First line is the trigger condition, not the moral.**
  `# When: running pytest in repos with xdist` — because lessons are
  *retrieved by situation*: the index lists trigger lines, and the model
  pulls the body only when the trigger matches. A lesson filed under its
  conclusion is never found again.
- **`MEMORY.md` is an index, hard-capped at 150 lines.** One line per
  lesson (trigger + filename), a pointer to `state/`. `memory.index()` is
  what enters the first user message; bodies are fetched just-in-time by
  path. Never load the whole memory directory into context.
- **Gardening.** `memory.garden()` — a cheap-tier pass — merges duplicate
  lessons, deletes lessons that stopped being true (the fix landed
  upstream), and rebuilds the index. Run it between sessions, not during.
- **Containment and redaction.** Every write path resolves and checks
  `relative_to(root)` — `../` and encoded traversal variants raise
  `ContainmentError`. Secret-shaped strings (key patterns, tokens) are
  redacted before any write; memory files outlive sessions and end up in
  git, in backups, in other agents' contexts.

### 5.2 `feature_list.json`: the reliability inversion

The task list is machine-checkable state, and it embeds FABLE's central
trust rule: **only the harness flips `passes` to true.**
`memory.mark_passed(feature_id, evidence_ids)` is the sole code path that
does it, and it is called by the gate after evidence exists — never by the
model, which does not have a tool for it. The model proposes; the gate
disposes; the file records. A model-writable done-list converges on
optimistic fiction within a handful of turns (the fabricated-status
failure mode from [04-verification.md](04-verification.md), in
persistent form). The blueprint's step statuses follow the same rule:
harness-owned, so the model cannot "re-plan" already-executed work out of
existence.

### 5.3 Checkpoint and the fresh-session respawn

The `Checkpoint` schema is exactly what a *cold* successor needs and
nothing else:

```text
goal            — one sentence
decisions       — each with its why (the why is what prevents re-litigating)
files_touched   — paths only
verified_done   — each item carries evidence ids (gate-confirmed only)
open_issues     — known-broken, known-unknown
next_steps      — exactly the next 3, concrete
lessons         — candidates for lessons/
```

Everything else — code state, test results, file contents — is
deliberately *not* in the checkpoint, because it reconstructs from disk
and git more reliably than any summary preserves it. The
`memory.resume_prompt()` litany a fresh session boots with:

1. Print the working directory and list it — confirm where you are. (The
   memory index arrives separately via `memory.index()` in the boot
   context; the litany itself does not re-read `MEMORY.md`.)
2. If this is a git repo, read `git log --oneline -10` — what actually
   landed, not what was claimed.
3. Read `state/progress.md` (newest entry first) for what previous
   sessions did and why.
4. Read `state/feature_list.json`; entries with `passes: false` are the
   open work.
5. Pick **one** unfinished feature. One.
6. Re-run one smoke check (that feature's verifier command) before editing
   anything — verify the world matches the notes, because the notes are
   claims.

Why respawn beats in-window summarization for very long runs: a summary is
a lossy compression chosen by the *outgoing* context, which doesn't know
what the successor will need; files don't rot, git doesn't lie, and the
six-step litany costs a few thousand tokens against a clean 1M window.
Filesystem rediscovery is also this framework's answer to the missing
resume path in MiroFish's own long jobs
(see [06-operations.md](06-operations.md)).

## 6. Subagent context isolation

A subagent (`subagents.spawn`) is context engineering by another name: a
fresh window that sees *only* its `Brief` — objective, output format, tool
guidance, boundaries — plus its scoped tools. Not the orchestrator's
history, not sibling transcripts, not the memory directory. The isolation
buys three things: the subagent's exploration garbage (twenty search
results, five dead ends) never pollutes the orchestrator's window; the
40-line brief replaces a 200K-token shared history; and one-responsibility-
per-subagent makes failure attribution structural
(see [03-orchestration.md](03-orchestration.md)).

What crosses back is a hard contract, enforced by the harness: a digest of
≤ `subagent_digest_max_tokens` (2K — one cheap-tier re-summarize on
overflow, then truncation), artifact **paths** for everything bulky,
confidence, and open questions. Full findings live in `scratch/` files;
the sidechain transcript goes to its own JSONL. The orchestrator reads
digests and opens artifacts by path only when it must.

Price the isolation honestly: multi-agent runs cost ~15x a chat
interaction in tokens (single agents ~4x)
([Anthropic, multi-agent research system](https://www.anthropic.com/engineering/built-multi-agent-research-system)),
because every subagent re-pays a prefix write and its own exploration.
Isolation is a correctness-and-focus purchase, not a cost optimization —
which is why the delegation heuristics in
[03-orchestration.md](03-orchestration.md) default to a single agent.

## 7. JIT retrieval over RAG

For code and structured project state, FABLE ships no embedding index. The
retrieval loop is: `MEMORY.md` and the file map preloaded (cheap, small),
then `grep_search` → `glob` → windowed `read_file` on demand. Agentic
just-in-time retrieval beats embedding retrieval for code because
identifiers are exact strings (grep finds `parse_tool_calls` with
precision 1.0), structure is navigable (imports, directory layout), and
the model can iterate query → result → refined query within the loop.
Embedding pipelines add an index to build, staleness to manage, and an
unverifiable relevance model in the middle of your verification spine.

RAG remains right for large *prose* corpora — docs sites, papers, support
tickets — where exact-string search genuinely fails. That is a
user-supplied `@tool` wrapping whatever store you like; the loop treats it
like any other tool.

## 8. The denylist: what never enters context

Each of these is a recurring, expensive mistake. The harness blocks most
of them mechanically (caps, spills, windowed reads); the rest are prompt
contract ([prompts/executor.md](../prompts/executor.md)):

- **Raw logs and full command dumps** — capped and spilled; the model gets
  head/tail + a grep hint.
- **Base64, binary, minified blobs** — token-dense noise; always
  path-referenced.
- **Secrets** — redacted before any write or context insertion; a secret
  in context is a secret in the trace JSONL forever.
- **Whole-file dumps when a window suffices** — `read_file` is windowed
  (~100 lines, offset/limit) by design.
- **Duplicate re-reads** — re-reading an unchanged file re-buys the same
  tokens; the placeholder-with-path pattern from §3 exists so re-fetches
  are targeted.
- **The whole memory directory** — index in, bodies by path (§5.1).
- **Another agent's transcript** — digests and artifacts cross the
  boundary; transcripts don't (§6).
- **Generator rationale into a judge** — an isolation rule, not a cost
  rule ([04-verification.md](04-verification.md)).

## 9. Honesty box

Context engineering changes what the model sees and what each turn costs.
It does not change how well the model uses what it sees: recall from a
well-packed window, inference over a lesson file, judgment about which
scratch artifact matters — all of that is weights. The cache discipline in
§1–2 is worth a 5–7x input-cost reduction and the memory discipline in §5
is worth runs that survive their own length, and neither adds a point of
per-step correctness. Cheaper and longer, not smarter — that is the whole,
sufficient pitch.
