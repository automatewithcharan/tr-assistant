#!/usr/bin/env bash
# Repository test entrypoint. It scopes isolation to this test process and its
# descendants; unrelated host processes are never inspected, signalled, or
# killed.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/pkscreener-offline.XXXXXX")"
status=0

cleanup() {
  if [[ "$status" -eq 0 ]]; then
    rm -rf -- "$test_root"
  else
    printf 'Offline test diagnostics preserved at: %s\n' "$test_root" >&2
  fi
}
trap cleanup EXIT

export PKSCREENER_TEST_OFFLINE=1
export NO_PROXY='*'
export PKSCREENER_TEST_ROOT="$test_root"
export PKSCREENER_TEST_PID="$$"
export HOME="$test_root/home"
export XDG_CACHE_HOME="$test_root/cache"
export XDG_CONFIG_HOME="$test_root/config"
export PYTHONNOUSERSITE=1
export KITE_EXECUTION_MODE=paper
export KITE_DRY_RUN=1
export KITE_AUTO_TRADE=0
export LOKY_MAX_CPU_COUNT=1
export PYTHONPATH="$repo_root/tests/offline_bootstrap:$repo_root${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$HOME" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME"

"${PYTHON_BIN:-$repo_root/.venv/bin/python}" -m pytest --basetemp "$test_root/pytest" "$@" || status=$?
exit "$status"
