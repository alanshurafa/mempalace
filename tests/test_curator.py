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
