#!/usr/bin/env bash
# Run the same checks CI runs, locally, for free: the full test suite with the desktop extra under the offscreen Qt
# platform (a superset of the `sqlite-schema` and `desktop-smoke` CI jobs). Fails if pytest fails; unlike piping
# through `tail`, the exit code is never masked.
#
#   scripts/check.sh            full suite (about 2 minutes)
#   scripts/check.sh --quick    skip the slow real-daemon/desktop tests (about 80 seconds) for small changes
#   scripts/check.sh <pytest args>   anything else is passed to pytest, e.g. tests/test_backup.py -k restore
set -u
cd "$(dirname "$0")/.."
export QT_QPA_PLATFORM=offscreen
args=("$@")
if [ "${1:-}" = "--quick" ]; then
  shift
  args=(-k "not desktop and not real_daemon and not cli_daemon" "$@")
fi
out="$(mktemp)"
uv run --extra test --extra desktop --with-editable . python -m pytest -q "${args[@]}" >"$out" 2>&1
status=$?
grep -E "passed|failed|^FAILED|^ERROR|Fatal" "$out" || true
if [ $status -ne 0 ]; then
  echo "check.sh: FAILED (exit $status). Full output: $out" >&2
else
  rm -f "$out"
  echo "check.sh: OK"
fi
exit $status
