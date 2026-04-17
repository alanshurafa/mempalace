#!/usr/bin/env bash
set -euo pipefail
HOOK_NAME="${1:?Usage: mempal-hook.sh <hook-name>}"

resolve_python() {
  local script_dir plugin_root candidate
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  plugin_root="$(dirname "$script_dir")"

  for candidate in \
    "${MEMPALACE_PYTHON:-}" \
    "$plugin_root/../.venv/Scripts/python.exe" \
    "$plugin_root/../.venv/bin/python"
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

  echo "Could not locate a Python interpreter for MemPalace" >&2
  return 1
}

resolve_python
INPUT_FILE=$(mktemp) || { echo "Failed to create temp file" >&2; exit 1; }
cat > "$INPUT_FILE"
cat "$INPUT_FILE" | "${MEMPAL_PY_CMD[@]}" -m mempalace hook run --hook "$HOOK_NAME" --harness codex
EXIT_CODE=$?
rm -f "$INPUT_FILE" 2>/dev/null
exit $EXIT_CODE
