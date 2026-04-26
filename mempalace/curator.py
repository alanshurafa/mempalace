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
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
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
    memories = general_extractor.extract_memories(
        document, min_confidence=min_confidence
    )
    return {"flagged": bool(memories), "memories": memories}
