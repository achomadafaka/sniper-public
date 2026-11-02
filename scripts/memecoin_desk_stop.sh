#!/usr/bin/env bash
# DESK STOP, the operator's panic button for the LLM desk. DRY RUN by default: lists the desk workers
# it would stop. With --confirm it sends SIGTERM to each, in order (JUDGE first, so no new intent is
# queued, then assembler, ranker, cluster, crawler, RISK shadow, checker, post-mortem, the feeds), waits
# up to STOP_WAIT_S for them to exit and reports any survivor. Producers U and X are stopped only with
# --include-producers (they are collection, not decisions).
#
# It NEVER touches the funded runner, its watchdog, the funded cluster gate or any open position, and
# never edits .env or the crontab: it prints the .env knob lines and the cron lines the operator must
# also disable, or the watchdogs restart the workers within a minute. Open desk positions keep their
# rails in the funded runner.
#
# Identity comes from /proc: argv[0] a python and argv[1]'s basename exactly a desk worker script, so an
# editor, a grep or a log tail naming the file is never signalled. DESK_STOP_PROC_ROOT points tests at a
# fake process table, and a faked table never signals.
#
#   scripts/memecoin_desk_stop.sh                       # dry run
#   scripts/memecoin_desk_stop.sh --confirm             # stop the decision side
#   scripts/memecoin_desk_stop.sh --confirm --include-producers
set -euo pipefail

CONFIRM=0 PRODUCERS=0
for arg in "$@"; do
  case "$arg" in
    --confirm) CONFIRM=1 ;;
    --include-producers) PRODUCERS=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done
PROC_ROOT="${DESK_STOP_PROC_ROOT:-/proc}"
STOP_WAIT_S="${STOP_WAIT_S:-10}"

# script|.env knob that keeps it off (empty: none, the cron line is the only off switch)|cron entry
ORDER=(
  "memecoin_desk_judge_worker.py|MEMECOIN_DESK_JUDGE_ENABLED|memecoin_desk_judge_watchdog.sh"
  "memecoin_desk_assembler.py|MEMECOIN_DESK_ASSEMBLER_ENABLED|memecoin_desk_assembler_watchdog.sh"
  "memecoin_desk_ranker_worker.py|MEMECOIN_DESK_RANKER_ENABLED|memecoin_desk_ranker_watchdog.sh"
  "memecoin_desk_cluster_worker.py||memecoin_desk_cluster_watchdog.sh"
  "memecoin_desk_crawler.py|MEMECOIN_DESK_CRAWLER_ENABLED|memecoin_desk_crawler_watchdog.sh"
  "memecoin_desk_risk_shadow.py|MEMECOIN_DESK_RISK_SHADOW_ENABLED|memecoin_desk_risk_shadow.py"
  "memecoin_desk_checker.py|MEMECOIN_DESK_CHECKER_ENABLED|memecoin_desk_checker.py"
  "memecoin_desk_postmortem.py|MEMECOIN_DESK_POSTMORTEM_ENABLED|memecoin_desk_postmortem.py"
  "memecoin_desk_dexscreener.py|MEMECOIN_DESK_DEXSCREENER_ENABLED|memecoin_desk_dexscreener_watchdog.sh"
  "memecoin_desk_intake_dexscreener.py|MEMECOIN_DESK_INTAKE_ENABLED|memecoin_desk_intake_dexscreener_watchdog.sh"
  "memecoin_desk_fomo_intake.py||memecoin_desk_fomo_intake_watchdog.sh"
  "memecoin_desk_intake_pumpfun.py|MEMECOIN_DESK_INTAKE_ENABLED|memecoin_desk_intake_pumpfun_watchdog.sh"
  "memecoin_desk_risk_watcher_paper.py||memecoin_desk_risk_watcher_paper_watchdog.sh"   # its knob would stop the LIVE watcher too: cron line only
  "memecoin_desk_paper_engine.py||memecoin_desk_paper_engine_watchdog.sh"
  "memecoin_desk_trending.py|MEMECOIN_DESK_TRENDING_ENABLED|memecoin_desk_trending_watchdog.sh"
  "memecoin_desk_offchain.py|MEMECOIN_DESK_OFFCHAIN_ENABLED|memecoin_desk_offchain_watchdog.sh"
  "memecoin_desk_narrative.py|MEMECOIN_DESK_NARRATIVE_ENABLED|memecoin_desk_narrative_watchdog.sh"
  "memecoin_desk_source_scout.py|MEMECOIN_DESK_SCOUT_ENABLED|memecoin_desk_source_scout.py"
)
if [[ "$PRODUCERS" -eq 1 ]]; then
  ORDER+=("memecoin_desk_universe_worker.py|MEMECOIN_DESK_UNIVERSE_ENABLED|memecoin_desk_universe_watchdog.sh"
          "memecoin_desk_x_producer.py|MEMECOIN_DESK_X_PRODUCER_ENABLED|memecoin_desk_x_producer_watchdog.sh")
fi

pids_of() {   # pids whose argv[0] is a python and argv[1]'s basename is exactly $1
  local want="$1" proc pid argv arg0 arg1
  for proc in "$PROC_ROOT"/[0-9]*; do
    pid="${proc##*/}"
    [[ -r "$proc/cmdline" ]] || continue
    argv=()
    while IFS= read -r -d '' word; do argv+=("$word"); done < "$proc/cmdline" 2>/dev/null || true
    arg0="${argv[0]:-}" arg1="${argv[1]:-}"
    [[ "${arg0##*/}" == python* && "${arg1##*/}" == "$want" ]] && echo "$pid"
  done
  return 0
}

[[ "$CONFIRM" -eq 1 ]] && echo "DESK STOP (--confirm): SIGTERM in order" || echo "DESK STOP dry run: would SIGTERM, in order"
stopped=()
for entry in "${ORDER[@]}"; do
  IFS='|' read -r script knob cron <<<"$entry"
  pids="$(pids_of "$script" | tr '\n' ' ')"
  if [[ -z "${pids// }" ]]; then
    echo "  $script: not running"
    continue
  fi
  echo "  $script: pid ${pids% }"
  if [[ "$CONFIRM" -eq 1 ]]; then
    for pid in $pids; do
      # a faked process table never signals: its pids name real host processes (the runner, say)
      [[ "$PROC_ROOT" == /proc ]] && kill -TERM "$pid" 2>/dev/null || true
      stopped+=("$pid")
    done
  fi
done

if [[ "$CONFIRM" -eq 1 && "$PROC_ROOT" == /proc && "${#stopped[@]}" -gt 0 ]]; then
  for _ in $(seq 1 "$((STOP_WAIT_S * 4))"); do
    alive=0
    for pid in "${stopped[@]}"; do [[ -d "/proc/$pid" ]] && alive=1; done
    [[ "$alive" -eq 0 ]] && break
    sleep 0.25
  done
  for pid in "${stopped[@]}"; do
    [[ -d "/proc/$pid" ]] && echo "  STILL RUNNING pid $pid after ${STOP_WAIT_S}s (send SIGKILL by hand if it must go)"
  done
fi

echo
echo "So the watchdogs do not restart them, the operator also sets in the state root's .env:"
for entry in "${ORDER[@]}"; do
  IFS='|' read -r script knob cron <<<"$entry"
  [[ -n "$knob" ]] && echo "  $knob=0"
done
echo "and comments out (crontab -e) every line naming:"
for entry in "${ORDER[@]}"; do
  IFS='|' read -r script knob cron <<<"$entry"
  echo "  $cron"
done
echo "The funded runner, its watchdog and every open position are untouched; desk positions keep their rails."
echo
# Gamma: stopping the JUDGE stops NEW intents only. The runner drains intents already queued in the
# funded ledger and refuses them only on MEMECOIN_DESK_ENABLED, which it read at its start.
echo "MONEY NOTE: queued desk intents may still fill for up to 30 s (enter_ttl_max_s); check the Desk tab /"
echo "ledger intents after 30 s. An .env edit does not reach the running runner: a HARD stop of desk entries"
echo "needs a runner restart with MEMECOIN_DESK_ENABLED=0. That is the operator's call; this script never does it."
