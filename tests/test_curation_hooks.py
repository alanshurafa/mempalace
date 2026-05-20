"""Snapshot tests for hook block-reason JSON.

Hook output is the contract with Claude Code's runtime. If it changes
shape, that's an explicit decision — not an accident.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SAVE_HOOK = REPO_ROOT / "hooks" / "mempal_save_hook.sh"
PRECOMPACT_HOOK = REPO_ROOT / "hooks" / "mempal_precompact_hook.sh"


def _run_hook(hook_path: Path, payload: dict, env_extra: dict | None = None) -> tuple[str, int]:
    """Run a hook script with the given JSON payload on stdin.

    Returns ``(stdout, returncode)``. Skips the test if bash is unavailable
    (e.g. running on a Windows shell without Git Bash).

    Sets MEMPALACE_PYTHON to the current interpreter so the hook's python
    resolution works without depending on a worktree-local .venv.
    """
    import sys

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not on PATH — hook tests require a POSIX shell")
    env = os.environ.copy()
    # Normalize path separators — Git Bash's `[ -x ]` test rejects
    # backslash-Windows paths but accepts forward-slash equivalents.
    env.setdefault("MEMPALACE_PYTHON", sys.executable.replace("\\", "/"))
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [bash, str(hook_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return result.stdout, result.returncode


def _write_transcript(path: Path, n_user_messages: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i in range(n_user_messages):
            f.write(json.dumps({"message": {"role": "user", "content": f"msg {i}"}}) + "\n")


def test_save_hook_blocks_at_save_interval_with_structured_prompt(tmp_path):
    """At exchange 30 with no prior save, hook should block and emit the
    structured AAAK + KG-triple prompt by default (MEMPAL_VERBOSE not set)."""
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, 30)

    payload = {
        "session_id": "test-session-30",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
    }
    # Explicitly clear MEMPAL_VERBOSE so we test the new default.
    env = {"MEMPAL_VERBOSE": ""}
    stdout, rc = _run_hook(SAVE_HOOK, payload, env_extra=env)
    assert rc == 0, f"hook failed: {stdout}"
    out = json.loads(stdout)
    assert out.get("decision") == "block", f"expected block, got: {out}"
    reason = out.get("reason", "")
    assert "mempalace_diary_write" in reason
    assert "mempalace_kg_add" in reason
    assert "AAAK" in reason


def test_save_hook_silent_when_under_interval(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, 5)
    payload = {
        "session_id": "test-session-5",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
    }
    stdout, rc = _run_hook(SAVE_HOOK, payload)
    assert rc == 0
    assert json.loads(stdout) == {}


def test_save_hook_silent_mode_opt_out(tmp_path):
    """``MEMPAL_VERBOSE=false`` should suppress the block even at the interval."""
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, 30)
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


def test_precompact_hook_always_blocks_with_structured_prompt():
    payload = {"session_id": "pre-1"}
    stdout, rc = _run_hook(PRECOMPACT_HOOK, payload)
    assert rc == 0, f"hook failed: {stdout}"
    out = json.loads(stdout)
    assert out.get("decision") == "block"
    reason = out.get("reason", "")
    assert "mempalace_diary_write" in reason
    assert "mempalace_kg_add" in reason
    assert "AAAK" in reason
