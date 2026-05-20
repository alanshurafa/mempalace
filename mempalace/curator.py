"""Background curator: extract KG triples and diary observations from drawers.

The curator complements the in-conversation save hook (Pipeline A in the
2026-04-24 design spec) by retroactively scanning drawers that the live
agent never saw. Filters by date + room allowlist; runs heuristic and/or
Claude-CLI extraction; writes triples through tool_kg_add (with source_closet
provenance) and a summary diary entry through tool_diary_write.

State persists to ``~/.mempalace/curation_state.json`` so reruns are
idempotent — a drawer is only sent to the (paid) Claude extractor once.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional


@dataclass
class CurationState:
    """Persistent state for the curator. Tracks which drawers have been processed."""

    state_file: Path
    processed_drawer_ids: set[str] = field(default_factory=set)
    last_run_iso: Optional[str] = None

    @classmethod
    def load(cls, state_file: "Path | str") -> "CurationState":
        path = Path(state_file)
        if not path.exists():
            return cls(state_file=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable state file — start fresh rather than
            # crashing the run. The user can always rebuild from KG/diary.
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
        """Atomic save: write to temp file in same directory, then os.replace().

        On any failure during the rename, the temp file is cleaned up and the
        original state file is left untouched. This means a crash mid-save
        cannot leave a half-written state file behind.
        """
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "processed_drawer_ids": sorted(self.processed_drawer_ids),
            "last_run_iso": self.last_run_iso,
        }
        fd, tmp_path = tempfile.mkstemp(
            prefix=".curator_state_",
            suffix=".tmp",
            dir=str(self.state_file.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, self.state_file)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise


def filter_drawers(
    drawers: Iterable[dict],
    since: datetime,
    allowed_rooms: set[str],
    excluded_rooms: set[str],
) -> Iterator[dict]:
    """Yield drawers that are filed at-or-after ``since``, in ``allowed_rooms``,
    and not in ``excluded_rooms``. Drawers with missing or malformed metadata
    are skipped silently — chroma occasionally returns ``None`` metadata under
    upgrade/migration scenarios."""
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


def extract_heuristic(drawer: dict, min_confidence: float = 0.3) -> dict:
    """Run :mod:`mempalace.general_extractor` over a drawer's document text.

    Returns ``{"flagged": bool, "memories": [...]}``. ``flagged`` is True when
    at least one memory marker fired; this is the cheap pre-filter that
    decides which drawers are worth a more expensive LLM extraction pass.
    """
    from mempalace import general_extractor  # local import to avoid cycles

    document = drawer.get("document") or ""
    if not document:
        return {"flagged": False, "memories": []}
    memories = general_extractor.extract_memories(document, min_confidence=min_confidence)
    return {"flagged": bool(memories), "memories": memories}


# ---------------------------------------------------------------------------
# Claude CLI extraction backend
# ---------------------------------------------------------------------------


_EXTRACTION_PROMPT_TEMPLATE = """You are a memory curator for a verbatim AI memory system.

Read the drawer text below and extract any FACTS about entities (people, projects,
tools, concepts) and OBSERVATIONS worth recording in a session diary. Be conservative
- prefer fewer high-quality items over many speculative ones.

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
    """Build the extraction prompt for a single drawer.

    Document text is capped at 6000 chars — drawer content is bounded by
    miner.CHUNK_SIZE (~800 chars) so this only ever truncates pathological
    cases.
    """
    meta = drawer.get("metadata") or {}
    return _EXTRACTION_PROMPT_TEMPLATE.format(
        wing=meta.get("wing", "?"),
        room=meta.get("room", "?"),
        drawer_id=drawer.get("id", "?"),
        filed_at=meta.get("filed_at", "?"),
        document=(drawer.get("document") or "")[:6000],
    )


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def extract_claude(drawer: dict, timeout_seconds: int = 60) -> dict:
    """Call ``claude -p`` with the extraction prompt; parse JSON response.

    Returns ``{"triples": [...], "observations": [...]}``, both empty on any
    failure (timeout, missing binary, non-zero exit, malformed JSON). Failures
    are silent so a single bad drawer doesn't kill the whole run; the
    orchestrator counts failures and surfaces them in the run summary.
    """
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

    raw = (result.stdout or "").strip()
    # Try strict JSON first; fall back to first {...} block in case Claude
    # wrapped the response in commentary.
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(raw)
        if not match:
            return {"triples": [], "observations": []}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {"triples": [], "observations": []}

    if not isinstance(parsed, dict):
        return {"triples": [], "observations": []}

    return {
        "triples": list(parsed.get("triples", []) or []),
        "observations": list(parsed.get("observations", []) or []),
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


# Lazy-imported to keep curator import time low and avoid eager
# initialization of the MCP server module's KG sqlite handle when callers
# only need state-file utilities.
def _import_mcp_tools():
    from mempalace.mcp_server import tool_kg_add, tool_diary_write

    return tool_kg_add, tool_diary_write


# Bind names at module scope so tests can patch them via
# ``mempalace.curator.tool_kg_add`` / ``mempalace.curator.tool_diary_write``.
try:
    from mempalace.mcp_server import tool_kg_add, tool_diary_write
except Exception:  # pragma: no cover - defensive; mcp_server should always import
    tool_kg_add = None  # type: ignore
    tool_diary_write = None  # type: ignore


def _iter_palace_drawers(
    palace_path: str,
    since_iso: str,
    allowed_rooms: set[str],
    excluded_rooms: set[str],
    fetch_limit: int = 5000,
) -> Iterator[dict]:
    """Yield drawers matching ``filed_at >= since_iso`` and room filters,
    pushing the predicate down to ChromaDB so we don't pull all 344K drawers
    into Python memory.

    Returns an empty iterator if the palace can't be opened.
    """
    from mempalace.palace import get_collection

    collection = get_collection(palace_path)
    if collection is None:
        return

    where_clauses: list[dict] = [{"filed_at": {"$gte": since_iso}}]
    if allowed_rooms:
        where_clauses.append({"room": {"$in": sorted(allowed_rooms)}})
    if excluded_rooms:
        where_clauses.append({"room": {"$nin": sorted(excluded_rooms)}})
    where: dict
    if len(where_clauses) == 1:
        where = where_clauses[0]
    else:
        where = {"$and": where_clauses}

    try:
        raw = collection.get(
            where=where,
            limit=fetch_limit,
            include=["documents", "metadatas"],
        )
    except Exception:
        # Older ChromaDB versions / unexpected schema — fall back to pulling
        # everything and filtering client-side. Still bounded by fetch_limit.
        try:
            raw = collection.get(limit=fetch_limit, include=["documents", "metadatas"])
        except Exception:
            return

    ids = raw.get("ids") or []
    docs = raw.get("documents") or [None] * len(ids)
    metas = raw.get("metadatas") or [None] * len(ids)
    for did, doc, meta in zip(ids, docs, metas):
        yield {"id": did, "document": doc, "metadata": meta}


def curate(
    palace_path: str,
    since: datetime,
    allowed_rooms: set[str],
    excluded_rooms: set[str],
    engine: str = "both",
    max_drawers: int = 500,
    state_file: "Optional[Path | str]" = None,
    dry_run: bool = False,
    agent_name: str = "curator",
) -> dict:
    """Background curation pass.

    Filters drawers by date + room allowlist, runs them through the chosen
    extraction engine, writes triples through ``tool_kg_add`` (with
    ``source_closet`` provenance) and a single end-of-run summary entry
    through ``tool_diary_write``.

    Returns a stats dict; structured detail goes into the curator diary
    entry so each run is auditable from MemPalace itself.
    """
    state_path = (
        Path(state_file)
        if state_file is not None
        else Path.home() / ".mempalace" / "curation_state.json"
    )
    state = CurationState.load(state_path)

    candidate_drawers = _iter_palace_drawers(
        palace_path=palace_path,
        since_iso=since.isoformat(),
        allowed_rooms=allowed_rooms,
        excluded_rooms=excluded_rooms,
    )
    # The unit-test mock substitutes a list iterator that ignores arguments;
    # both real and mocked iterators are consumed the same way below.

    drawers_processed = 0
    triples_added = 0
    observations_count = 0
    failures = 0
    observations_by_drawer: list[tuple[str, list[str]]] = []

    for drawer in candidate_drawers:
        if drawers_processed >= max_drawers:
            break
        did = drawer.get("id")
        if not did or state.is_processed(did):
            continue

        # In production, ChromaDB has already filtered by room/date. The unit
        # tests pass raw drawer lists, so re-apply filter_drawers semantics
        # here as a defense-in-depth check — guards against drawers with
        # missing metadata reaching the extractor.
        meta = drawer.get("metadata") or {}
        room = meta.get("room")
        if room is None or room in excluded_rooms:
            continue
        if allowed_rooms and room not in allowed_rooms:
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

        if not dry_run and tool_kg_add is not None:
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

    if not dry_run and drawers_processed > 0 and tool_diary_write is not None:
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
