#!/usr/bin/env bash
# Started by launchd. Runs run_weekly.sh once for each new trigger/run-request.
# launchd's WatchPaths only fires when the file is replaced (mv), not when it is written in place, so the
# agent also starts this every 30 s. Comparing the file with the last one handled covers both, and an
# unchanged file exits at once. `--mark` records the current file as handled without running anything.
set -euo pipefail
cd "$(dirname "$0")"
REQ=trigger/run-request; SEEN=trigger/.last-handled
[ -f "$REQ" ] || exit 0
SIG="$(stat -f %m "$REQ") $(cksum < "$REQ")"
if [ "${1:-}" = "--mark" ]; then printf '%s\n' "$SIG" > "$SEEN"; exit 0; fi
[ "$SIG" = "$(cat "$SEEN" 2>/dev/null || true)" ] && exit 0
printf '%s\n' "$SIG" > "$SEEN"
exec /bin/bash ./run_weekly.sh
