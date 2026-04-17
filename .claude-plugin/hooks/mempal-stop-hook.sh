#!/bin/bash
# MemPalace Stop Hook — thin wrapper calling Python CLI
# All logic lives in mempalace.hooks_cli for cross-harness extensibility

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

    echo '{"decision":"block","reason":"MemPalace hook could not find a Python runtime. Configure MEMPALACE_PYTHON or install the package first."}'
    exit 0
}

resolve_python
INPUT=$(cat)
echo "$INPUT" | "${MEMPAL_PY_CMD[@]}" -m mempalace hook run --hook stop --harness claude-code
