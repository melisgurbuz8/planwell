#!/bin/bash
# Remove all PlanWell LaunchAgents (watcher + nightly + server).
set -euo pipefail
for LABEL in com.planwell.watcher com.planwell.nightly com.planwell.server; do
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  if [ -f "$PLIST" ]; then
    launchctl unload -w "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed $PLIST"
  else
    echo "Not installed: $LABEL"
  fi
done
