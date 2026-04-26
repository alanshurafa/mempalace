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
