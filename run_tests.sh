#!/usr/bin/env bash
# Run the test suite inside a headless Weston compositor so GTK4 windows never
# appear on your real screen (GTK4 has no offscreen backend, so the tests need a
# real compositor — this mirrors the CI setup in .github/workflows/ci.yml).
#
# Usage:
#   ./run_tests.sh                      # full suite
#   ./run_tests.sh --fast               # fast tier only (skips window tests —
#                                       # seconds, not minutes; see conftest.py)
#   ./run_tests.sh -k TestCallouts      # any pytest args pass straight through
#   ./run_tests.sh -x -q test_pdfeditor.py::TestPageInsertAndConfirm
#
# Workflow: run --fast (or the tests for the area you touched) after every
# change, and the full suite once before committing.
#
# A headless Weston is started once on a private socket and left running for fast
# repeat runs; `./run_tests.sh --stop` tears it down.
set -euo pipefail

RT="${SIDEMARK_TEST_RUNTIME:-/tmp/sidemark-test-wl}"
SOCK="wayland-sidemark-test"
LOG="/tmp/sidemark-weston.log"

if [ "${1:-}" = "--stop" ]; then
  pkill -f "weston.*$SOCK" 2>/dev/null && echo "stopped headless weston" || echo "not running"
  exit 0
fi

TIER=()
if [ "${1:-}" = "--fast" ]; then
  shift
  TIER=(-m "not window")
fi

if ! command -v weston >/dev/null 2>&1; then
  echo "weston not found. Install it once with:  sudo pacman -S weston" >&2
  exit 1
fi

mkdir -p "$RT"
chmod 700 "$RT"

if [ ! -S "$RT/$SOCK" ]; then
  XDG_RUNTIME_DIR="$RT" weston --backend=headless --socket="$SOCK" --idle-time=0 \
    >"$LOG" 2>&1 &
  for _ in $(seq 40); do [ -S "$RT/$SOCK" ] && break; sleep 0.25; done
  [ -S "$RT/$SOCK" ] || { echo "weston failed to start; see $LOG" >&2; exit 1; }
fi

exec env XDG_RUNTIME_DIR="$RT" WAYLAND_DISPLAY="$SOCK" GDK_BACKEND=wayland \
  SIDEMARK_TEST=1 /usr/bin/python3 -m pytest "${TIER[@]}" "${@:-test_pdfeditor.py}" -q
