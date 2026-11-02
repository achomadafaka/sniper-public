#!/bin/bash
# Keep the pump.fun snipe tape alive (scripts/research/pump_snipe_tape.py run). Its
# data/pump_snipe_tape.sqlite feeds the Wallet Map funnel (wallet_follow_funnel.py,
# wallet_follow_candidates.py). Research path only, no funded surface.
#
# Was research_daemons_watchdog.sh until 2026-10-02 (operator: the Research section is archived,
# Wallet Map is the research now). Its fresh-cohort and runner_scanner lanes moved to
# archive/legacy/research-lab-2026-10-02/; only this relaunch stays live.
#
# Exact-invocation pgrep (a bare pattern matches the checking shell); pinned pyenv python (bare
# python3 lacks websockets under cron).
REPO="/home/bigpapi/Desktop/Sniper"
PY="/home/bigpapi/.pyenv/versions/3.13.0/bin/python3"

pgrep -f "pump_snipe_tape.py run" >/dev/null || {
  cd "$REPO" && nohup "$PY" scripts/research/pump_snipe_tape.py run \
      >> data/pump_snipe.log 2>&1 &
  # Absolute: the `cd` above runs inside the backgrounded list, so this line runs from cron's
  # $HOME, where data/ does not exist, and no relaunch line was written since 2026-08-22.
  echo "$(date -u +%FT%TZ) relaunched pump_snipe_tape pid $!" >> "$REPO/data/pump_snipe.log"
}
