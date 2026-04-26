# MemPalace Memory Curation: Design Spec

**Date:** 2026-04-24
**Author:** Claude (autonomous, reviewed by Alan)
**Status:** Draft → Implementation
**Branch:** `claude/tender-austin-767b90`

---

## Goal

Make MemPalace function as a **memory system**, not just a code-and-conversation index.

The data ingestion pipeline already works — 344,332 drawers filed across 18 wings. The **curation pipeline is broken**: KG has 0 entities / 0 triples; diary has 8 entries across 4 agent wings. Per the existing protocol (`mempalace_status`), Claude is supposed to call `kg_query FIRST`, but there's nothing to query. Search returns code, not curated memory.

This design fixes that with two complementary pipelines.

## Non-Goals

- Reprocessing all 344K drawers with an LLM (we'll filter to high-signal rooms).
- Upstream PR readiness on day 1 (local-first, refactor for upstream later).
- Replacing the existing miner or hooks (we extend, not rewrite).
- Cloud APIs, telemetry, or anything that violates the `local-first, zero API` design principle in `CLAUDE.md`.

## Architecture

Two pipelines, complementary not redundant:

```
                     [drawers in palace]
                             |
              +--------------+----------------+
              |                               |
              v                               v
       PIPELINE B                       PIPELINE A
       (background)                     (in-conversation)
              |                               |
   curate_memory.py                  Stop-hook block @
   --since N days                    every 30 exchanges
   --rooms allowlist                       |
              |                               v
   filters drawers ──> Claude CLI    AI writes diary entry
   extracts:                         + KG triples for the
     - entity triples                session, using AAAK
     - structured                    + protocol from
       observations                  mempalace_status
              |                               |
              v                               v
       kg_add(...)                    kg_add + diary_write
       diary_write(...)                via MCP tools
              |                               |
              +-----------+-------------------+
                          v
              [populated KG + diary]
```

### Pipeline A — Agent-Driven (in-conversation)

**Trigger:** existing `hooks/mempal_save_hook.sh` Stop hook, with two changes:
1. Default flips to `MEMPAL_VERBOSE=true` (block-and-prompt mode), opt-out via env var.
2. `SAVE_INTERVAL` raised from 15 → 30 (less interruption; longer sessions are when curation has signal).
3. Block reason rewritten to demand structured output: AAAK-format diary entry + N KG triples for any new facts/decisions/preferences/people, using the existing `mempalace_diary_write` and `mempalace_kg_add` MCP tools.

The precompact hook stays as the safety net — it always blocks and the new prompt applies there too.

**Why agent-driven:** the AI has live conversation context the background pipeline can't see — what was decided, who matters, what surprised the user. That's the high-signal layer.

### Pipeline B — Background Extraction

**New script:** `scripts/curate_memory.py`. CLI:

```
mempalace curate \
    [--since 7]                # days back; default = since last successful run
    [--rooms general,documentation,decisions,diary,journals,proposals]
    [--exclude-rooms dashboard,data] # drop high-volume non-memory rooms
    [--max-drawers 500]         # daily cap
    [--engine claude|heuristic|both]
    [--dry-run]
```

The room allowlist is the primary filter. `--exclude-rooms` is for cases where an allowlisted room contains noise on a specific run. The 266K-drawer `exocortex.dashboard` room is naturally excluded by not being on the allowlist; the explicit exclude is belt-and-suspenders.

**Engine choices:**
- `claude` (default) — uses `scripts/lib/claude_cli.sh` wrapper from CLAUDE.md. Strips harness env vars, calls `claude -p` with a structured extraction prompt. Returns JSON with triples + diary candidates.
- `heuristic` — uses existing `mempalace.general_extractor.extract_memories()`. Zero cost, lower recall. Useful for cheap pre-filter.
- `both` — heuristic first to flag drawers worth LLM extraction; only LLM-process flagged ones. Best cost/quality.

**State:** `~/.mempalace/curation_state.json` — `{last_run_iso, drawers_processed: [drawer_ids], stats}`. Idempotent + resumable.

**Why background:** captures the long tail of facts buried in 344K drawers that no AI ever saw live. Backfills the KG.

### Verbatim Reconciliation

KG triples now carry `source_drawer_id` (already supported via `source_closet` parameter — we'll standardize). Following any KG fact takes you to the verbatim drawer. The KG is treated as the AAAK-style index layer; drawers remain the source of truth. **No summarization of drawer contents** — the KG is *new structured data extracted as a sibling to* the verbatim text, not a lossy compression of it.

### Data Model

`tool_kg_add` already accepts `source_closet` (used as drawer-id pointer here). No schema change required for v1. We will:
- Use `source_closet=<drawer_id>` to pin every triple to a drawer.
- Add a `valid_from` of "today" (or the drawer's filed timestamp when known) so the temporal model is correct.

`tool_diary_write` is unchanged. Pipeline B will use `agent_name="curator"` so curator entries are distinguishable from live-agent diaries.

## Components

| Path | Purpose | New / Modified |
|------|---------|----------------|
| `hooks/mempal_save_hook.sh` | Default flips to verbose; interval 30; new prompt | Modified |
| `hooks/mempal_precompact_hook.sh` | Same prompt as save hook | Modified |
| `mempalace/curator.py` | Background curation engine | New |
| `mempalace/cli.py` | Add `mempalace curate` subcommand | Modified |
| `scripts/lib/extract_prompts.py` | Prompts for Claude CLI extraction | New |
| `tests/test_curator.py` | Curator unit tests | New |
| `tests/test_save_hook.py` | Hook prompt format tests | Modified |

## Data Flow — Pipeline B Walkthrough

1. User runs `mempalace curate --since 7` (or scheduled task fires it nightly).
2. Curator queries the palace for drawers added in last 7 days, filtered by allowlist of rooms (`general`, `documentation`, `decisions`, `diary`, `journals`, `proposals`) and excluded wings.
3. For each drawer, run `general_extractor.extract_memories()` heuristic. If any markers fire, queue the drawer for LLM extraction.
4. Batch queued drawers (10 at a time) → `claude -p` with the structured extraction prompt. Prompt asks for JSON: `{triples: [{subject, predicate, object, valid_from}], observations: [...]}`.
5. Parse JSON. For each triple, call `tool_kg_add(...source_closet=drawer_id)`. For each observation worth recording, append to a curator diary entry.
6. Mark drawer IDs as processed in `~/.mempalace/curation_state.json`. Atomic write.
7. End-of-run: write one curator diary entry summarizing the run.

## Auto-Selected Decisions

The user opted into autonomous completion after Q1+Q2. The remaining decisions were picked using project-context defaults:

| # | Decision | Picked | Alternatives rejected | Override hint |
|---|----------|--------|----------------------|---------------|
| Q3 | Extraction engine for Pipeline B | `claude` (Claude CLI via `claude_cli.sh`), with `heuristic` pre-filter in `--engine both` | Codex CLI (better for cross-AI critique, not batch); local Ollama (overkill, more setup); heuristic-only (low recall on relationship triples) | Pass `--engine codex` once we add a Codex backend, or run with `--engine heuristic` for zero-cost mode |
| Q4 | Diary enforcement mechanism | `MEMPAL_VERBOSE=true` default, `SAVE_INTERVAL=30`, structured AAAK + KG-triple prompt | Silent-only (current state, demonstrably ineffective — 8 entries in months); precompact-only (only fires on long sessions); SessionEnd hook (Claude Code Stop already covers this) | `export MEMPAL_VERBOSE=false` to revert; tune `SAVE_INTERVAL` in the hook |
| Q5 | Verbatim reconciliation | KG triples carry `source_closet=drawer_id`; KG is index layer, drawers are source of truth. No summarization of drawer contents. | Store summaries in drawers (violates verbatim principle); KG as standalone (loses provenance) | N/A — load-bearing per design principles |
| Q6 | Backfill scope | Last 90 days, restricted to high-signal rooms, excluding `exocortex.dashboard` (266K data drawers). `--since 90` for first run. | Backfill all 344K (cost-prohibitive, low ROI on code chunks); forward-only (KG would never reach useful coverage) | Pass `--since N` and `--rooms` to override |
| Q7 | Success metric | KG ≥100 entities + ≥500 triples within 30 days; diary ≥10 new agent entries within 7 days; manual smoke test: `mempalace_search "what did I decide about MemPalace v3.3.2"` returns the diary entry from this session, not source code. | "It populates" (untestable); per-wing thresholds (over-specified for v1) | Tune in `tests/test_curator.py::test_smoke_targets` |

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Claude CLI extraction wrong / hallucinated triples | `source_closet` pins every triple to a drawer; `kg_invalidate` available to retract; curator diary entries make extraction runs auditable; `--dry-run` mode for inspection |
| Save-hook interruption annoys user | `MEMPAL_VERBOSE=false` opt-out; `SAVE_INTERVAL=30` is roomy; structured prompt is concise so the AI's response is short |
| Daily compute cost blows up | `--max-drawers` daily cap; `compute-guard` skill already enforces a budget on `claude -p` calls; heuristic pre-filter in `--engine both` reduces LLM calls 10x |
| Curator state file corrupted mid-run | Atomic write via temp file + rename; resume reads last good state; idempotent triple writes (KG dedupes via `(subject, predicate, object, valid_from)` key — needs verification) |
| Schema drift if upstream changes `kg_add` signature | We're on v3.3.2 just-merged; pin to MCP tool surface, write integration test that exercises real `kg_add` |
| KG fills with noise (every "we decided to use snake_case" becomes a triple) | Extraction prompt explicitly asks only for facts about *people, projects, and entities* — not coding conventions; manual review of first run's curator diary |

## Success Criteria

After 30 days of normal use:
- `mempalace_kg_stats` returns ≥100 entities, ≥500 triples.
- `mempalace_search "what did Alan decide about X"` returns a curator-extracted observation or diary entry, not raw source code, for at least 5 representative `X` values.
- Each major active wing (`exocortex`, `mempalace`, `gsd`, `openclaude`) has ≥10 entities in the KG.
- At least 10 new diary entries written by `wing_claude` or `wing_curator` since the change shipped.
- No more than 1 user-reported "the save hook interrupts me too often" complaint.

If we miss these by week 2, fall back to `MEMPAL_VERBOSE=false` and lean harder on Pipeline B (raise `--max-drawers` cap, run nightly).

## Out of Scope (Explicitly)

- Migrating to a different KG backend (sqlite is fine for current scale).
- Adding new MCP tools (existing `kg_add` / `diary_write` are sufficient).
- Updating the upstream landing page or website docs.
- Backfilling the 266K ExoCortex `dashboard` drawers (excluded by design).
- Real-time streaming extraction during conversation (Pipeline A's hook block is sufficient).

## Implementation Order

1. `mempalace/curator.py` skeleton + heuristic-only mode + state file.
2. `mempalace curate` CLI subcommand wiring.
3. Tests for filter logic, state persistence, idempotency.
4. Claude CLI extraction backend + prompt files.
5. Tests with a recorded fixture (offline — record one real `claude -p` response, replay in test).
6. Hook updates (`MEMPAL_VERBOSE=true` default, prompt rewrite, interval bump).
7. End-to-end smoke test: file a fake drawer, run curator, assert triple appears in KG with `source_closet` set.
8. First real run with `--since 7 --rooms decisions,diary,proposals` (smallest safe scope), inspect curator diary, tune.
9. Commit each step atomically.

The writing-plans skill will turn this into a step-by-step plan with file-level changes.
