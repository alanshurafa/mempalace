"""Tests for the memory curator (mempalace.curator)."""

import json
import os

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
    state_file.write_text(
        '{"processed_drawer_ids": ["existing"], "last_run_iso": "2026-04-23"}'
    )
    state = CurationState.load(state_file)
    state.mark_processed(["new_drawer"])

    def failing_replace(src, dst):
        raise OSError("simulated crash mid-rename")

    monkeypatch.setattr("mempalace.curator.os.replace", failing_replace)
    with pytest.raises(OSError):
        state.save()

    # Original file should be untouched.
    on_disk = json.loads(state_file.read_text())
    assert on_disk == {
        "processed_drawer_ids": ["existing"],
        "last_run_iso": "2026-04-23",
    }


def test_is_processed_returns_false_for_new_drawer(tmp_path):
    state = CurationState.load(tmp_path / "state.json")
    state.mark_processed(["a", "b"])
    assert state.is_processed("a")
    assert not state.is_processed("c")


def test_curation_state_handles_corrupt_state_file_by_starting_fresh(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("{not valid json")
    state = CurationState.load(state_file)
    assert state.processed_drawer_ids == set()
    assert state.last_run_iso is None


# ---------------------------------------------------------------------------
# filter_drawers
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta

from mempalace.curator import filter_drawers


def test_filter_drawers_returns_only_allowlisted_rooms_within_window():
    now = datetime.fromisoformat("2026-04-24T12:00:00")
    drawers = [
        # In window, in allowlist — keep.
        {"id": "d1", "metadata": {"room": "decisions", "wing": "mempalace",
                                   "filed_at": "2026-04-23T10:00:00"},
         "document": "Decided X."},
        # In window, NOT in allowlist — drop.
        {"id": "d2", "metadata": {"room": "dashboard", "wing": "exocortex",
                                   "filed_at": "2026-04-23T10:00:00"},
         "document": "graph data"},
        # In allowlist but OUT of window — drop.
        {"id": "d3", "metadata": {"room": "diary", "wing": "wing_claude",
                                   "filed_at": "2026-03-01T10:00:00"},
         "document": "old"},
        # In allowlist, in window, but exclude_rooms hits — drop.
        {"id": "d4", "metadata": {"room": "data", "wing": "x",
                                   "filed_at": "2026-04-23T10:00:00"},
         "document": "raw"},
    ]
    kept = list(
        filter_drawers(
            drawers,
            since=now - timedelta(days=7),
            allowed_rooms={"decisions", "diary", "data"},
            excluded_rooms={"data"},
        )
    )
    assert [d["id"] for d in kept] == ["d1"]


def test_filter_drawers_skips_drawers_with_missing_metadata():
    """ChromaDB occasionally returns None metadata; don't crash the run."""
    drawers = [
        {"id": "ok", "metadata": {"room": "decisions", "wing": "x",
                                   "filed_at": "2026-04-23T10:00:00"},
         "document": "."},
        {"id": "no_meta", "metadata": None, "document": "."},
        {"id": "no_filed_at", "metadata": {"room": "decisions"}, "document": "."},
    ]
    kept = list(
        filter_drawers(
            drawers,
            since=datetime.fromisoformat("2026-04-01T00:00:00"),
            allowed_rooms={"decisions"},
            excluded_rooms=set(),
        )
    )
    assert [d["id"] for d in kept] == ["ok"]


# ---------------------------------------------------------------------------
# extract_heuristic
# ---------------------------------------------------------------------------

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
    assert any(m["memory_type"] == "decision" for m in result["memories"])


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


def test_extract_heuristic_handles_empty_document():
    drawer = {
        "id": "empty",
        "metadata": {"room": "decisions", "wing": "x",
                     "filed_at": "2026-04-23T10:00:00"},
        "document": "",
    }
    result = extract_heuristic(drawer)
    assert result["flagged"] is False
    assert result["memories"] == []


# ---------------------------------------------------------------------------
# extract_claude  (subprocess backend, mocked in unit tests)
# ---------------------------------------------------------------------------

from unittest.mock import patch, MagicMock

from mempalace.curator import extract_claude, build_extraction_prompt


def _drawer_for_claude():
    return {
        "id": "d1",
        "metadata": {"wing": "mempalace", "room": "decisions",
                     "filed_at": "2026-04-23T10:00:00"},
        "document": "We chose ChromaDB.",
    }


def test_build_extraction_prompt_includes_drawer_text_and_instructions():
    prompt = build_extraction_prompt(_drawer_for_claude())
    assert "We chose ChromaDB." in prompt
    assert "JSON" in prompt
    assert "triples" in prompt.lower()
    assert "d1" in prompt


def test_extract_claude_parses_valid_json_response():
    fake_response = (
        '{"triples": [{"subject": "MemPalace", "predicate": "uses", '
        '"object": "ChromaDB", "valid_from": "2026-04-23"}], '
        '"observations": ["MemPalace switched to ChromaDB."]}'
    )
    with patch("mempalace.curator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=fake_response, returncode=0, stderr="")
        result = extract_claude(_drawer_for_claude())

    assert len(result["triples"]) == 1
    assert result["triples"][0]["subject"] == "MemPalace"
    assert result["observations"] == ["MemPalace switched to ChromaDB."]


def test_extract_claude_extracts_json_block_from_chatty_response():
    fake_response = (
        "Here is the extraction:\n"
        '{"triples": [], "observations": ["just an obs"]}\n'
        "Hope this helps!"
    )
    with patch("mempalace.curator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=fake_response, returncode=0, stderr="")
        result = extract_claude(_drawer_for_claude())
    assert result == {"triples": [], "observations": ["just an obs"]}


def test_extract_claude_returns_empty_on_invalid_json():
    with patch("mempalace.curator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="this is not JSON", returncode=0, stderr="")
        result = extract_claude(_drawer_for_claude())
    assert result == {"triples": [], "observations": []}


def test_extract_claude_returns_empty_on_subprocess_error():
    with patch("mempalace.curator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", returncode=1, stderr="boom")
        result = extract_claude(_drawer_for_claude())
    assert result == {"triples": [], "observations": []}


def test_extract_claude_returns_empty_on_timeout():
    import subprocess as _sp
    with patch("mempalace.curator.subprocess.run",
               side_effect=_sp.TimeoutExpired("claude", 60)):
        result = extract_claude(_drawer_for_claude())
    assert result == {"triples": [], "observations": []}


def test_extract_claude_returns_empty_when_binary_missing():
    with patch("mempalace.curator.subprocess.run", side_effect=FileNotFoundError()):
        result = extract_claude(_drawer_for_claude())
    assert result == {"triples": [], "observations": []}


# ---------------------------------------------------------------------------
# curate() orchestrator
# ---------------------------------------------------------------------------

from mempalace.curator import curate


def _make_drawer(did, room="decisions", text="We chose ChromaDB because local."):
    return {
        "id": did,
        "metadata": {"room": room, "wing": "mempalace",
                     "filed_at": "2026-04-23T10:00:00"},
        "document": text,
    }


def test_curate_writes_triples_and_observations_for_flagged_drawers(tmp_path):
    drawers = [
        _make_drawer("d1"),
        _make_drawer("d2"),
        _make_drawer("code", text="def f(): pass"),
    ]
    extraction_response = {
        "triples": [{"subject": "MemPalace", "predicate": "uses",
                     "object": "ChromaDB", "valid_from": "2026-04-23"}],
        "observations": ["Switched to ChromaDB."],
    }

    with patch("mempalace.curator._iter_palace_drawers", return_value=iter(drawers)), \
         patch("mempalace.curator.extract_claude",
               return_value=extraction_response) as mock_extract, \
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

    # Two drawers passed the heuristic (d1, d2). The third (pure code) didn't.
    assert mock_extract.call_count == 2
    # Each extraction yielded 1 triple → 2 kg_add calls.
    assert mock_kg_add.call_count == 2
    # Diary write happens once at end-of-run (summary), not per-drawer.
    assert mock_diary.call_count == 1
    assert result["drawers_processed"] == 2
    assert result["triples_added"] == 2
    assert result["observations"] == 2

    state = CurationState.load(tmp_path / "state.json")
    # All three are marked processed: d1/d2 were extracted, "code" was
    # heuristic-rejected (so it doesn't get retried next run).
    assert state.processed_drawer_ids == {"d1", "d2", "code"}
    assert state.last_run_iso is not None


def test_curate_skips_already_processed_drawers(tmp_path):
    state_file = tmp_path / "state.json"
    pre = CurationState.load(state_file)
    pre.mark_processed(["d1"])
    pre.save()

    drawers = [_make_drawer("d1"), _make_drawer("d2")]
    extraction_response = {"triples": [], "observations": []}

    with patch("mempalace.curator._iter_palace_drawers", return_value=iter(drawers)), \
         patch("mempalace.curator.extract_claude",
               return_value=extraction_response) as mock_extract, \
         patch("mempalace.curator.tool_kg_add"), \
         patch("mempalace.curator.tool_diary_write"):
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
         patch("mempalace.curator.extract_claude",
               return_value=extraction_response) as mock_extract, \
         patch("mempalace.curator.tool_kg_add"), \
         patch("mempalace.curator.tool_diary_write"):
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
        "triples": [{"subject": "X", "predicate": "is", "object": "Y",
                     "valid_from": None}],
        "observations": ["something"],
    }

    with patch("mempalace.curator._iter_palace_drawers", return_value=iter(drawers)), \
         patch("mempalace.curator.extract_claude",
               return_value=extraction_response), \
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


def test_curate_passes_source_closet_to_kg_add(tmp_path):
    """Verbatim provenance: every triple must carry source_closet=drawer_id."""
    drawers = [_make_drawer("provenance_drawer")]
    extraction_response = {
        "triples": [{"subject": "A", "predicate": "is", "object": "B",
                     "valid_from": None}],
        "observations": [],
    }
    with patch("mempalace.curator._iter_palace_drawers", return_value=iter(drawers)), \
         patch("mempalace.curator.extract_claude",
               return_value=extraction_response), \
         patch("mempalace.curator.tool_kg_add") as mock_kg_add, \
         patch("mempalace.curator.tool_diary_write"):
        curate(
            palace_path=str(tmp_path),
            since=datetime.fromisoformat("2026-04-01T00:00:00"),
            allowed_rooms={"decisions"},
            excluded_rooms=set(),
            engine="claude",
            max_drawers=10,
            state_file=tmp_path / "state.json",
        )
    assert mock_kg_add.call_count == 1
    kwargs = mock_kg_add.call_args.kwargs
    assert kwargs["source_closet"] == "provenance_drawer"


# ---------------------------------------------------------------------------
# End-to-end integration test against a real ChromaDB collection
# ---------------------------------------------------------------------------


def test_curator_writes_to_real_palace_via_heuristic(tmp_path, monkeypatch):
    """End-to-end: file a drawer in a temp palace, run curator with the
    heuristic engine (no Claude needed), verify stats and side effects.

    Uses ``engine="heuristic"`` to avoid hitting the real ``claude`` CLI in
    CI. The heuristic path produces no triples (kg_add is not called) but
    does extract observations into the curator diary entry, exercising
    the full orchestrator + ChromaDB + state-file path.
    """
    from mempalace.palace import get_collection
    from mempalace.curator import curate

    palace = tmp_path / "palace"
    palace.mkdir()

    coll = get_collection(str(palace))
    coll.add(
        ids=["smoke-1"],
        documents=[
            "We decided to use ChromaDB because it is local-first and zero-API."
        ],
        metadatas=[
            {
                "room": "decisions",
                "wing": "mempalace",
                "filed_at": "2026-04-23T10:00:00",
            }
        ],
    )

    # Mock tool_diary_write so we don't write into the real palace's diary.
    with patch("mempalace.curator.tool_diary_write") as mock_diary:
        stats = curate(
            palace_path=str(palace),
            since=datetime.fromisoformat("2026-04-01T00:00:00"),
            allowed_rooms={"decisions"},
            excluded_rooms=set(),
            engine="heuristic",
            max_drawers=10,
            state_file=tmp_path / "state.json",
        )

    assert stats["drawers_processed"] == 1
    assert stats["observations"] >= 1
    # Heuristic engine: no triples produced.
    assert stats["triples_added"] == 0
    # Diary was written exactly once at end-of-run.
    assert mock_diary.call_count == 1

    # State persisted.
    state = CurationState.load(tmp_path / "state.json")
    assert "smoke-1" in state.processed_drawer_ids
