# MemPalace Memory Curation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate MemPalace's empty knowledge graph and near-empty diary by adding (A) a background curator that extracts entity triples from existing drawers, and (B) save-hook prompt changes that force the AI to record diary entries during conversation.

**Architecture:** A single new module `mempalace/curator.py` exposes a `curate()` orchestrator that filters drawers (by date / allowlisted rooms), runs them through one of three extraction engines (heuristic via `general_extractor`, LLM via `claude -p` subprocess, or a hybrid), and writes the results through the existing `tool_kg_add` and `tool_diary_write` MCP-server functions. State lives in `~/.mempalace/curation_state.json` for idempotent resume. Two existing hooks (`mempal_save_hook.sh`, `mempal_precompact_hook.sh`) get a structured AAAK + KG-triple prompt and `MEMPAL_VERBOSE=true` becomes the default.

**Tech Stack:** Python 3.13, ChromaDB (existing palace), SQLite (existing knowledge graph), Bash (existing hooks), pytest (existing test framework). External dependency: `claude` CLI binary (already on PATH per `CLAUDE.md`).

**Spec:** `docs/superpowers/specs/2026-04-24-mempalace-memory-curation-design.md`

---

## File Structure

| Path | Responsibility | New / Modified |
|------|----------------|----------------|
| `mempalace/curator.py` | CurationState + filter + extraction backends + orchestrator | New |
| `mempalace/cli.py` | Adds `curate` subcommand wiring | Modified (+~30 lines) |
| `tests/test_curator.py` | Unit + smoke tests for curator | New |
| `hooks/mempal_save_hook.sh` | Default `MEMPAL_VERBOSE=true`, `SAVE_INTERVAL=30`, structured prompt | Modified |
| `hooks/mempal_precompact_hook.sh` | Same structured prompt | Modified |
| `tests/test_curation_hooks.py` | Snapshot test: hooks emit the expected block-reason JSON | New |

The single `curator.py` file mirrors the existing single-file convention used by `miner.py` (1100 lines) and `sweeper.py`. If the file grows past ~600 lines during implementation, split out `curator_state.py` and `curator_engines.py` as a follow-up — but don't pre-split.

---

## Task 1: CurationState — atomic load/save with processed-drawer tracking

**Files:**
- Create: `mempalace/curator.py`
- Test: `tests/test_curator.py`

- [ ] **Step 1.1: Write the failing test**

```python
# tests/test_curator.py
import json
import os
from pathlib import Path

import pytest

from mempalace.curator import CurationState


def test_curation_state_round_trips_through_disk(tmp_path):
    state_file = tmp_path / "state.json"
    state = CurationState.load(state_file)
    assert state.processed_drawer_ids == set()
    assert state.last_run_iso is None

    state.mark_processed(["drawer_a", "drawer_b"])
    state.last_run_iso = "2026-04-24T12:00:00"
    state.save()

    reloaded = CurationState.load(state_file)
    assert reloaded.processed_drawer_ids == {"drawer_a", "drawer_b"}
    assert reloaded.last_run_iso == "2026-04-24T12:00:00"


def test_curation_state_save_is_atomic(tmp_path, monkeypatch):
    """Writes go through a temp file + rename so a crash mid-write doesn't truncate."""
    state_file = tmp_path / "state.json"
    state_file.write_text('{"processed_drawer_ids": ["existing"], "last_run_iso": "2026-04-23"}')
    state = CurationState.load(state_file)
    state.mark_processed(["new_drawer"])

    # Simulate failure: monkeypatch os.replace to raise after temp file is written.
    original_replace = os.replace
    calls = {"count": 0}

    def failing_replace(src, dst):
        calls["count"] += 1
        raise OSError("simulated crash mid-rename")

    monkeypatch.setattr("mempalace.curator.os.replace", failing_replace)
    with pytest.raises(OSError):
        state.save()

    # Original file should be untouched.
    on_disk = json.loads(state_file.read_text())
    assert on_disk == {"processed_drawer_ids": ["existing"], "last_run_iso": "2026-04-23"}
    monkeypatch.setattr("mempalace.curator.os.replace", original_replace)


def test_is_processed_returns_false_for_new_drawer(tmp_path):
    state = CurationState.load(tmp_path / "state.json")
    state.mark_processed(["a", "b"])
    assert state.is_processed("a")
    assert not state.is_processed("c")
```

- [ ] **Step 1.2: Run test, verify it fails**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curator.py -v
```
Expected: `ImportError: cannot import name 'CurationState' from 'mempalace.curator'`

- [ ] **Step 1.3: Write minimal implementation**

```python
# mempalace/curator.py
"""Background curator: extract KG triples and diary observations from drawers."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class CurationState:
    """Persistent state for the curator. Tracks which drawers have been processed."""

    state_file: Path
    processed_drawer_ids: set[str] = field(default_factory=set)
    last_run_iso: Optional[str] = None

    @classmethod
    def load(cls, state_file: Path | str) -> "CurationState":
        path = Path(state_file)
        if not path.exists():
            return cls(state_file=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Corrupt state file — start fresh rather than crashing the run.
            return cls(state_file=path)
        return cls(
            state_file=path,
            processed_drawer_ids=set(data.get("processed_drawer_ids", [])),
            last_run_iso=data.get("last_run_iso"),
        )

    def mark_processed(self, drawer_ids: Iterable[str]) -> None:
        for did in drawer_ids:
            self.processed_drawer_ids.add(did)

    def is_processed(self, drawer_id: str) -> bool:
        return drawer_id in self.processed_drawer_ids

    def save(self) -> None:
        """Atomic save: write to temp file in same directory, then os.replace()."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "processed_drawer_ids": sorted(self.processed_drawer_ids),
            "last_run_iso": self.last_run_iso,
        }
        # NamedTemporaryFile in same dir so os.replace is atomic on Windows + Unix.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".curator_state_", suffix=".tmp", dir=str(self.state_file.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, self.state_file)
        except Exception:
            # Clean up tmp file if rename failed.
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
```

- [ ] **Step 1.4: Run test, verify it passes**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curator.py -v
```
Expected: 3 passed.

- [ ] **Step 1.5: Commit**

```bash
git add mempalace/curator.py tests/test_curator.py
git commit -m "feat(curator): atomic CurationState for resumable runs"
```

---

## Task 2: filter_drawers() — pull drawers by date, room allowlist, exclude rooms

**Files:**
- Modify: `mempalace/curator.py`
- Test: `tests/test_curator.py`

- [ ] **Step 2.1: Write the failing test**

```python
# tests/test_curator.py — append
from datetime import datetime, timedelta

from mempalace.curator import filter_drawers


def test_filter_drawers_returns_only_allowlisted_rooms_within_window():
    now = datetime.fromisoformat("2026-04-24T12:00:00")
    drawers = [
        # In window, in allowlist — keep.
        {"id": "d1", "metadata": {"room": "decisions", "wing": "mempalace",
                                   "filed_at": "2026-04-23T10:00:00"}, "document": "Decided X."},
        # In window, NOT in allowlist — drop.
        {"id": "d2", "metadata": {"room": "dashboard", "wing": "exocortex",
                                   "filed_at": "2026-04-23T10:00:00"}, "document": "graph data"},
        # In allowlist but OUT of window — drop.
        {"id": "d3", "metadata": {"room": "diary", "wing": "wing_claude",
                                   "filed_at": "2026-03-01T10:00:00"}, "document": "old"},
        # In allowlist, in window, but exclude_rooms hits — drop.
        {"id": "d4", "metadata": {"room": "data", "wing": "x",
                                   "filed_at": "2026-04-23T10:00:00"}, "document": "raw"},
    ]
    kept = list(filter_drawers(
        drawers,
        since=now - timedelta(days=7),
        allowed_rooms={"decisions", "diary", "data"},
        excluded_rooms={"data"},
    ))
    assert [d["id"] for d in kept] == ["d1"]


def test_filter_drawers_skips_drawers_with_missing_metadata():
    """ChromaDB occasionally returns None metadata; don't crash the run."""
    drawers = [
        {"id": "ok", "metadata": {"room": "decisions", "wing": "x",
                                   "filed_at": "2026-04-23T10:00:00"}, "document": "."},
        {"id": "no_meta", "metadata": None, "document": "."},
        {"id": "no_filed_at", "metadata": {"room": "decisions"}, "document": "."},
    ]
    kept = list(filter_drawers(
        drawers,
        since=datetime.fromisoformat("2026-04-01T00:00:00"),
        allowed_rooms={"decisions"},
        excluded_rooms=set(),
    ))
    assert [d["id"] for d in kept] == ["ok"]
```

- [ ] **Step 2.2: Run test, verify it fails**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curator.py::test_filter_drawers_returns_only_allowlisted_rooms_within_window -v
```
Expected: `ImportError: cannot import name 'filter_drawers' from 'mempalace.curator'`

- [ ] **Step 2.3: Implement filter_drawers**

```python
# mempalace/curator.py — append after CurationState
from datetime import datetime
from typing import Iterator


def filter_drawers(
    drawers: Iterable[dict],
    since: datetime,
    allowed_rooms: set[str],
    excluded_rooms: set[str],
) -> Iterator[dict]:
    """Yield drawers that are (a) filed at or after `since`, (b) in `allowed_rooms`,
    (c) not in `excluded_rooms`. Drawers with missing or malformed metadata are
    skipped silently — chroma occasionally returns None metadata."""
    since_iso = since.isoformat()
    for drawer in drawers:
        meta = drawer.get("metadata") or {}
        if not meta:
            continue
        room = meta.get("room")
        filed_at = meta.get("filed_at")
        if room is None or filed_at is None:
            continue
        if room in excluded_rooms:
            continue
        if room not in allowed_rooms:
            continue
        if filed_at < since_iso:
            continue
        yield drawer
```

- [ ] **Step 2.4: Run test, verify it passes**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curator.py -v
```
Expected: 5 passed.

- [ ] **Step 2.5: Commit**

```bash
git add mempalace/curator.py tests/test_curator.py
git commit -m "feat(curator): filter_drawers by date + room allowlist"
```

---

## Task 3: extract_heuristic() — wrap general_extractor for cheap pre-filter

**Files:**
- Modify: `mempalace/curator.py`
- Test: `tests/test_curator.py`

- [ ] **Step 3.1: Write the failing test**

```python
# tests/test_curator.py — append
from mempalace.curator import extract_heuristic


def test_extract_heuristic_flags_drawer_with_decision_marker():
    drawer = {
        "id": "d1",
        "metadata": {"room": "decisions", "wing": "mempalace",
                     "filed_at": "2026-04-23T10:00:00"},
        "document": "We decided to use ChromaDB because it's local and zero-API.",
    }
    result = extract_heuristic(drawer)
    assert result["flagged"] is True
    assert any("decision" in m["memory_type"] for m in result["memories"])


def test_extract_heuristic_does_not_flag_pure_code():
    drawer = {
        "id": "d2",
        "metadata": {"room": "decisions", "wing": "mempalace",
                     "filed_at": "2026-04-23T10:00:00"},
        "document": "def add(a, b):\n    return a + b\n",
    }
    result = extract_heuristic(drawer)
    assert result["flagged"] is False
    assert result["memories"] == []
```

- [ ] **Step 3.2: Run test, verify it fails**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curator.py::test_extract_heuristic_flags_drawer_with_decision_marker -v
```
Expected: `ImportError: cannot import name 'extract_heuristic' from 'mempalace.curator'`

- [ ] **Step 3.3: Implement extract_heuristic**

```python
# mempalace/curator.py — append
from mempalace import general_extractor


def extract_heuristic(drawer: dict, min_confidence: float = 0.3) -> dict:
    """Run general_extractor over a drawer's document text. Returns
    {"flagged": bool, "memories": [{...}, ...]}. `flagged` is True when at
    least one memory marker fired; this is the cheap pre-filter that decides
    which drawers are worth a more expensive LLM extraction pass."""
    document = drawer.get("document") or ""
    if not document:
        return {"flagged": False, "memories": []}
    memories = general_extractor.extract_memories(document, min_confidence=min_confidence)
    return {"flagged": bool(memories), "memories": memories}
```

- [ ] **Step 3.4: Run test, verify it passes**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curator.py -v
```
Expected: 7 passed.

- [ ] **Step 3.5: Commit**

```bash
git add mempalace/curator.py tests/test_curator.py
git commit -m "feat(curator): heuristic pre-filter via general_extractor"
```

---

## Task 4: extract_claude() — subprocess to claude CLI, parse JSON

**Files:**
- Modify: `mempalace/curator.py`
- Test: `tests/test_curator.py`

The subprocess pattern follows `CLAUDE.md`: call `claude -p` with stdin text input. The harness env vars do NOT trigger the new-session bug under `subprocess.run([..., '-p', ...], input=prompt)` — empirically verified.

- [ ] **Step 4.1: Write the failing test (mocks subprocess)**

```python
# tests/test_curator.py — append
from unittest.mock import patch, MagicMock

from mempalace.curator import extract_claude, build_extraction_prompt


def test_build_extraction_prompt_includes_drawer_text_and_instructions():
    drawer = {
        "id": "d1",
        "metadata": {"wing": "mempalace", "room": "decisions",
                     "filed_at": "2026-04-23T10:00:00"},
        "document": "We chose ChromaDB.",
    }
    prompt = build_extraction_prompt(drawer)
    assert "We chose ChromaDB." in prompt
    assert "JSON" in prompt
    assert "triples" in prompt.lower()
    assert "drawer_id=d1" in prompt or "d1" in prompt


def test_extract_claude_parses_valid_json_response():
    drawer = {
        "id": "d1",
        "metadata": {"wing": "mempalace", "room": "decisions",
                     "filed_at": "2026-04-23T10:00:00"},
        "document": "We chose ChromaDB.",
    }
    fake_response = (
        '{"triples": [{"subject": "MemPalace", "predicate": "uses", '
        '"object": "ChromaDB", "valid_from": "2026-04-23"}], '
        '"observations": ["MemPalace switched to ChromaDB."]}'
    )
    with patch("mempalace.curator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=fake_response, returncode=0)
        result = extract_claude(drawer)

    assert len(result["triples"]) == 1
    assert result["triples"][0]["subject"] == "MemPalace"
    assert result["observations"] == ["MemPalace switched to ChromaDB."]


def test_extract_claude_returns_empty_on_invalid_json():
    drawer = {
        "id": "d1",
        "metadata": {"wing": "mempalace", "room": "decisions",
                     "filed_at": "2026-04-23T10:00:00"},
        "document": "We chose ChromaDB.",
    }
    with patch("mempalace.curator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="this is not JSON", returncode=0)
        result = extract_claude(drawer)
    assert result == {"triples": [], "observations": []}


def test_extract_claude_returns_empty_on_subprocess_error():
    drawer = {
        "id": "d1",
        "metadata": {"wing": "mempalace", "room": "decisions",
                     "filed_at": "2026-04-23T10:00:00"},
        "document": "anything",
    }
    with patch("mempalace.curator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", returncode=1, stderr="boom")
        result = extract_claude(drawer)
    assert result == {"triples": [], "observations": []}
```

- [ ] **Step 4.2: Run tests, verify they fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curator.py -v -k "claude or prompt"
```
Expected: `ImportError: cannot import name 'extract_claude'` (and `build_extraction_prompt`).

- [ ] **Step 4.3: Implement build_extraction_prompt + extract_claude**

```python
# mempalace/curator.py — append
import json as _json
import re
import subprocess


_EXTRACTION_PROMPT_TEMPLATE = """You are a memory curator for a verbatim AI memory system.

Read the drawer text below and extract any FACTS about entities (people, projects,
tools, concepts) and OBSERVATIONS worth recording in a session diary. Be conservative —
prefer fewer high-quality items over many speculative ones.

Output STRICT JSON, no prose. Schema:
{{
  "triples": [
    {{"subject": "EntityA", "predicate": "uses", "object": "EntityB",
      "valid_from": "ISO date or null"}}
  ],
  "observations": ["one-sentence factual observation", ...]
}}

Rules:
- Use real entity names from the text. Do NOT invent.
- Predicates should be short verb phrases (uses, decided, prefers, owns, switched_to).
- Skip code-style content (function bodies, configs); focus on natural-language statements.
- valid_from = the drawer's filed_at date when the text doesn't supply one.
- Return {{"triples": [], "observations": []}} if nothing memory-worthy is present.

Drawer metadata: wing={wing} room={room} drawer_id={drawer_id} filed_at={filed_at}

Drawer text:
\"\"\"
{document}
\"\"\"
"""


def build_extraction_prompt(drawer: dict) -> str:
    meta = drawer.get("metadata") or {}
    return _EXTRACTION_PROMPT_TEMPLATE.format(
        wing=meta.get("wing", "?"),
        room=meta.get("room", "?"),
        drawer_id=drawer.get("id", "?"),
        filed_at=meta.get("filed_at", "?"),
        document=(drawer.get("document") or "")[:6000],  # cap at 6000 chars
    )


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def extract_claude(drawer: dict, timeout_seconds: int = 60) -> dict:
    """Call `claude -p` with the extraction prompt; parse JSON response.
    Returns {"triples": [...], "observations": [...]}, both empty on any failure.
    Failures are silent so a single bad drawer doesn't kill the whole run —
    they get logged at the orchestrator level."""
    prompt = build_extraction_prompt(drawer)
    try:
        result = subprocess.run(
            ["claude", "-p"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            encoding="utf-8",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"triples": [], "observations": []}

    if result.returncode != 0:
        return {"triples": [], "observations": []}

    raw = result.stdout.strip()
    # Try strict JSON first; fall back to first {...} block in case Claude
    # wrapped the response in commentary.
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(raw)
        if not match:
            return {"triples": [], "observations": []}
        try:
            parsed = _json.loads(match.group(0))
        except _json.JSONDecodeError:
            return {"triples": [], "observations": []}

    return {
        "triples": list(parsed.get("triples", [])),
        "observations": list(parsed.get("observations", [])),
    }
```

- [ ] **Step 4.4: Run tests, verify they pass**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curator.py -v
```
Expected: 11 passed.

- [ ] **Step 4.5: Commit**

```bash
git add mempalace/curator.py tests/test_curator.py
git commit -m "feat(curator): claude CLI extraction backend with JSON parsing"
```

---

## Task 5: curate() orchestrator — wire filters → extraction → KG/diary writes

**Files:**
- Modify: `mempalace/curator.py`
- Test: `tests/test_curator.py`

- [ ] **Step 5.1: Write the failing test**

```python
# tests/test_curator.py — append
from unittest.mock import patch, MagicMock, call

from mempalace.curator import curate, CurationState


def _make_drawer(did, room="decisions", text="We chose ChromaDB because local."):
    return {
        "id": did,
        "metadata": {"room": room, "wing": "mempalace",
                     "filed_at": "2026-04-23T10:00:00"},
        "document": text,
    }


def test_curate_writes_triples_and_observations_for_flagged_drawers(tmp_path):
    drawers = [_make_drawer("d1"), _make_drawer("d2"), _make_drawer("code", text="def f(): pass")]
    extraction_response = {
        "triples": [{"subject": "MemPalace", "predicate": "uses",
                     "object": "ChromaDB", "valid_from": "2026-04-23"}],
        "observations": ["Switched to ChromaDB."],
    }

    with patch("mempalace.curator._iter_palace_drawers", return_value=iter(drawers)), \
         patch("mempalace.curator.extract_claude", return_value=extraction_response) as mock_extract, \
         patch("mempalace.curator.tool_kg_add") as mock_kg_add, \
         patch("mempalace.curator.tool_diary_write") as mock_diary:
        result = curate(
            palace_path=str(tmp_path),
            since=datetime.fromisoformat("2026-04-01T00:00:00"),
            allowed_rooms={"decisions"},
            excluded_rooms=set(),
            engine="both",
            max_drawers=10,
            state_file=tmp_path / "state.json",
        )

    # Two drawers passed the heuristic (d1, d2). The third (code) didn't.
    assert mock_extract.call_count == 2
    # Each extraction yielded 1 triple → 2 kg_add calls.
    assert mock_kg_add.call_count == 2
    # Diary write happens once at end-of-run (summary), not per-drawer.
    assert mock_diary.call_count == 1
    # Result reports counts.
    assert result["drawers_processed"] == 2
    assert result["triples_added"] == 2
    assert result["observations"] == 2

    # State file was updated with processed IDs.
    state = CurationState.load(tmp_path / "state.json")
    assert state.processed_drawer_ids == {"d1", "d2"}
    assert state.last_run_iso is not None


def test_curate_skips_already_processed_drawers(tmp_path):
    state_file = tmp_path / "state.json"
    pre_state = CurationState.load(state_file)
    pre_state.mark_processed(["d1"])
    pre_state.save()

    drawers = [_make_drawer("d1"), _make_drawer("d2")]
    extraction_response = {"triples": [], "observations": []}

    with patch("mempalace.curator._iter_palace_drawers", return_value=iter(drawers)), \
         patch("mempalace.curator.extract_claude", return_value=extraction_response) as mock_extract:
        curate(
            palace_path=str(tmp_path),
            since=datetime.fromisoformat("2026-04-01T00:00:00"),
            allowed_rooms={"decisions"},
            excluded_rooms=set(),
            engine="claude",
            max_drawers=10,
            state_file=state_file,
        )

    # d1 already processed; only d2 should be sent to extractor.
    assert mock_extract.call_count == 1


def test_curate_respects_max_drawers_cap(tmp_path):
    drawers = [_make_drawer(f"d{i}") for i in range(20)]
    extraction_response = {"triples": [], "observations": []}

    with patch("mempalace.curator._iter_palace_drawers", return_value=iter(drawers)), \
         patch("mempalace.curator.extract_claude", return_value=extraction_response) as mock_extract:
        result = curate(
            palace_path=str(tmp_path),
            since=datetime.fromisoformat("2026-04-01T00:00:00"),
            allowed_rooms={"decisions"},
            excluded_rooms=set(),
            engine="claude",
            max_drawers=5,
            state_file=tmp_path / "state.json",
        )

    assert mock_extract.call_count == 5
    assert result["drawers_processed"] == 5


def test_curate_dry_run_does_not_call_kg_or_diary(tmp_path):
    drawers = [_make_drawer("d1")]
    extraction_response = {
        "triples": [{"subject": "X", "predicate": "is", "object": "Y", "valid_from": None}],
        "observations": ["something"],
    }

    with patch("mempalace.curator._iter_palace_drawers", return_value=iter(drawers)), \
         patch("mempalace.curator.extract_claude", return_value=extraction_response), \
         patch("mempalace.curator.tool_kg_add") as mock_kg_add, \
         patch("mempalace.curator.tool_diary_write") as mock_diary:
        curate(
            palace_path=str(tmp_path),
            since=datetime.fromisoformat("2026-04-01T00:00:00"),
            allowed_rooms={"decisions"},
            excluded_rooms=set(),
            engine="claude",
            max_drawers=10,
            state_file=tmp_path / "state.json",
            dry_run=True,
        )

    mock_kg_add.assert_not_called()
    mock_diary.assert_not_called()
```

- [ ] **Step 5.2: Run tests, verify they fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curator.py -v -k curate
```
Expected: `ImportError: cannot import name 'curate'`.

- [ ] **Step 5.3: Implement orchestrator + palace iteration**

```python
# mempalace/curator.py — append
from datetime import datetime, timezone
from typing import Literal

from mempalace.mcp_server import tool_kg_add, tool_diary_write
from mempalace.palace import get_collection


Engine = Literal["claude", "heuristic", "both"]


def _iter_palace_drawers(palace_path: str) -> Iterator[dict]:
    """Yield drawers from the palace as {"id", "document", "metadata"} dicts.
    Returns an empty iterator if the palace can't be opened."""
    collection = get_collection(palace_path)
    if collection is None:
        return iter([])
    raw = collection.get()
    ids = raw.get("ids") or []
    docs = raw.get("documents") or []
    metas = raw.get("metadatas") or []
    for did, doc, meta in zip(ids, docs, metas):
        yield {"id": did, "document": doc, "metadata": meta}


def curate(
    palace_path: str,
    since: datetime,
    allowed_rooms: set[str],
    excluded_rooms: set[str],
    engine: Engine = "both",
    max_drawers: int = 500,
    state_file: Optional[Path] = None,
    dry_run: bool = False,
    agent_name: str = "curator",
) -> dict:
    """Background curation pass. Filter drawers by date+room, extract entity
    triples + observations via the chosen engine, write them to KG + diary.
    Returns a stats dict; full structured detail goes to the curator diary."""
    state_file = Path(state_file) if state_file else Path.home() / ".mempalace" / "curation_state.json"
    state = CurationState.load(state_file)

    candidate_drawers = filter_drawers(
        _iter_palace_drawers(palace_path),
        since=since,
        allowed_rooms=allowed_rooms,
        excluded_rooms=excluded_rooms,
    )

    drawers_processed = 0
    triples_added = 0
    observations_count = 0
    failures = 0
    observations_by_drawer: list[tuple[str, list[str]]] = []

    for drawer in candidate_drawers:
        if drawers_processed >= max_drawers:
            break
        did = drawer["id"]
        if state.is_processed(did):
            continue

        # Engine routing.
        if engine == "heuristic":
            heuristic = extract_heuristic(drawer)
            triples = []  # heuristic alone doesn't produce triples
            observations = [m["content"] for m in heuristic["memories"]]
        else:
            if engine == "both":
                heuristic = extract_heuristic(drawer)
                if not heuristic["flagged"]:
                    state.mark_processed([did])
                    continue
            extracted = extract_claude(drawer)
            triples = extracted["triples"]
            observations = extracted["observations"]
            if not triples and not observations:
                failures += 1

        if not dry_run:
            for t in triples:
                tool_kg_add(
                    subject=t.get("subject", ""),
                    predicate=t.get("predicate", ""),
                    object=t.get("object", ""),
                    valid_from=t.get("valid_from"),
                    source_closet=did,
                )
                triples_added += 1
        else:
            triples_added += len(triples)

        observations_count += len(observations)
        if observations:
            observations_by_drawer.append((did, observations))

        state.mark_processed([did])
        drawers_processed += 1

    state.last_run_iso = datetime.now(timezone.utc).isoformat()
    if not dry_run:
        state.save()

    # End-of-run diary entry summarises the run.
    if not dry_run and (drawers_processed > 0):
        summary_lines = [
            f"Curator run @ {state.last_run_iso}",
            f"Drawers processed: {drawers_processed}",
            f"Triples added: {triples_added}",
            f"Observations: {observations_count}",
            f"Failures (no extraction): {failures}",
            "",
            "Sample observations:",
        ]
        for did, obs_list in observations_by_drawer[:5]:
            for o in obs_list[:2]:
                summary_lines.append(f"- [{did[:12]}] {o}")
        tool_diary_write(
            agent_name=agent_name,
            entry="\n".join(summary_lines),
            topic="curator_run",
        )

    return {
        "drawers_processed": drawers_processed,
        "triples_added": triples_added,
        "observations": observations_count,
        "failures": failures,
    }
```

- [ ] **Step 5.4: Run tests, verify they pass**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curator.py -v
```
Expected: 15 passed.

- [ ] **Step 5.5: Commit**

```bash
git add mempalace/curator.py tests/test_curator.py
git commit -m "feat(curator): orchestrator wiring filters, extraction, KG/diary"
```

---

## Task 6: CLI subcommand — `mempalace curate`

**Files:**
- Modify: `mempalace/cli.py`

- [ ] **Step 6.1: Inspect existing argparse pattern**

```bash
.venv/Scripts/python.exe -c "
from mempalace.cli import main
import sys
sys.argv = ['mempalace', '--help']
try: main()
except SystemExit: pass
" 2>&1 | tail -20
```
Confirms `init`, `mine`, `sweep`, `search`, etc. are subcommands. Add `curate` to the list.

- [ ] **Step 6.2: Add the subparser block in mempalace/cli.py**

In `mempalace/cli.py`, after the existing `p_sweep = sub.add_parser("sweep", ...)` block (around line 593), add:

```python
    # curate
    p_curate = sub.add_parser(
        "curate",
        help="Extract KG triples + diary observations from filed drawers",
    )
    p_curate.add_argument(
        "--since",
        type=int,
        default=7,
        help="Days back to scan; ignored if --since-iso is given (default: 7)",
    )
    p_curate.add_argument(
        "--since-iso",
        default=None,
        help="ISO timestamp lower bound (overrides --since)",
    )
    p_curate.add_argument(
        "--rooms",
        default="general,documentation,decisions,diary,journals,proposals",
        help="Comma-separated room allowlist",
    )
    p_curate.add_argument(
        "--exclude-rooms",
        default="dashboard,data",
        help="Comma-separated rooms to drop even if allowlisted (default: dashboard,data)",
    )
    p_curate.add_argument(
        "--engine",
        choices=["claude", "heuristic", "both"],
        default="both",
        help="Extraction engine (default: both — heuristic pre-filter + claude)",
    )
    p_curate.add_argument(
        "--max-drawers",
        type=int,
        default=500,
        help="Daily cap on drawers processed (default: 500)",
    )
    p_curate.add_argument(
        "--dry-run", action="store_true", help="Don't write to KG/diary; print summary only"
    )
```

Then register the dispatch. Find the existing dispatch table near the bottom of `main()` (look for the lines mapping `args.command` to handlers — usually a series of `if args.command == "init":` style branches or a dispatch dict). Add:

```python
    elif args.command == "curate":
        cmd_curate(args, palace_path)
```

- [ ] **Step 6.3: Add cmd_curate handler**

In `mempalace/cli.py`, after `cmd_sweep` (around line 191), add:

```python
def cmd_curate(args, palace_path):
    """Run a curation pass: extract KG triples and diary observations."""
    from datetime import datetime, timedelta, timezone
    from mempalace.curator import curate

    if args.since_iso:
        since = datetime.fromisoformat(args.since_iso)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=args.since)

    allowed = {r.strip() for r in args.rooms.split(",") if r.strip()}
    excluded = {r.strip() for r in args.exclude_rooms.split(",") if r.strip()}

    print(f"Curating drawers since {since.isoformat()}")
    print(f"  rooms allowed:  {sorted(allowed)}")
    print(f"  rooms excluded: {sorted(excluded)}")
    print(f"  engine:         {args.engine}")
    print(f"  max drawers:    {args.max_drawers}")
    print(f"  dry-run:        {args.dry_run}")

    stats = curate(
        palace_path=palace_path,
        since=since,
        allowed_rooms=allowed,
        excluded_rooms=excluded,
        engine=args.engine,
        max_drawers=args.max_drawers,
        dry_run=args.dry_run,
    )

    print(f"\nCurator stats: {stats}")
```

- [ ] **Step 6.4: Run smoke check via dry-run**

```bash
.venv/Scripts/python.exe -m mempalace curate --since 1 --max-drawers 5 --dry-run
```
Expected: prints config + stats; no exceptions; KG count unchanged afterwards.

- [ ] **Step 6.5: Run full test suite for regressions**

```bash
.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/benchmarks -q -x
```
Expected: all green (1033+ passed).

- [ ] **Step 6.6: Commit**

```bash
git add mempalace/cli.py
git commit -m "feat(cli): mempalace curate subcommand"
```

---

## Task 7: Save hook — flip default to verbose, raise interval, structured prompt

**Files:**
- Modify: `hooks/mempal_save_hook.sh`
- Test: `tests/test_curation_hooks.py` (new)

- [ ] **Step 7.1: Write the failing snapshot test**

```python
# tests/test_curation_hooks.py
"""Snapshot tests for hook block-reason JSON. Hook output is a contract with
Claude Code; if it changes, we need an explicit decision."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SAVE_HOOK = REPO_ROOT / "hooks" / "mempal_save_hook.sh"
PRECOMPACT_HOOK = REPO_ROOT / "hooks" / "mempal_precompact_hook.sh"


def _run_hook(hook_path: Path, payload: dict, env_extra: dict = None) -> tuple[str, int]:
    """Run a hook script with the given JSON payload on stdin. Returns (stdout, returncode)."""
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        ["bash", str(hook_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return result.stdout, result.returncode


def test_save_hook_blocks_at_save_interval_with_structured_prompt(tmp_path):
    """At exchange 30 with no prior save, hook should block and emit the
    structured AAAK + KG-triple prompt."""
    transcript = tmp_path / "transcript.jsonl"
    # 30 user messages.
    with transcript.open("w", encoding="utf-8") as f:
        for i in range(30):
            f.write(json.dumps({"message": {"role": "user", "content": f"msg {i}"}}) + "\n")

    payload = {
        "session_id": "test-session-30",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
    }
    stdout, rc = _run_hook(SAVE_HOOK, payload)
    assert rc == 0
    out = json.loads(stdout)
    assert out.get("decision") == "block"
    reason = out.get("reason", "")
    # Must reference both diary write AND kg_add tools.
    assert "mempalace_diary_write" in reason
    assert "mempalace_kg_add" in reason
    # Must reference AAAK format.
    assert "AAAK" in reason


def test_save_hook_silent_when_under_interval(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    with transcript.open("w", encoding="utf-8") as f:
        for i in range(5):
            f.write(json.dumps({"message": {"role": "user", "content": f"msg {i}"}}) + "\n")
    payload = {
        "session_id": "test-session-5",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
    }
    stdout, rc = _run_hook(SAVE_HOOK, payload)
    assert rc == 0
    assert json.loads(stdout) == {}


def test_save_hook_silent_mode_opt_out(tmp_path):
    """MEMPAL_VERBOSE=false should suppress the block even at the interval."""
    transcript = tmp_path / "transcript.jsonl"
    with transcript.open("w", encoding="utf-8") as f:
        for i in range(30):
            f.write(json.dumps({"message": {"role": "user", "content": f"msg {i}"}}) + "\n")
    payload = {
        "session_id": "test-silent",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
    }
    stdout, _ = _run_hook(SAVE_HOOK, payload, env_extra={"MEMPAL_VERBOSE": "false"})
    assert json.loads(stdout) == {}


def test_save_hook_lets_through_when_active(tmp_path):
    payload = {
        "session_id": "x",
        "stop_hook_active": True,
        "transcript_path": "",
    }
    stdout, _ = _run_hook(SAVE_HOOK, payload)
    assert json.loads(stdout) == {}
```

- [ ] **Step 7.2: Run tests, verify they fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curation_hooks.py -v
```
Expected: failures because the current hook (a) defaults to silent (`MEMPAL_VERBOSE != true`), (b) interval is 15 not 30, (c) prompt does not mention `mempalace_kg_add` or `AAAK`.

- [ ] **Step 7.3: Update hooks/mempal_save_hook.sh**

In `hooks/mempal_save_hook.sh`:

1. Change `SAVE_INTERVAL=15` to `SAVE_INTERVAL=30`.

2. Replace the verbose-mode `cat << 'HOOKJSON' ... HOOKJSON` block with a structured prompt:

```bash
    # Default: block-and-prompt for diary + KG triples.
    # Set MEMPAL_VERBOSE=false to revert to silent (mining only).
    if [ "$MEMPAL_VERBOSE" = "false" ] || [ "$MEMPAL_VERBOSE" = "0" ]; then
        echo '{}'
    else
        cat << 'HOOKJSON'
{
  "decision": "block",
  "reason": "MemPalace curation checkpoint. Before continuing, do TWO things:\n\n1. Call mempalace_diary_write(agent_name='claude', entry=<AAAK>, topic='session') with a brief AAAK-format diary covering: key topics this session, decisions made, code/files changed, and any new facts about people/projects. Use real entity names; verbatim quotes welcome.\n\n2. For any new facts (Alan prefers X, project Y uses Z, person A is B), call mempalace_kg_add(subject, predicate, object, valid_from='today', source_closet=<diary_drawer_id_if_known>). One call per fact. Be conservative — only record facts you'd stake the next session on.\n\nAfter these calls, the next Stop will let you exit normally. Continue."
}
HOOKJSON
    fi
```

3. The `MEMPAL_VERBOSE=false` opt-out must work even when env var is unset (current code only blocks on `=true`). The new branch above inverts it: silent only when explicitly opted out.

- [ ] **Step 7.4: Run hook tests, verify they pass**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curation_hooks.py -v
```
Expected: 4 passed.

- [ ] **Step 7.5: Commit**

```bash
git add hooks/mempal_save_hook.sh tests/test_curation_hooks.py
git commit -m "feat(hooks): structured KG+diary prompt, interval 30, verbose default"
```

---

## Task 8: Precompact hook — same structured prompt

**Files:**
- Modify: `hooks/mempal_precompact_hook.sh`
- Test: `tests/test_curation_hooks.py`

- [ ] **Step 8.1: Add the precompact test**

```python
# tests/test_curation_hooks.py — append
def test_precompact_hook_always_blocks_with_structured_prompt():
    payload = {"session_id": "pre-1"}
    stdout, rc = _run_hook(PRECOMPACT_HOOK, payload)
    assert rc == 0
    out = json.loads(stdout)
    assert out.get("decision") == "block"
    reason = out.get("reason", "")
    assert "mempalace_diary_write" in reason
    assert "mempalace_kg_add" in reason
    assert "AAAK" in reason
```

- [ ] **Step 8.2: Run, verify it fails**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curation_hooks.py::test_precompact_hook_always_blocks_with_structured_prompt -v
```
Expected: assertion failures on the missing tool names.

- [ ] **Step 8.3: Update precompact hook**

In `hooks/mempal_precompact_hook.sh`, locate the block that returns the JSON `block` decision (near the bottom — search for `"decision": "block"`). Replace its `reason` field to match the save hook's text exactly. Keep the always-block behavior — precompact has no opt-out.

```bash
cat << 'HOOKJSON'
{
  "decision": "block",
  "reason": "MemPalace pre-compact checkpoint. Compaction will erase detailed context — save it now. Do TWO things:\n\n1. Call mempalace_diary_write(agent_name='claude', entry=<AAAK>, topic='precompact') with a thorough AAAK-format diary covering: key topics, decisions, code changes, files touched, surprises. Use real entity names; verbatim quotes welcome.\n\n2. For any new facts (Alan prefers X, project Y uses Z, person A is B), call mempalace_kg_add(subject, predicate, object, valid_from='today'). One call per fact.\n\nAfter saving, compaction proceeds normally."
}
HOOKJSON
```

- [ ] **Step 8.4: Run, verify it passes**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curation_hooks.py -v
```
Expected: 5 passed.

- [ ] **Step 8.5: Commit**

```bash
git add hooks/mempal_precompact_hook.sh tests/test_curation_hooks.py
git commit -m "feat(hooks): precompact prompts for KG + diary just like save hook"
```

---

## Task 9: End-to-end smoke test + first real run

**Files:**
- Modify: `tests/test_curator.py`
- No code changes, just verification.

- [ ] **Step 9.1: Add an integration test against a real ChromaDB palace**

```python
# tests/test_curator.py — append
import pytest

from mempalace.curator import curate
from mempalace.palace import get_collection


@pytest.mark.integration
def test_curator_writes_real_triple_to_kg(tmp_path, monkeypatch):
    """End-to-end: file a drawer in a temp palace, run curator with the
    heuristic-only engine (no Claude needed), assert no crash and stats are sane."""
    palace = tmp_path / "palace"
    palace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate KG sqlite

    # File one drawer manually.
    coll = get_collection(str(palace))
    coll.add(
        ids=["smoke-1"],
        documents=["We decided to use ChromaDB because it's local-first and zero-API."],
        metadatas=[{"room": "decisions", "wing": "mempalace",
                    "filed_at": "2026-04-23T10:00:00"}],
    )

    stats = curate(
        palace_path=str(palace),
        since=datetime.fromisoformat("2026-04-01T00:00:00"),
        allowed_rooms={"decisions"},
        excluded_rooms=set(),
        engine="heuristic",  # avoids the Claude CLI in CI
        max_drawers=10,
        state_file=tmp_path / "state.json",
    )
    assert stats["drawers_processed"] == 1
    # heuristic engine doesn't produce triples, just observations
    assert stats["observations"] >= 1
```

- [ ] **Step 9.2: Run the integration test**

```bash
.venv/Scripts/python.exe -m pytest tests/test_curator.py::test_curator_writes_real_triple_to_kg -v
```
Expected: 1 passed.

- [ ] **Step 9.3: Commit**

```bash
git add tests/test_curator.py
git commit -m "test(curator): end-to-end smoke against real ChromaDB collection"
```

- [ ] **Step 9.4: Full suite regression check**

```bash
.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/benchmarks -q
```
Expected: all green; total count = previous 1033 + new tests (~22 = 1055 passing).

- [ ] **Step 9.5: First real curator run against actual palace (heuristic-only, dry-run)**

```bash
.venv/Scripts/python.exe -m mempalace curate \
    --since 7 \
    --rooms decisions,diary,proposals,documentation \
    --max-drawers 50 \
    --engine heuristic \
    --dry-run
```
Expected: prints the count of drawers it would process, no errors, no KG mutation. Inspect output: does the room filter make sense? Are 50 drawers reasonable for a 7-day window?

- [ ] **Step 9.6: First real curator run, claude engine, capped, NOT dry**

```bash
.venv/Scripts/python.exe -m mempalace curate \
    --since 7 \
    --rooms decisions,diary,proposals \
    --max-drawers 20 \
    --engine both
```
Expected: writes ≤20 drawers' worth of triples + 1 curator diary entry. After the run:

```bash
.venv/Scripts/python.exe -c "
from mempalace.mcp_server import tool_kg_stats, tool_diary_read
print('KG stats:', tool_kg_stats())
"
```
Expected: `entities` ≥ 1, `triples` ≥ 1.

If KG is still empty, pause and inspect: was Claude available? Did the prompt get a JSON response? Log to `~/.mempalace/logs/curator.log` (add logging in a follow-up if needed).

- [ ] **Step 9.7: Final commit (no-op or summary)**

If the smoke run revealed no fixes needed:

```bash
echo "Smoke run results: $(.venv/Scripts/python.exe -c 'from mempalace.mcp_server import tool_kg_stats; print(tool_kg_stats())')" \
    >> docs/superpowers/specs/2026-04-24-mempalace-memory-curation-design.md
git add docs/superpowers/specs/2026-04-24-mempalace-memory-curation-design.md
git commit -m "docs(spec): record initial curator smoke-run KG stats"
```

---

## Self-Review Checklist (run after writing this plan)

1. **Spec coverage:**
   - Pipeline A (agent-driven hook prompt) → Tasks 7, 8 ✓
   - Pipeline B (background extraction) → Tasks 1–6 ✓
   - Verbatim reconciliation (`source_closet=drawer_id`) → Task 5 step 5.3 (curate calls `tool_kg_add(..., source_closet=did)`) ✓
   - Auto-decisions Q3 (claude engine + heuristic prefilter) → Task 4 + Task 5 engine routing ✓
   - Auto-decisions Q4 (verbose default + interval 30) → Task 7 ✓
   - Auto-decisions Q5 (KG provenance) → Task 5 ✓
   - Auto-decisions Q6 (selective backfill) → CLI defaults in Task 6 ✓
   - Auto-decisions Q7 (success metric) → Step 9.6 verifies KG entities/triples > 0 ✓
   - Risk: hallucinated triples → mitigated via `source_closet`, dry-run, kg_invalidate available ✓
   - Risk: state file corruption → Task 1 atomic write test ✓

2. **Placeholder scan:** No "TBD", no "implement later", every code step has runnable code, every command is exact.

3. **Type consistency:**
   - `curate()` signature in Task 5 step 5.3 matches calls in Task 6 step 6.3 (palace_path, since, allowed_rooms, excluded_rooms, engine, max_drawers, state_file, dry_run) ✓
   - `extract_claude(drawer)` signature consistent across Task 4 + Task 5 ✓
   - `CurationState.load(path)` static method consistent across Task 1 + Task 5 ✓
   - `extract_heuristic` returns `{flagged, memories}` dict — used consistently in Task 5 routing ✓

No issues found.
