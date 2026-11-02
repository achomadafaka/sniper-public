#!/usr/bin/env bash
# Resume all timers paused during the local-simulation phase.
# Companion to /home/sniper/sniper/PAUSED_TIMERS.md.
set -euo pipefail

USER_TIMERS=(
  sniper-brain-health.timer
  sniper-world-analyst.timer
  sniper-finance-analyst.timer
  sniper-agent-wallet-scout.timer
  sniper-agent-insider-copy.timer
  sniper-agent-insider-analyst.timer
  sniper-agent-strategy-reviewer.timer
  sniper-news-intel.timer
  sniper-knowledge-compile.timer
  sniper-handoff-snapshot.timer
  sniper-sync-state.timer
  sniper-sim-compact.timer
  sniper-predict-cleanup.timer
  sniper-db-prune.timer
)

echo "Re-enabling user timers..."
for t in "${USER_TIMERS[@]}"; do
  if systemctl --user list-unit-files "$t" >/dev/null 2>&1; then
    systemctl --user enable --now "$t" 2>&1 | sed "s/^/  $t: /"
  else
    echo "  $t: (unit file missing — skipping)"
  fi
done

echo ""
echo "Re-enabling system timer (sniper-event-trigger)..."
sudo systemctl enable --now sniper-event-trigger.timer

echo ""
echo "Active sniper timers:"
systemctl --user list-timers --no-pager | grep sniper || true
systemctl list-timers --no-pager | grep sniper || true
