#!/bin/bash
# Install PlanWell's macOS LaunchAgents:
#   com.planwell.watcher  - `python3 -m planwell.watcher --once` every 10 min (prechecks, preview,
#                           morning digest). Log: data/watcher.log
#   com.planwell.nightly  - `python3 -m planwell.nightly` daily at 03:30 (Oura + calendar history).
#                           Log: data/nightly.log
#   com.planwell.server   - `python3 -m planwell.server --no-browser`, always running (RunAtLoad +
#                           KeepAlive) on http://127.0.0.1:8765/ only. Log: data/server.log
# Undo with scripts/uninstall_launchd.sh.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
  echo "python3 not found on PATH" >&2
  exit 1
fi
mkdir -p "$AGENTS" "$REPO/data"

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
R="$(xml_escape "$REPO")"
P="$(xml_escape "$PYTHON")"

# write_plist LABEL MODULE "EXTRA_ARG_XML" "SCHEDULE_XML" LOGFILE
write_plist() {
  local label="$1" module="$2" extra="$3" schedule="$4" logfile="$5"
  local plist="$AGENTS/$label.plist"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$P</string>
    <string>-m</string>
    <string>$module</string>$extra
  </array>
  <key>WorkingDirectory</key><string>$R</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key><string>$R</string>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin</string>
  </dict>
$schedule
  <key>StandardOutPath</key><string>$R/data/$logfile</string>
  <key>StandardErrorPath</key><string>$R/data/$logfile</string>
</dict>
</plist>
EOF
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load -w "$plist"
  echo "Installed $plist"
}

write_plist "com.planwell.watcher" "planwell.watcher" $'\n    <string>--once</string>' \
  '  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>' "watcher.log"

write_plist "com.planwell.nightly" "planwell.nightly" "" \
  '  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer></dict>' "nightly.log"

write_plist "com.planwell.server" "planwell.server" $'\n    <string>--no-browser</string>' \
  '  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>' "server.log"

echo "Using $PYTHON in $REPO"
echo "PlanWell is always available at http://127.0.0.1:8765/ (this Mac only)."
echo "Logs: $REPO/data/watcher.log, $REPO/data/nightly.log, $REPO/data/server.log"
