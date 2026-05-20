#!/bin/bash
# MemPalace Stop Hook — thin wrapper calling Python CLI
# All logic lives in mempalace.hooks_cli for cross-harness extensibility

run_mempalace_hook() {
  # Explicit interpreter override — useful when mempalace is installed in a
  # venv that isn't on PATH (common on Windows where venv\Scripts isn't
  # auto-added to GUI-launched processes). MEMPAL_PYTHON is the documented
  # name; MEMPALACE_PYTHON is accepted as a back-compat alias.
  for _mp in "${MEMPAL_PYTHON:-}" "${MEMPALACE_PYTHON:-}"; do
    if [ -n "$_mp" ] && [ -x "$_mp" ] && "$_mp" -c "import mempalace" >/dev/null 2>&1; then
      "$_mp" -m mempalace hook run "$@"
      return $?
    fi
  done

  if command -v mempalace >/dev/null 2>&1; then
    mempalace hook run "$@"
    return $?
  fi

  if command -v python3 >/dev/null 2>&1 && python3 -c "import mempalace" >/dev/null 2>&1; then
    python3 -m mempalace hook run "$@"
    return $?
  fi

  if command -v python >/dev/null 2>&1 && python -c "import mempalace" >/dev/null 2>&1; then
    python -m mempalace hook run "$@"
    return $?
  fi

  echo "MemPalace hook error: could not find a runnable mempalace command or module" >&2
  return 1
}

run_mempalace_hook --hook stop --harness claude-code
