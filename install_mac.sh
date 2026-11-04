#!/usr/bin/env bash
# One-time setup on macOS: Python env, the crawler's own Chrome profile with your X login, and an on-demand
# LaunchAgent. There is NO crawl schedule on the Mac: the agent starts a crawl whenever the Claude
# scheduled task writes trigger/run-request (Mondays 08:00), so Claude's task is the only schedule.
# The agent does wake every 30 s, but only to see whether that file changed (see trigger_check.sh).
set -euo pipefail
cd "$(dirname "$0")"; DIR="$(pwd)"
LABEL="com.xweek.crawl"; PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then PY="$(command -v "$c")"; break; fi
done
[ -n "$PY" ] || { echo "Need Python 3.10+ (brew install python@3.12)"; exit 1; }
echo "Using $PY"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# The crawler runs your installed Google Chrome on its own profile (not your everyday one).
[ -d "/Applications/Google Chrome.app" ] || { echo "Google Chrome is required in /Applications"; exit 1; }
if .venv/bin/python -m xweek.crawl check --channel chrome >/dev/null 2>&1; then
  echo "X login already works for the crawler's Chrome profile."
else
  .venv/bin/python -m xweek.crawl login
  read -r -p "After logging in to X there and quitting that Chrome (Cmd+Q), press Enter here... " _
  if .venv/bin/python -m xweek.crawl check --channel chrome; then
    echo "X login works."
  else
    echo "The crawler still sees X as logged out (or that Chrome window is still open). Run this script again."
    exit 1
  fi
fi

# Remove the scheduled agent from the earlier version, if it was installed.
launchctl bootout "gui/$(id -u)/com.xweek.weekly" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.xweek.weekly.plist"

mkdir -p "$HOME/Library/LaunchAgents" data trigger
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/trigger_check.sh</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>WatchPaths</key><array><string>$DIR/trigger/run-request</string></array>
  <key>StartInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$DIR/data/run.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/run.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
bash trigger_check.sh --mark   # an old request must not start a crawl now
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo
echo "Installed $LABEL. It runs a crawl whenever $DIR/trigger/run-request changes."
echo "Test it now:   date > $DIR/trigger/run-request   then watch: tail -f $DIR/data/run.log"
echo "Remove it:     launchctl bootout gui/$(id -u)/$LABEL && rm $PLIST"
