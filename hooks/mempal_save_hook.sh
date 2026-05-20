#!/bin/bash
# MEMPALACE SAVE HOOK — Auto-save every N exchanges
#
# Claude Code "Stop" hook. After every assistant response:
# 1. Counts human messages in the session transcript
# 2. Every SAVE_INTERVAL messages, BLOCKS the AI from stopping
# 3. Returns a reason telling the AI to save structured diary + palace entries
# 4. AI does the save (topics, decisions, code, quotes → organized into palace)
# 5. Next Stop fires with stop_hook_active=true → lets AI stop normally
#
# The AI does the classification — it knows what wing/hall/closet to use
# because it has context about the conversation. No regex needed.
#
# === INSTALL ===
# Add to .claude/settings.local.json:
#
#   "hooks": {
#     "Stop": [{
#       "matcher": "*",
#       "hooks": [{
#         "type": "command",
#         "command": "/absolute/path/to/mempal_save_hook.sh",
#         "timeout": 30
#       }]
#     }]
#   }
#
# For Codex CLI, add to .codex/hooks.json:
#
#   "Stop": [{
#     "type": "command",
#     "command": "/absolute/path/to/mempal_save_hook.sh",
#     "timeout": 30
#   }]
#
# === HOW IT WORKS ===
#
# Claude Code sends JSON on stdin with these fields:
#   session_id       — unique session identifier
#   stop_hook_active — true if AI is already in a save cycle (prevents infinite loop)
#   transcript_path  — path to the JSONL transcript file
#
# When we block, Claude Code shows our "reason" to the AI as a system message.
# The AI then saves to memory, and when it tries to stop again,
# stop_hook_active=true so we let it through. No infinite loop.
#
# === MEMPALACE CLI ===
# The hook ALWAYS mines the active conversation transcript automatically
# (via `mempalace mine <transcript-dir> --mode convos`). MEMPAL_DIR is an
# *additional*, optional target for project files — it does not replace
# the conversation mine.
#
# === CONFIGURATION ===

SAVE_INTERVAL=30  # Save every N human messages (adjust to taste)
STATE_DIR="$HOME/.mempalace/hook_state"
mkdir -p "$STATE_DIR"

# Optional: project directory (code / notes / docs) to also mine each
# save trigger. Mined with `--mode projects`. The conversation transcript
# is always mined regardless — this is purely additive.
# Example: MEMPAL_DIR="$HOME/projects/my_app"
MEMPAL_DIR=""

# Resolve the Python interpreter the hook should use.
#
# Why this is nontrivial: GUI-launched Claude Code on Windows/macOS (or any
# harness that doesn't inherit the user's shell PATH) may find a `python3`
# on PATH that lacks mempalace — e.g. /usr/bin/python3 while the user
# installed mempalace into a venv. Users in that situation can point the
# hook at the right interpreter by exporting MEMPAL_PYTHON.
#
# Resolution order (first hit wins):
#   1. $MEMPAL_PYTHON          — explicit user override (absolute path)
#   2. $MEMPALACE_PYTHON       — back-compat alias for $MEMPAL_PYTHON
#   3. <repo>/.venv/Scripts/python.exe or <repo>/.venv/bin/python
#   4. $(command -v python3)   — first python3 on PATH
#   5. $(command -v python)    — first python on PATH
#   6. py -3                   — Windows py launcher fallback
resolve_python() {
    local script_dir candidate
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    for candidate in \
        "${MEMPAL_PYTHON:-}" \
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

    echo '{"decision":"block","reason":"MemPalace hook could not find a Python runtime. Configure MEMPAL_PYTHON or create the repo .venv first."}'
    exit 0
}

resolve_python

# Read JSON input from stdin
INPUT=$(cat)

# Parse all fields in a single Python call (3x faster than separate invocations)
# without invoking ``eval`` on generated code: Python prints one sanitized
# value per line, the shell reads them with the POSIX ``read`` builtin and
# does plain variable assignment — same data, smaller blast radius if the
# sanitizer is ever bypassed (#1231 review). ``read`` is used rather than
# ``mapfile``/``readarray``, which are bash 4.0+ only — macOS ships bash 3.2.
{ IFS= read -r SESSION_ID; IFS= read -r STOP_HOOK_ACTIVE; IFS= read -r TRANSCRIPT_PATH; } \
  < <(echo "$INPUT" | "${MEMPAL_PY_CMD[@]}" -c "
import sys, json, re
# Force LF line endings — on Windows, print() writes CRLF by default which
# leaves a trailing \r in each value the shell would otherwise read.
sys.stdout.reconfigure(newline='')
data = json.load(sys.stdin)
sid = data.get('session_id', 'unknown')
sha_raw = data.get('stop_hook_active', False)
tp = data.get('transcript_path', '')
# Shell-safe output — only allow alphanumeric, underscore, hyphen, slash, dot,
# tilde, backslash, colon, and space (the last three for Windows paths like
# C:\Users\Alice Smith\transcript.jsonl). The values are only ever interpolated
# inside double-quoted shell assignments and file-existence checks, so these
# extra characters do not introduce injection risk.
safe = lambda s: re.sub(r'[^a-zA-Z0-9_/.\-~\\\\: ]', '', str(s))
# Coerce stop_hook_active to strict boolean string
sha = 'True' if sha_raw is True or str(sha_raw).lower() in ('true', '1', 'yes') else 'False'
print(safe(sid))
print(sha)
print(safe(tp))
" 2>/dev/null | tr -d '\r')
SESSION_ID="${SESSION_ID:-unknown}"
STOP_HOOK_ACTIVE="${STOP_HOOK_ACTIVE:-False}"
TRANSCRIPT_PATH="${TRANSCRIPT_PATH:-}"

# Expand ~ in path
TRANSCRIPT_PATH="${TRANSCRIPT_PATH/#\~/$HOME}"

# Validate that TRANSCRIPT_PATH looks like a transcript file:
#   - non-empty
#   - .jsonl or .json suffix
#   - no traversal segments (.. components)
# Mirrors mempalace.hooks_cli._validate_transcript_path so the shell hook
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

# If we're already in a save cycle, let the AI stop normally
# This is the infinite-loop prevention: block once → AI saves → tries to stop again → we let it through
if [ "$STOP_HOOK_ACTIVE" = "True" ] || [ "$STOP_HOOK_ACTIVE" = "true" ]; then
    echo "{}"
    exit 0
fi

# Count human messages in the JSONL transcript
# SECURITY: Pass transcript path as sys.argv to avoid shell injection via crafted paths
if [ -f "$TRANSCRIPT_PATH" ]; then
    EXCHANGE_COUNT=$("${MEMPAL_PY_CMD[@]}" - "$TRANSCRIPT_PATH" <<'PYEOF'
import json, sys
count = 0
with open(sys.argv[1]) as f:
    for line in f:
        try:
            entry = json.loads(line)
            msg = entry.get('message', {})
            if isinstance(msg, dict) and msg.get('role') == 'user':
                content = msg.get('content', '')
                if isinstance(content, str) and '<command-message>' in content:
                    continue
                count += 1
        except:
            pass
print(count)
PYEOF
2>/dev/null)
else
    EXCHANGE_COUNT=0
fi

# Track last save point for this session
LAST_SAVE_FILE="$STATE_DIR/${SESSION_ID}_last_save"
LAST_SAVE=0
if [ -f "$LAST_SAVE_FILE" ]; then
    LAST_SAVE_RAW=$(cat "$LAST_SAVE_FILE")
    # SECURITY: Validate as plain integer before arithmetic to prevent command injection
    if [[ "$LAST_SAVE_RAW" =~ ^[0-9]+$ ]]; then
        LAST_SAVE="$LAST_SAVE_RAW"
    fi
fi

SINCE_LAST=$((EXCHANGE_COUNT - LAST_SAVE))

# Log for debugging (check ~/.mempalace/hook_state/hook.log)
echo "[$(date '+%H:%M:%S')] Session $SESSION_ID: $EXCHANGE_COUNT exchanges, $SINCE_LAST since last save" >> "$STATE_DIR/hook.log"

# Time to save?
if [ "$SINCE_LAST" -ge "$SAVE_INTERVAL" ] && [ "$EXCHANGE_COUNT" -gt 0 ]; then
    # Update last save point
    echo "$EXCHANGE_COUNT" > "$LAST_SAVE_FILE"

    echo "[$(date '+%H:%M:%S')] TRIGGERING SAVE at exchange $EXCHANGE_COUNT" >> "$STATE_DIR/hook.log"

    # Auto-mine. Two independent targets — both run if both are set:
    #   1. TRANSCRIPT_PATH (from Claude Code) → parent dir, --mode convos
    #      (Claude Code session JSONL — must use the convo miner)
    #   2. MEMPAL_DIR (user-configured project) → --mode projects
    #      (code, notes, docs)
    # MEMPAL_DIR is *additive*, not an override: a user with MEMPAL_DIR
    # pointed at their project still gets the active conversation mined.
    if is_valid_transcript_path "$TRANSCRIPT_PATH" && [ -f "$TRANSCRIPT_PATH" ]; then
        "${MEMPAL_PY_CMD[@]}" -m mempalace mine "$(dirname "$TRANSCRIPT_PATH")" --mode convos \
            >> "$STATE_DIR/hook.log" 2>&1 &
    elif [ -n "$TRANSCRIPT_PATH" ]; then
        echo "[$(date '+%H:%M:%S')] Skipping invalid transcript path: $TRANSCRIPT_PATH" \
            >> "$STATE_DIR/hook.log"
    fi
    if [ -n "$MEMPAL_DIR" ] && [ -d "$MEMPAL_DIR" ]; then
        "${MEMPAL_PY_CMD[@]}" -m mempalace mine "$MEMPAL_DIR" --mode projects \
            >> "$STATE_DIR/hook.log" 2>&1 &
    fi

    # MEMPAL_VERBOSE toggle:
    #   default (unset/true/1) = block-and-prompt for diary + KG triples.
    #     This is what fills the KG. Without it, the curator (mempalace
    #     curate) is the only path to KG content, and live-conversation
    #     facts never get captured.
    #   false/0 = silent mode (mining only). Use when interruption is too
    #     costly for the work in flight.
    if [ "$MEMPAL_VERBOSE" = "false" ] || [ "$MEMPAL_VERBOSE" = "0" ]; then
        echo '{}'
    else
        cat << 'HOOKJSON'
{
  "decision": "block",
  "reason": "MemPalace curation checkpoint. Before continuing, do TWO things:\n\n1. Call mempalace_diary_write(agent_name='claude', entry=<AAAK>, topic='session') with a brief AAAK-format diary covering key topics, decisions, code/files changed, and any new facts about people or projects since the last save. Use real entity names; verbatim quotes welcome.\n\n2. For any new facts (Alan prefers X, project Y uses Z, person A is B), call mempalace_kg_add(subject, predicate, object, valid_from='today'). One call per fact. Be conservative — only record facts you'd stake the next session on.\n\nAfter these calls, the next Stop will let you exit normally. Continue."
}
HOOKJSON
    fi
else
    # Not time yet — let the AI stop normally
    echo "{}"
fi
