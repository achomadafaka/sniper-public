#!/bin/sh
# STOP THE LIVE 4663, 8453 AND 56 FAST LANES IN ONE COMMAND (Alpha 2026-10-02, for Gamma's pilot hard
# stops, docs/audits/2026-10-02-rh-fast-lane-pilot-stop-rule.md). The stop file is shared: the 4663,
# Base 8453 and BSC 56 fast lanes all watch it (56 since 67bdba4ac, 2026-10-14). The operator runs it:
#   1. writes the stop file: all three live lanes refuse every signal within 1 s, with no restart;
#   2. sets MEMECOIN_RH_FAST_LANE=runner_shadow in .env (the 4663 lane's mode only; 8453 and 56
#      stay stopped by the file alone);
#   3. restarts the runner on its live release (deploy --restart --defer-backup).
# The deploy refuses while a signed or held reservation is open, which is the state H3 fires in.
# The lane then stays stopped by the file, and this prints the restart to run once it resolves.
# Re-enable later: MEMECOIN_RH_FAST_LANE=live in .env, remove the stop file, restart. Removing the
# file re-enables 8453 and 56 within 1 s, even before the restart.
set -eu
usage() {
  echo "usage: $0            stop the live 4663, 8453 and 56 fast lanes (stop file, 4663 .env runner_shadow, restart)"
  echo "       $0 -h|--help  print this and change nothing"
}
# ARGUMENTS FIRST, BEFORE ANY EFFECT (Alpha 2026-10-02: `--help` once ran the whole stop). The only
# action is the bare command; help prints and exits; anything else refuses.
case "${1-}" in
  -h|--help) usage; exit 0 ;;
esac
if [ "$#" -gt 0 ]; then
  echo "REFUSED: unknown argument(s): $*. Nothing was changed." >&2
  usage >&2
  exit 64
fi
ROOT="${SNIPER_STATE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# Run from a release copy (.runtime/memecoin/current/scripts or releases/<sha>/scripts), the state
# root is the checkout that holds .runtime (Gamma MEDIUM on 1ba4102eb).
case "$ROOT" in
  */.runtime/memecoin/*) ROOT="${ROOT%%/.runtime/memecoin/*}" ;;
esac
# A STOP THAT SILENTLY DOES NOTHING IS WORSE THAN NONE: refuse, before writing anything, unless this is
# the runner's state root (its .env and its live release link).
if [ ! -f "$ROOT/.env" ] || [ ! -e "$ROOT/.runtime/memecoin/current" ]; then
  echo "REFUSED: $ROOT is not the runner's state root (no .env or no .runtime/memecoin/current)." >&2
  echo "Run it from the checkout, or set SNIPER_STATE_ROOT to the runner's state root." >&2
  exit 3
fi
STOP="$ROOT/data/memecoin_rh_fast_lane.stop"
ENV_FILE="$ROOT/.env"
DEPLOY="${MEMECOIN_FAST_LANE_STOP_DEPLOY:-$ROOT/scripts/deploy_memecoin_runtime.sh}"

mkdir -p "$ROOT/data"
date -u +%Y-%m-%dT%H:%M:%SZ > "$STOP"
echo "stop file written: $STOP (the live 4663, 8453 and 56 fast lanes fall back within 1 s)"

if grep -q '^MEMECOIN_RH_FAST_LANE=' "$ENV_FILE" 2>/dev/null; then
  sed -i 's/^MEMECOIN_RH_FAST_LANE=.*/MEMECOIN_RH_FAST_LANE=runner_shadow/' "$ENV_FILE"
else
  echo 'MEMECOIN_RH_FAST_LANE=runner_shadow' >> "$ENV_FILE"
fi
echo ".env: MEMECOIN_RH_FAST_LANE=runner_shadow (4663 lane mode; 8453 and 56 stay stopped by the file)"

SHA="$(basename "$(readlink -f "$ROOT/.runtime/memecoin/current")")"
if "$DEPLOY" "$SHA" --restart --defer-backup; then
  echo "runner restarted on $SHA in runner_shadow (the 4663 feed keeps running; harmless there)"
else
  echo "restart REFUSED (above). The 4663, 8453 and 56 lanes stay stopped by the stop file. When the reservation resolves:"
  echo "  $DEPLOY $SHA --restart --defer-backup"
  exit 2
fi
