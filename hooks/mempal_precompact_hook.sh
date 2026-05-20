#!/bin/bash
# MEMPALACE PRE-COMPACT HOOK — Emergency save before compaction
#
# Claude Code "PreCompact" hook. Fires RIGHT BEFORE the conversation
# gets compressed to free up context window space.
#
# This is the safety net. When compaction happens, the AI loses detailed
# context about what was discussed. This hook forces one final save of
# EVERYTHING before that happens.
#
# Unlike the save hook (which triggers every N exchanges), this ALWAYS
# blocks — because compaction is always worth saving before.
#
# === INSTALL ===
# Add to .claude/settings.local.json:
#
#   "hooks": {
#     "PreCompact": [{
#       "hooks": [{
#         "type": "command",
#         "command": "/absolute/path/to/mempal_precompact_hook.sh",
#         "timeout": 30
#       }]
#     }]
#   }
#
# For Codex CLI, add to .codex/hooks.json:
#
#   "PreCompact": [{
#     "type": "command",
#     "command": "/absolute/path/to/mempal_precompact_hook.sh",
#     "timeout": 30
#   }]
#
# === HOW IT WORKS ===
#
# Claude Code sends JSON on stdin with:
#   session_id — unique session identifier
#
# We always return decision: "block" with a reason telling the AI
# to save everything. After the AI saves, compaction proceeds normally.
#
# === MEMPALACE CLI ===
# The hook ALWAYS mines the active conversation transcript synchronously
# before compaction (via `mempalace mine <transcript-dir> --mode convos`).
# MEMPAL_DIR is an *additional*, optional target for project files — it
# does not replace the conversation mine.

STATE_DIR="$HOME/.mempalace/hook_state"
mkdir -p "$STATE_DIR"

# Optional: project directory (code / notes / docs) to also mine before
# compaction. Mined with `--mode projects`. The conversation transcript
# is always mined regardless — this is purely additive.
# Example: MEMPAL_DIR="$HOME/projects/my_app"
MEMPAL_DIR=""

# Resolve the Python interpreter. Same contract as mempal_save_hook.sh:
# MEMPALACE_PYTHON → repo .venv → python3 → python → py -3.
resolve_python() {
    local script_dir candidate
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    for candidate in \
        "${MEMPALACE_PYTHON:-}" \
        "$script_dir/../.venv/Scripts/python.exe" \
        "$script_dir/../.venv/bin/python"
    do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            MEMPAL_PY_CMD=("$candidate")
            return 0
        fi
    done

    if command -v python3 >/dev/null 2>&1; then
        MEMPAL_PY_CMD=("python3")
        return 0
    fi
    if command -v python >/dev/null 2>&1; then
        MEMPAL_PY_CMD=("python")
        return 0
    fi
    if command -v py >/dev/null 2>&1; then
        MEMPAL_PY_CMD=("py" "-3")
        return 0
    fi

    echo '{"decision":"block","reason":"MemPalace hook could not find a Python runtime. Configure MEMPALACE_PYTHON or create the repo .venv first."}'
    exit 0
}

resolve_python

# Read JSON input from stdin
INPUT=$(cat)

# Parse session_id and transcript_path in one call. Sanitize both, then
# read sanitized values from one-per-line stdout into shell variables —
# avoids ``eval`` on generated code (#1231 review). Same contract as
# mempal_save_hook.sh.
mapfile -t _mempal_parsed < <(echo "$INPUT" | "${MEMPAL_PY_CMD[@]}" -c "
import sys, json, re
# Force LF line endings — on Windows, print() writes CRLF by default which
# leaves a trailing \r in each value as mapfile sees it.
sys.stdout.reconfigure(newline='')
data = json.load(sys.stdin)
sid = data.get('session_id', 'unknown')
tp = data.get('transcript_path', '')
safe = lambda s: re.sub(r'[^a-zA-Z0-9_/.\-~]', '', str(s))
print(safe(sid))
print(safe(tp))
" 2>/dev/null | tr -d '\r')
SESSION_ID="${_mempal_parsed[0]:-unknown}"
TRANSCRIPT_PATH="${_mempal_parsed[1]:-}"

# Expand ~ in path
TRANSCRIPT_PATH="${TRANSCRIPT_PATH/#\~/$HOME}"

# Validate that TRANSCRIPT_PATH looks like a transcript file. Mirrors
# mempalace.hooks_cli._validate_transcript_path so the shell hook
# rejects the same shapes the Python hook rejects (#1231 review).
is_valid_transcript_path() {
    local path="$1"
    [ -n "$path" ] || return 1
    case "$path" in
        *.json|*.jsonl) ;;
        *) return 1 ;;
    esac
    case "/$path/" in
        */../*) return 1 ;;
    esac
    return 0
}

echo "[$(date '+%H:%M:%S')] PRE-COMPACT triggered for session $SESSION_ID" >> "$STATE_DIR/hook.log"

# Run ingest synchronously so memories land before compaction. Two
# independent targets — both run if both are set:
#   1. TRANSCRIPT_PATH (from Claude Code) → parent dir, --mode convos
#   2. MEMPAL_DIR → --mode projects
if is_valid_transcript_path "$TRANSCRIPT_PATH" && [ -f "$TRANSCRIPT_PATH" ]; then
    "${MEMPAL_PY_CMD[@]}" -m mempalace mine "$(dirname "$TRANSCRIPT_PATH")" --mode convos \
        >> "$STATE_DIR/hook.log" 2>&1
elif [ -n "$TRANSCRIPT_PATH" ]; then
    echo "[$(date '+%H:%M:%S')] Skipping invalid transcript path: $TRANSCRIPT_PATH" \
        >> "$STATE_DIR/hook.log"
fi
if [ -n "$MEMPAL_DIR" ] && [ -d "$MEMPAL_DIR" ]; then
    "${MEMPAL_PY_CMD[@]}" -m mempalace mine "$MEMPAL_DIR" --mode projects \
        >> "$STATE_DIR/hook.log" 2>&1
fi

# Compaction is about to erase detailed context — always block and demand
# the AI flush diary + KG state before we lose it. There is no opt-out:
# this fires rarely (only when context is full) and the saved memory is
# the only thing that survives compaction.
cat << 'HOOKJSON'
{
  "decision": "block",
  "reason": "MemPalace pre-compact checkpoint. Compaction will erase detailed context — save it now. Do TWO things:\n\n1. Call mempalace_diary_write(agent_name='claude', entry=<AAAK>, topic='precompact') with a thorough AAAK-format diary covering key topics, decisions, code changes, files touched, and surprises. Use real entity names; verbatim quotes welcome.\n\n2. For any new facts (Alan prefers X, project Y uses Z, person A is B), call mempalace_kg_add(subject, predicate, object, valid_from='today'). One call per fact.\n\nAfter saving, compaction proceeds normally."
}
HOOKJSON
