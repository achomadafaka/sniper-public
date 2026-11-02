#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <full-sha> [--restart] [--dry-run] [--provider-env FILE --provider-preflight FILE] [--require-flat] [--activate-rollover] [--activate-band-policy] [--activate-cluster-gate] [--activate-timecap-lift] [--with-backup] [--defer-backup] [--allow-rollback] [--allow-open-book] [--allow-local-commit]" >&2
  exit 2
}

[ $# -ge 1 ] || usage
SHA="$1"; shift
RESTART=0; DRY_RUN=0; ACTIVATE_ROLLOVER=0; ACTIVATE_BAND_POLICY=0
ACTIVATE_CLUSTER_GATE=0; ACTIVATE_TIMECAP_LIFT=0; DEFER_BACKUP=1
ALLOW_ROLLBACK=0; REQUIRE_FLAT=0; ALLOW_OPEN_BOOK=0; ALLOW_LOCAL_COMMIT=0
PROVIDER_ENV_INPUT=""; PROVIDER_PREFLIGHT_INPUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --restart) RESTART=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --activate-rollover) ACTIVATE_ROLLOVER=1 ;;
    --activate-band-policy) ACTIVATE_BAND_POLICY=1 ;;
    --activate-cluster-gate) ACTIVATE_CLUSTER_GATE=1 ;;
    --activate-timecap-lift) ACTIVATE_TIMECAP_LIFT=1 ;;
    --defer-backup) DEFER_BACKUP=1 ;;
    --with-backup) DEFER_BACKUP=0 ;;
    --allow-rollback) ALLOW_ROLLBACK=1 ;;
    --require-flat) REQUIRE_FLAT=1 ;;
    --allow-open-book) ALLOW_OPEN_BOOK=1 ;;
    --allow-local-commit) ALLOW_LOCAL_COMMIT=1 ;;
    --provider-env) [ $# -ge 2 ] || usage; PROVIDER_ENV_INPUT="$2"; shift ;;
    --provider-preflight) [ $# -ge 2 ] || usage; PROVIDER_PREFLIGHT_INPUT="$2"; shift ;;
    *) usage ;;
  esac
  shift
done
[ -z "$PROVIDER_ENV_INPUT$PROVIDER_PREFLIGHT_INPUT" ] \
  || { [ -n "$PROVIDER_ENV_INPUT" ] && [ -n "$PROVIDER_PREFLIGHT_INPUT" ] \
       && [ "$RESTART" -eq 1 ] && [ "$ACTIVATE_ROLLOVER" -eq 0 ] \
       && [ "$ACTIVATE_BAND_POLICY" -eq 0 ] && [ "$ACTIVATE_CLUSTER_GATE" -eq 0 ] \
       && [ "$ACTIVATE_TIMECAP_LIFT" -eq 0 ]; } \
  || { echo "provider staging requires --restart and cannot share a policy activation" >&2; exit 2; }
# Policy activations stop earlier; the flat-only wrapper performs ordinary code cutovers.
[ "$REQUIRE_FLAT" -eq 0 ] \
  || { [ "$ACTIVATE_ROLLOVER" -eq 0 ] && [ "$ACTIVATE_BAND_POLICY" -eq 0 ] \
       && [ "$ACTIVATE_CLUSTER_GATE" -eq 0 ] && [ "$ACTIVATE_TIMECAP_LIFT" -eq 0 ]; } \
  || { echo "require-flat cannot share a policy activation" >&2; exit 2; }
[ "$ACTIVATE_ROLLOVER" -eq 0 ] || [ "$ACTIVATE_BAND_POLICY" -eq 0 ] \
  || { echo "deploy accepts one policy activation mode" >&2; exit 2; }
[ "$ACTIVATE_CLUSTER_GATE" -eq 0 ] \
  || { [ "$ACTIVATE_ROLLOVER" -eq 0 ] && [ "$ACTIVATE_BAND_POLICY" -eq 0 ]; } \
  || { echo "cluster activation cannot share a policy activation" >&2; exit 2; }
# The timecap lift changes what NEW contracts say. Sharing a deploy with another activation would
# make one restart carry two policy changes, so a later cohort split could not attribute either.
[ "$ACTIVATE_TIMECAP_LIFT" -eq 0 ] \
  || { [ "$ACTIVATE_ROLLOVER" -eq 0 ] && [ "$ACTIVATE_BAND_POLICY" -eq 0 ] \
       && [ "$ACTIVATE_CLUSTER_GATE" -eq 0 ]; } \
  || { echo "timecap-lift activation cannot share another activation" >&2; exit 2; }

[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "deploy requires a full lowercase commit SHA" >&2; exit 1; }

SCRIPT="$(readlink -f "$0")"
DEFAULT_SOURCE="$(cd "$(dirname "$SCRIPT")/.." && pwd -P)"
SOURCE_ROOT="${SNIPER_SOURCE_ROOT:-$DEFAULT_SOURCE}"
STATE_ROOT="${SNIPER_STATE_ROOT:-$SOURCE_ROOT}"
RUNTIME_ROOT="${SNIPER_RUNTIME_ROOT:-$SOURCE_ROOT/.runtime/memecoin}"
RELEASES="$RUNTIME_ROOT/releases"
RELEASE="$RELEASES/$SHA"
CURRENT="$RUNTIME_ROOT/current"
# Written just before the runner is stopped, removed on success and by the rollback. Its presence
# means a deploy started stopping things and never finished. See the write site for why a trap is
# not enough.
DEPLOY_INTENT="$STATE_ROOT/data/.memecoin_deploy_intent"
# Durable post-deploy rollback pointer. `prune_old_releases` has always protected OLD_RELEASE -- the
# release being REPLACED by this deploy -- but nothing ever wrote a symlink an operator could roll
# back TO once the deploy finished. Found 2026-08-30 dangling at a SHA pruned days earlier, on the
# FUNDED lane, and nothing would have reported it until a rollback was actually needed. Every other
# runtime's `previous` is frozen at a date between 08-03 and 08-26 for the same reason: it was set
# once by hand and no deploy path maintains it.
PREVIOUS="$RUNTIME_ROOT/previous"
MANIFEST="$STATE_ROOT/data/memecoin_deploy.json"
DB="${DB_PATH:-$STATE_ROOT/sniper_tracker.db}"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
TOKEN_STORE="${FOMO_TOKEN_STORE:-$STATE_ROOT/.fomo_tokens.json}"
PROVIDER_ENV=""; PROVIDER_PREFLIGHT=""; PROVIDER_BACKUP=""; PROVIDER_CHANGED=0
PROVIDER_APPLIED=0; PROVIDER_OLD_MODE=""; PROVIDER_RECOVERY_REQUIRED=0
PROVIDER_CANDIDATE_LAUNCHED=0
PROVIDER_ORIGINAL_IDENTITIES=""; PROVIDER_RH_RECORD=""; PROVIDER_RH_STOPPED=0
PROVIDER_RH_CRON_HANDOFF=0
SHYFT_RUNTIME=0
SHYFT_BASELINE=""; SHYFT_PREEXISTING=""; SHYFT_PAGED=0; ROLLBACK_RUNNER_VERIFIED=0

cleanup_provider_stage() {
  rm -f "$PROVIDER_ENV" "$PROVIDER_PREFLIGHT" 2>/dev/null || true
  [ "$PROVIDER_RECOVERY_REQUIRED" -eq 1 ] || rm -f "$PROVIDER_BACKUP" 2>/dev/null || true
  [ "$PROVIDER_RECOVERY_REQUIRED" -eq 1 ] || rm -f "$PROVIDER_ORIGINAL_IDENTITIES" "$PROVIDER_RH_RECORD" 2>/dev/null || true
}

verify_provider_stage() {
  [ -n "${PROVIDER_ENV:-}" ] || return 0
  "$PY" "$DEFAULT_SOURCE/scripts/memecoin_runtime_verify.py" --code-root "$RELEASE" \
    --state-root "$STATE_ROOT" --provider-env "$PROVIDER_ENV" \
    --provider-preflight "$PROVIDER_PREFLIGHT" --check-provider-stage \
    || fail "provider preflight refused"
}

fail() { echo "deploy refused: $*" >&2; exit 1; }
# One UTC-stamped line per cutover step (2026-09-27). The deploy log had no timestamps, so the
# ~6.5 s with no runner after the switch could not be split between the LI.FI activation and the
# second verify. Output only; no step changes because of it.
# %N, cut to milliseconds by hand: this host's date prints all nine digits for %3N.
# Never fails: under set -e a logging line must not be able to abort (or roll back) a cutover.
step() {
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%S.%N 2>/dev/null)" || ts="unknown-time"
  echo "deploy step ${ts:0:23}Z $*" || true
}
require_flat_book() {
  [ "$REQUIRE_FLAT" -eq 0 ] || "$PY" "$DEFAULT_SOURCE/scripts/memecoin_book_flat.py" "$DB" \
    || fail "funded book is not flat after entry drain"
}
# Validation overrides exist only for the isolated deploy harness. A live deploy always validates.
if [ "${SNIPER_DEPLOY_SKIP_VALIDATION:-0}" = "1" ] \
    || [ -n "${SNIPER_DEPLOY_VERIFY_COMMAND:-}" ]; then
  [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" = "1" ] \
    && [ -f "$STATE_ROOT/data/.sniper_deploy_test_fixture" ] \
    && [ "$(readlink -f "$SOURCE_ROOT")" != "$DEFAULT_SOURCE" ] \
    && [ "$(readlink -f "$STATE_ROOT")" != "$DEFAULT_SOURCE" ] \
    && [ "$(readlink -f "$RUNTIME_ROOT")" != "$DEFAULT_SOURCE/.runtime/memecoin" ] \
    || fail "validation override is only for isolated no-process tests"
fi
# THE WEB FOLLOWS THE RUNNER (operator 2026-10-02: "dashboard is being left behind on each update").
# A restarted runner deploy also deploys the web to the same SHA (below, after success). The script it
# runs may be overridden ONLY in the isolated harness, whose source tree has no web server to import.
WEB_DEPLOY_SCRIPT="$DEFAULT_SOURCE/scripts/deploy_web_runtime.sh"
if [ -n "${SNIPER_WEB_DEPLOY_SCRIPT:-}" ]; then
  [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" = "1" ] \
    && [ -f "$STATE_ROOT/data/.sniper_deploy_test_fixture" ] \
    && [ "$(readlink -f "$STATE_ROOT")" != "$DEFAULT_SOURCE" ] \
    || fail "SNIPER_WEB_DEPLOY_SCRIPT is only for the isolated deploy harness"
  WEB_DEPLOY_SCRIPT="$SNIPER_WEB_DEPLOY_SCRIPT"
fi
# WHERE THE NEW RUNNER IS LAUNCHED FROM (2026-09-27). The watchdog's `setsid nohup` gives the runner
# a new session but NOT a new cgroup, so a deploy run from a Claude session left the funded runner
# inside the desktop app's systemd scope. At 14:50:48Z an out-of-memory kill in that scope made
# systemd stop the whole scope, runner included: 26.4 s with no funded runner until cron relaunched
# it. A deploy whose shell is NOT under system.slice therefore hands the launch to the cron watchdog
# (system.slice/cron.service): it releases the watchdog lock and waits for the cron launch. That
# costs up to one cron minute of stand-down, so such a deploy also defaults to --require-flat
# (--allow-open-book overrides). A deploy already under system.slice keeps the direct launch.
# MEMECOIN_DEPLOY_CALLER_CGROUP replaces /proc/$$/cgroup for the isolated harness ONLY: it is refused
# unless the state root carries the test fixture and is not this repository. In the harness's
# no-process mode the decision is skipped unless the override is set, because no runner launches.
if [ -n "${MEMECOIN_DEPLOY_CALLER_CGROUP:-}" ]; then
  [ -f "$STATE_ROOT/data/.sniper_deploy_test_fixture" ] \
    && [ "$(readlink -f "$STATE_ROOT")" != "$DEFAULT_SOURCE" ] \
    || fail "MEMECOIN_DEPLOY_CALLER_CGROUP is only for the isolated deploy harness"
fi
LAUNCH_VIA_CRON=0
CRON_LAUNCH_FALLBACK=0
if [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ] || [ -n "${MEMECOIN_DEPLOY_CALLER_CGROUP:-}" ]; then
  CALLER_CGROUP="${MEMECOIN_DEPLOY_CALLER_CGROUP:-$(cat "/proc/$$/cgroup" 2>/dev/null || true)}"
  case "$CALLER_CGROUP" in
    "0::/system.slice/"*) ;;
    *) LAUNCH_VIA_CRON=1 ;;
  esac
fi
if [ "$LAUNCH_VIA_CRON" -eq 1 ]; then
  echo "deploy shell is outside system.slice (${CALLER_CGROUP:-unreadable}); the runner launch is handed to cron" >&2
  if [ "$ALLOW_OPEN_BOOK" -eq 0 ] && [ "$ACTIVATE_ROLLOVER" -eq 0 ] \
      && [ "$ACTIVATE_BAND_POLICY" -eq 0 ] && [ "$ACTIVATE_CLUSTER_GATE" -eq 0 ] \
      && [ "$ACTIVATE_TIMECAP_LIFT" -eq 0 ]; then
    REQUIRE_FLAT=1
  fi
fi
mkdir -p "$STATE_ROOT/data"
exec 9>"$STATE_ROOT/data/memecoin_deploy.lock"
flock -n 9 || fail "another memecoin deploy is running"
WATCHDOG_LOCK="${MEMECOIN_WATCHDOG_LOCK:-$STATE_ROOT/data/memecoin_watchdog.lock}"
exec 8>"$WATCHDOG_LOCK"
flock -w 120 8 || fail "memecoin watchdog did not yield to deploy"
pending_reservations() {
  "$PY" - "$DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memecoin_pending_entries'").fetchone()
if not table:
    raise SystemExit("pending reservation table missing")
print(conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'").fetchone()[0])
PY
}
approved_unexecuted_exits() {
  "$PY" "$SOURCE_ROOT/scripts/memecoin_deploy_exit_drain_gate.py" "$DB" --list
}
# DRAIN AN APPROVED EXIT BEFORE STOPPING THE RUNNER.
#
# TITAN 48565, 2026-09-02: the rail confirmed a stop at -41.5% and ALLOWED the sale at 18:51:19.
# A deploy sent SIGTERM at 18:53:02 with that sale unexecuted. The restarted process finally sold
# at -81.3% at 18:55:27. $27.80 of a $58.08 loss happened AFTER we had decided to sell.
#
# Waits for the executor to act on decisions it has already made, rather than blocking outright:
# a hard fail would leave a release unable to proceed while a position is mid-exit, which is the
# moment a fix is most likely to be wanted. Bounded, and it FAILS if the drain does not complete
# so a stuck exit can never be silently stepped over.
wait_for_exit_drain() {
  _stage="$1"
  _deadline=$(( $(date +%s) + ${MEMECOIN_DEPLOY_EXIT_DRAIN_S:-45} ))
  while :; do
    _stuck="$(approved_unexecuted_exits)" || fail "could not verify approved exits ($_stage)"
    [ "$_stuck" = "0" ] && return 0
    [ "$(date +%s)" -ge "$_deadline" ] && \
      fail "$_stuck funded position(s) hold an approved but unexecuted exit ($_stage); refusing to stop the runner mid-exit"
    echo "  waiting for $_stuck approved exit(s) to execute before stopping the runner ($_stage)" >&2
    sleep 3
  done
}
unresolved_submission_reservations() {
  "$PY" - "$DB" <<'PY'
import sqlite3, sys

conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memecoin_pending_entries'").fetchone()
if not table:
    raise SystemExit("pending reservation table missing")
required = {
    "tx_sig", "broadcast_at", "nonce", "signed_payload_b64", "signed_payload_sha256",
    "submission_blockhash", "submission_last_valid_height", "submission_backend",
    "submission_bound_at", "cleanup_tx_sig", "cleanup_nonce", "cleanup_amount_atomic",
    "cleanup_status", "cleanup_signed_at", "cleanup_signed_payload_sha256",
}
columns = {row[1] for row in conn.execute("PRAGMA table_info(memecoin_pending_entries)")}
if not required <= columns:
    print(conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'").fetchone()[0])
    raise SystemExit(0)
where = " OR ".join(f"{name} IS NOT NULL" for name in sorted(required))
print(conn.execute(
    f"SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved' AND ({where})"
).fetchone()[0])
PY
}
release_unsigned_reservations_after_runner_stop() {
  [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" = "1" ] && return 0
  local released_unsigned
  released_unsigned="$(PYTHONPATH="$RELEASE" DB_PATH="$DB" "$PY" - <<'PY'
from sniper.persistence import db_memecoin as m
from sniper.persistence.db_core import get_connection

with get_connection() as conn:
    print(len(m.release_unsigned_reservations_after_runner_stop(conn)))
PY
  )" || return 1
  [ "$released_unsigned" = "0" ] || echo "released $released_unsigned unsigned reservation(s) after runner stop"
}
release_unsigned_close_claims_after_runner_stop() {
  [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" = "1" ] && return 0
  local released_unsigned
  released_unsigned="$(PYTHONPATH="$RELEASE" DB_PATH="$DB" "$PY" - <<'PY'
from sniper.persistence import db_memecoin as m
from sniper.persistence.db_core import get_connection

with get_connection() as conn:
    print(len(m.release_unsigned_close_claims_after_runner_stop(conn)))
PY
  )" || return 1
  [ "$released_unsigned" = "0" ] || echo "released $released_unsigned unsigned close claim(s) after runner stop"
}
git -C "$SOURCE_ROOT" cat-file -e "$SHA^{commit}" 2>/dev/null || fail "commit does not exist"
if [ "$ALLOW_LOCAL_COMMIT" -eq 1 ]; then
  echo "operator-authorized local commit: $SHA (origin/main ancestry not required)"
else
  git -C "$SOURCE_ROOT" show-ref --verify --quiet refs/remotes/origin/main \
    || fail "origin/main is unavailable; fetch before deploy"
  git -C "$SOURCE_ROOT" merge-base --is-ancestor "$SHA" refs/remotes/origin/main \
    || fail "SHA is not reachable from origin/main"
fi
[ -z "$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=no)" ] \
  || fail "source repository has tracked changes"

OLD_SHA=""
OLD_RELEASE=""
if [ -f "$MANIFEST" ]; then
  OLD_SHA="$($PY -c "import json; print(json.load(open('$MANIFEST')).get('sha',''))" 2>/dev/null || true)"
fi
if [ -L "$CURRENT" ]; then
  OLD_RELEASE="$(readlink -f "$CURRENT" 2>/dev/null || true)"
fi
MEMECOIN_CORE_TESTS=(tests/test_web_release_relation.py tests/test_crew_handover_core.py tests/test_memecoin_desk_intake_assembler_runner.py tests/test_memecoin_j7_refusal_diag.py tests/test_archive_ps_keep_backlog.py tests/test_ps_archive_maintenance.py
  tests/test_memecoin_real_runner.py tests/test_memecoin_real_rails_v2.py
  tests/test_memecoin_exits.py tests/test_memecoin_exit_executor.py
  tests/test_memecoin_runner.py tests/test_memecoin_runner_health.py
  tests/test_memecoin_risk_context.py tests/test_memecoin_arming.py
  # TESTS THAT READ THE WHOLE TREE ALSO RUN ON EVERY DEPLOY (2026-10-15, Gamma's full run). They
  # walk tests/, sniper/ or scripts/ (rglob, os.walk, glob, walk_packages), or assert a property
  # of every file without naming one, so a change anywhere can break them and neither the changed-
  # files rule nor an importer grep ever selects them. tests/test_memecoin_deploy_core_treewalk.py
  # keeps this list complete; ~57 s at -n 2 when measured.
  tests/test_armed_wallet_link_collector.py tests/test_brain_graph_isolation.py
  tests/test_cleanup_proven_bad_memecoin_wallets.py tests/test_clock_naive_datetimes.py
  tests/test_daily_backup.py tests/test_desk_daily_report.py tests/test_fixture_v2_schema.py
  tests/test_dex_boost_family_probe.py
  tests/test_ledger_no_single_asset_price_loops.py tests/test_memecoin_alert_mcap_display.py
  tests/test_memecoin_arc_funding.py tests/test_memecoin_csrf_all_mutations.py
  tests/test_memecoin_deploy_core_treewalk.py tests/test_memecoin_evm_context_keys.py
  tests/test_memecoin_evm_standing_allowance.py tests/test_memecoin_exec_extremes_readers.py
  tests/test_memecoin_exit_followup.py tests/test_memecoin_exit_tape.py
  tests/test_memecoin_desk_crawler_live.py tests/test_memecoin_desk_cluster.py tests/test_memecoin_desk_cluster_review_witness.py
  tests/test_desk_risk_evidence_reader.py tests/test_desk_ps_state_witness.py
  tests/test_desk_ps_asof.py tests/test_memecoin_desk_pumpswap_worker.py tests/test_codex_pumpswap_merged_flow.py
  tests/test_memecoin_desk_universe_worker.py tests/test_memecoin_desk_txshape.py
  tests/test_memecoin_desk_funnel.py tests/test_memecoin_desk_x_producer.py
  tests/test_memecoin_inert_settings_knobs.py tests/test_memecoin_manual_intents.py
  tests/test_memecoin_opportunity_boundaries.py tests/test_memecoin_pause_ownership.py
  tests/test_memecoin_research_pacer.py tests/test_memecoin_robinhood_wrap_excess_gas.py
  tests/test_memecoin_runtime_deploy.py tests/test_memecoin_tpu_sidecar_build.py
  tests/test_memecoin_wallet_copy_cap.py tests/test_memecoin_wrap_excess_gas_56.py
  tests/test_no_bare_conftest_imports.py tests/test_no_duplicate_module_identity.py
  tests/test_phase_b_shift_diff.py tests/test_scripts_anchor_their_paths.py
  tests/test_settings_monkeypatch_names_exist.py tests/test_solana_tx_version_readers.py
  tests/test_source_entry_live.py tests/test_source_guard_helper.py tests/test_tests_refuse_real_dns.py
  tests/test_validate_schema_refs.py tests/test_wallet_map_readability.py)
EXPLICIT_MEMECOIN_TESTS=()
# An operator manifest bounds the gate without dropping core or changed tests. Validate against
# the target Git tree before carrying token state, materializing a release, or touching runtime.
if [ "${MEMECOIN_DEPLOY_TEST_FILES_FILE+x}" = x ]; then
  [ -f "$MEMECOIN_DEPLOY_TEST_FILES_FILE" ] && [ -r "$MEMECOIN_DEPLOY_TEST_FILES_FILE" ] \
    || fail "explicit test manifest is missing or unreadable"
  mapfile -t EXPLICIT_REQUESTED < "$MEMECOIN_DEPLOY_TEST_FILES_FILE" \
    || fail "could not read explicit test manifest"
  [ "${#EXPLICIT_REQUESTED[@]}" -gt 0 ] || fail "explicit test manifest is empty"
  CURRENT_SHA="${OLD_RELEASE##*/}"
  [[ "$CURRENT_SHA" =~ ^[0-9a-f]{40}$ ]] \
    && git -C "$SOURCE_ROOT" cat-file -e "$CURRENT_SHA^{commit}" 2>/dev/null \
    || fail "explicit test manifest requires a comparable live SHA"
  CHANGED_TEST_FILES="$(git -C "$SOURCE_ROOT" diff --name-only --diff-filter=ACMRT \
    "$CURRENT_SHA" "$SHA" -- tests)" || fail "could not diff live-to-target test files"
  while IFS= read -r f; do
    case "${f##*/}" in test_*.py) EXPLICIT_REQUESTED+=("$f") ;; esac
  done <<< "$CHANGED_TEST_FILES"
  declare -A EXPLICIT_PICK=()
  for f in "${MEMECOIN_CORE_TESTS[@]}" "${EXPLICIT_REQUESTED[@]}"; do
    [[ "$f" =~ ^tests/([a-zA-Z0-9_.-]+/)*test_[a-zA-Z0-9_.-]+\.py$ ]] \
      || fail "invalid explicit test path: $f"
    case "/$f/" in */../*|*/./*) fail "invalid explicit test path: $f" ;; esac
    test_tree_entry="$(git -C "$SOURCE_ROOT" ls-tree "$SHA" -- "$f")" \
      || fail "could not inspect explicit test path: $f"
    case "$test_tree_entry" in
      '100644 blob '*|'100755 blob '*) ;;
      *) fail "explicit test path is not a regular file in target: $f" ;;
    esac
    EXPLICIT_PICK["$f"]=1
  done
  mapfile -t EXPLICIT_MEMECOIN_TESTS < <(printf '%s\n' "${!EXPLICIT_PICK[@]}" | sort)
fi
# Refuse an unreadable default comparison before materializing or switching a release.
SCOPED_READY=0
SCOPED_CHANGES=""
CURRENT_SHA="${OLD_RELEASE##*/}"
if [ "${MEMECOIN_DEPLOY_TEST_FILES_FILE+x}" != x ] \
    && [ "${MEMECOIN_DEPLOY_FULL_TESTS:-0}" != "1" ] \
    && [[ "$CURRENT_SHA" =~ ^[0-9a-f]{40}$ ]] \
    && git -C "$SOURCE_ROOT" cat-file -e "$CURRENT_SHA^{commit}" 2>/dev/null; then
  SCOPED_CHANGES="$(git -C "$SOURCE_ROOT" diff --name-only "$CURRENT_SHA" "$SHA" \
    -- sniper scripts tests)" || fail "could not diff live-to-target source and test files"
  SCOPED_READY=1
fi

# NEVER DEPLOY BACKWARDS BY ACCIDENT (2026-09-16). A restart ran 1492f83f over a newer live
# 24518c79 for ~2 minutes. The live release must be an ancestor of (or equal to) the target; an
# intended rollback says so with --allow-rollback.
if [ -n "$OLD_SHA" ] && [ "$OLD_SHA" != "$SHA" ] && [ "$ALLOW_ROLLBACK" -eq 0 ] \
    && ! git -C "$SOURCE_ROOT" merge-base --is-ancestor "$OLD_SHA" "$SHA" 2>/dev/null; then
  fail "$SHA is older than the live release $OLD_SHA (or diverged); pass --allow-rollback to roll back"
fi
if [ "$OLD_SHA" = "$SHA" ] && [ "$OLD_RELEASE" = "$RELEASE" ] \
    && [ "$RESTART" -eq 0 ] && [ "$ACTIVATE_ROLLOVER" -eq 0 ] \
    && [ "$ACTIVATE_BAND_POLICY" -eq 0 ] && [ "$ACTIVATE_CLUSTER_GATE" -eq 0 ] \
    && [ "$ACTIVATE_TIMECAP_LIFT" -eq 0 ]; then
  echo "memecoin runtime already current at $SHA; no restart"
  exit 0
fi

[ -f "$DB" ] || fail "runtime database missing: $DB"
[ -f "$STATE_ROOT/.fomo.env" ] || fail "state .fomo.env missing"
if [ -n "$PROVIDER_ENV_INPUT" ]; then
  # Validate the original private file before copying; a copy must not hide unsafe permissions.
  "$PY" "$DEFAULT_SOURCE/scripts/memecoin_runtime_verify.py" --code-root "$RELEASE" \
    --state-root "$STATE_ROOT" --provider-env "$PROVIDER_ENV_INPUT" \
    --provider-preflight "$PROVIDER_PREFLIGHT_INPUT" --check-provider-stage \
    || fail "provider preflight refused"
  PROVIDER_ENV="$(mktemp "$STATE_ROOT/data/.provider-env.XXXXXX")"
  PROVIDER_PREFLIGHT="$(mktemp "$STATE_ROOT/data/.provider-preflight.XXXXXX")"
  trap cleanup_provider_stage EXIT
  install -m 600 "$PROVIDER_ENV_INPUT" "$PROVIDER_ENV" || fail "could not copy private provider stage"
  install -m 600 "$PROVIDER_PREFLIGHT_INPUT" "$PROVIDER_PREFLIGHT" || fail "could not copy provider preflight"
  verify_provider_stage
fi
LEGACY_TOKEN_STORE="$SOURCE_ROOT/.fomo_tokens.json"
if [ ! -s "$TOKEN_STORE" ] && [ -s "$LEGACY_TOKEN_STORE" ] \
    && [ "$(readlink -m "$TOKEN_STORE")" != "$(readlink -m "$LEGACY_TOKEN_STORE")" ]; then
  mkdir -p "$(dirname "$TOKEN_STORE")"
  install -m 600 "$LEGACY_TOKEN_STORE" "$TOKEN_STORE" \
    || fail "could not carry rotated fomo token store into state root"
fi
PENDING="$(pending_reservations)" || fail "could not verify pending reservations"
UNRESOLVED_SUBMISSIONS="$(unresolved_submission_reservations)" \
  || fail "could not verify submitted reservations"
[ "$UNRESOLVED_SUBMISSIONS" = "0" ] \
  || fail "$UNRESOLVED_SUBMISSIONS signed or ambiguous reservation(s) block deploy"

mkdir -p "$RELEASES" "$STATE_ROOT/data" "$STATE_ROOT/logs"
if [ -e "$RELEASE" ]; then
  [ "$(git -C "$RELEASE" rev-parse HEAD 2>/dev/null || true)" = "$SHA" ] \
    || fail "existing release path has wrong SHA"
  [ -z "$(git -C "$RELEASE" status --porcelain --untracked-files=no)" ] \
    || fail "existing release worktree is dirty"
else
  git -C "$SOURCE_ROOT" worktree add --detach "$RELEASE" "$SHA" >/dev/null \
    || fail "could not create detached release worktree"
fi

if [ "${SNIPER_DEPLOY_SKIP_VALIDATION:-0}" != "1" ]; then
  echo "validating exact release $SHA"
  VALIDATION_STATE="$(mktemp -d)"
  mkdir -p "$VALIDATION_STATE/data" "$VALIDATION_STATE/logs"
  printf 'MEMECOIN_REAL_ENABLED=0\n' > "$VALIDATION_STATE/.fomo.env"
  # The parent has validated its selection; nested deploy tests own independent manifests.
  VALIDATION_ENV=(env -u MEMECOIN_DEPLOY_TEST_FILES_FILE
    SNIPER_STATE_ROOT="$VALIDATION_STATE" DB_PATH="$VALIDATION_STATE/test.db"
    MEMECOIN_REAL_ENABLED=0 FOMO_TOKEN_STORE="$VALIDATION_STATE/.fomo_tokens.json")
  (cd "$RELEASE" && "${VALIDATION_ENV[@]}" "$PY" scripts/validate_schema_refs.py) \
    || { rm -rf "$VALIDATION_STATE"; fail "schema validation failed"; }
  # A RELEASE THAT CANNOT WRITE THE TABLE'S CURRENT SHAPE IS REFUSED HERE (N1, 2026-09-23).
  # `block_times` gained a fifth column on 2026-09-23, and a writer that inserts positionally
  # cannot run against it: SQLite refuses four values for five columns at prepare time. The two
  # such statements live in pre-6baf398e copies of the volume writer, one of them on the live
  # newHeads path, so rolling back past that commit stops the on-chain tape on the first block of
  # every chain rather than degrading it. No retained release contains them, but a deploy
  # materializes any permitted commit, so the rollback is reachable
  # and this is where it is refused -- with the incompatibility named, before anything is switched.
  (cd "$RELEASE" && "${VALIDATION_ENV[@]}" "$PY" \
      "$DEFAULT_SOURCE/scripts/check_positional_block_times_writes.py" sniper) \
    || { rm -rf "$VALIDATION_STATE"; fail "release writes block_times positionally"; }
  # Scope validation to what the deploy actually changes. The old rule ran every
  # tests/test_memecoin*.py — 437 files, ~5,500 tests, about 13 minutes — on every deploy
  # regardless of the diff, which made a one-line config change as slow as a rewrite.
  #
  # Selection = MANDATORY core (the funded money path, always run, never diff-dependent)
  #           + changed surviving tests + test files importing a changed module, regardless of prefix.
  # If the live SHA is unknown, fall back to the full memecoin suite:
  # a deploy is never validated on less than we can prove is enough.
  MEMECOIN_TESTS=()          # declared before the scoping branch: set -u trips on ${#…[@]} otherwise
  CURRENT_SHA=""
  if [ -L "$CURRENT" ]; then
    CURRENT_SHA="$(basename "$(readlink -f "$CURRENT")")"
  fi
  if [ "${MEMECOIN_DEPLOY_TEST_FILES_FILE+x}" = x ]; then
    MEMECOIN_TESTS=("${EXPLICIT_MEMECOIN_TESTS[@]}")
    echo "validation explicit: ${#MEMECOIN_TESTS[@]} test files (core and changed tests always included)"
  elif [ "$SCOPED_READY" -eq 1 ]; then
    CHANGED=()
    if [ -n "$SCOPED_CHANGES" ]; then mapfile -t CHANGED <<< "$SCOPED_CHANGES"; fi
    declare -A PICK=()
    for f in "${MEMECOIN_CORE_TESTS[@]}"; do
      [ -f "$RELEASE/$f" ] || { rm -rf "$VALIDATION_STATE"; fail "mandatory core test missing: $f"; }
      PICK["$f"]=1
    done
    for f in "${CHANGED[@]}"; do
      case "$f" in
        tests/*)
          case "${f##*/}" in test_*.py) [ -f "$RELEASE/$f" ] && PICK["$f"]=1 ;; esac
          continue ;;
      esac
      base="$(basename "$f" .py)"
      [ -z "$base" ] && continue
      # Match the MODULE PATH, not the bare basename (2026-08-24). A bare basename is a substring
      # match on a common English word: changing `sniper/web/server.py` matched the token `server`
      # inside tests/test_intel_revival.py, which imports `brain` and has nothing to do with this
      # deploy. It then failed the gate on an unrelated pre-existing module-shadowing collision and
      # refused a clean funded deploy. Search the dotted import path and the slash path instead --
      # `sniper.web.server` / `sniper/web/server` -- which only a test that really touches the
      # module can contain. The mirroring test file (tests/test_<base>.py) is still picked by name.
      dotted="${f%.py}"; dotted="${dotted//\//.}"
      # THE TWO PATTERNS ABOVE MISS HALF THE SUITE (2026-09-02). `from sniper.execution import
      # evm_v4_quote as V` contains NEITHER `sniper.execution.evm_v4_quote` NOR
      # `sniper/execution/evm_v4_quote`, so a test written that way is invisible to this grep.
      # Measured: 536 of 1,081 test files import a real module the two patterns cannot match, over
      # 229 distinct modules -- `sniper.persistence.db_memecoin` is invisible to 227 of its own
      # tests. It is not theoretical: `54e53205` changed evm_v4_quote.py and broke three tests in
      # test_memecoin_v4_pool_key_cache.py, the gate did not select that file, and the tests stayed
      # red for two days. Anchor on the IMPORT STATEMENT -- parent package, `import`, then the
      # module as a whole word. That keeps the 2026-08-24 property: it cannot substring-match a
      # common English word, because a bare `server` has no `from sniper.web import` in front of it.
      parent_esc="${dotted%.*}"; parent_esc="${parent_esc//./\\.}"
      while IFS= read -r t; do [ -n "$t" ] && PICK["$t"]=1; done < <(
        cd "$RELEASE" && grep -rlE --include='test_*.py' \
          -e "$dotted" -e "${f%.py}" -e "from ${parent_esc} import .*\\b${base}\\b" \
          tests 2>/dev/null | sort -u)
      [ -f "$RELEASE/tests/test_$base.py" ] && PICK["tests/test_$base.py"]=1
    done
    mapfile -t MEMECOIN_TESTS < <(printf '%s\n' "${!PICK[@]}" | sort)
    echo "validation scoped: ${#CHANGED[@]} changed files -> ${#MEMECOIN_TESTS[@]} test files (core always included)"
  fi
  if [ "${#MEMECOIN_TESTS[@]}" -eq 0 ]; then
    mapfile -t MEMECOIN_TESTS < <(cd "$RELEASE" && { find tests -maxdepth 1 -name 'test_memecoin*.py' -print;
      printf '%s\n' "${MEMECOIN_CORE_TESTS[@]}"; } | sort -u)
    echo "validation NOT scoped (no comparable live SHA) — running the full memecoin suite"
  fi
  [ "${#MEMECOIN_TESTS[@]}" -gt 0 ] || fail "no memecoin tests found"
  mkdir -p "$HOME/.cache"
  PYTEST_LOCK=()
  if [ "${MEMECOIN_DEPLOY_TEST_FILES_FILE+x}" != x ]; then
    PYTEST_LOCK=(flock "$HOME/.cache/codex-pytest-full.lock")
  fi
  # Validated explicit manifests are focused gates; default selection retains the suite lock.
  (cd "$RELEASE" && "${PYTEST_LOCK[@]}" \
    nice -n 15 ionice -c 3 "${VALIDATION_ENV[@]}" "$PY" -m pytest -n 2 "${MEMECOIN_TESTS[@]}" -q) \
    || { rm -rf "$VALIDATION_STATE"; fail "memecoin suite failed"; }
  (cd "$RELEASE" && "${VALIDATION_ENV[@]}" "$PY" -m compileall -q sniper \
    scripts/memecoin_runner_loop.py scripts/memecoin_runtime_verify.py \
    scripts/memecoin_cluster_gate_worker.py) \
    || { rm -rf "$VALIDATION_STATE"; fail "compile validation failed"; }
  rm -rf "$VALIDATION_STATE"
else
  echo "WARNING: validation skipped by SNIPER_DEPLOY_SKIP_VALIDATION=1"
fi

NOW="$(date -u +%FT%TZ)"
CANDIDATE_MANIFEST="$STATE_ROOT/data/.memecoin_deploy.candidate.$$"
OLD_MANIFEST="$STATE_ROOT/data/.memecoin_deploy.previous.$$"
cleanup_candidate_manifest() {
  rm -f "$CANDIDATE_MANIFEST"
  cleanup_provider_stage
}
trap cleanup_candidate_manifest EXIT

# Release retention: after a fully-verified deploy, keep the current release, the previous release
# (the immediate rollback target), and the newest MEMECOIN_RELEASE_KEEP-2 others; git-worktree-remove
# the rest. The deploy re-creates any worktree from its SHA on demand
# ("git worktree add --detach"); retain unpushed local branches until published. Pruning only
# drops the pre-materialized checkout, never recoverability. NEVER removes current or previous, and
# is fail-safe: it runs only after SUCCESS with the rollback trap already cleared, so a prune hiccup
# cannot undo a good deploy.
prune_old_releases() {
  local keep="${MEMECOIN_RELEASE_KEEP:-5}"
  case "$keep" in ''|*[!0-9]*) keep=5 ;; esac
  [ "$keep" -lt 2 ] && keep=2
  local cur_real prev_real real dir kept=0
  cur_real="$(readlink -f "$RELEASE" 2>/dev/null || true)"
  prev_real="$(readlink -f "$OLD_RELEASE" 2>/dev/null || true)"
  while IFS= read -r dir; do
    real="$(readlink -f "$dir" 2>/dev/null || true)"
    [ -n "$real" ] || continue
    if [ "$real" = "$cur_real" ] || [ "$real" = "$prev_real" ]; then
      continue                              # current + previous ALWAYS kept, outside the budget
    fi
    # A release still named by a cron line or a live process is in use (desk feeds pin their own SHAs).
    # Drain cron output: grep -q can SIGPIPE crontab and hide a match under pipefail.
    if crontab -l 2>/dev/null | grep -F "$real/" >/dev/null \
       || grep -qsF "$real/" /proc/[0-9]*/cmdline 2>/dev/null; then
      continue
    fi
    kept=$((kept + 1))
    [ "$kept" -le "$((keep - 2))" ] && continue   # within the "other recent" budget -> keep
    git -C "$SOURCE_ROOT" worktree remove "$real" 2>/dev/null \
      || git -C "$SOURCE_ROOT" worktree remove --force "$real" 2>/dev/null || true
  done < <(ls -1dt "$RELEASES"/*/ 2>/dev/null)
  git -C "$SOURCE_ROOT" worktree prune 2>/dev/null || true
}
# Predeploy backups are ~11G each; without a cap they climbed to 161G/111 files. Keep only the
# newest MEMECOIN_BACKUP_KEEP (default 1 = just this deploy's recovery point). Scoped to the
# predeploy-* files this script creates — never the live DB or a manually-placed backup.
prune_old_backups() {
  local keep="${MEMECOIN_BACKUP_KEEP:-1}"
  case "$keep" in ''|*[!0-9]*) keep=1 ;; esac
  [ "$keep" -lt 1 ] && keep=1
  local protected="${BACKUP:-}" others_to_keep n=0 f
  others_to_keep=$((keep - 1))
  while IFS= read -r f; do
    [ "$f" = "$protected" ] && continue       # this deploy's recovery point always survives
    n=$((n + 1))
    [ "$n" -le "$others_to_keep" ] && continue
    rm -f -- "$f" "$f-wal" "$f-shm" || true
  done < <(ls -1t "$STATE_ROOT"/data/backups/sniper_tracker-predeploy-*.db 2>/dev/null)
}
# Build + self-test the pinned keyless TPU sidecar artifact BEFORE stopping the old runner. A build
# failure aborts here, with the current release + runner untouched (nothing has been switched yet).
# The artifact lands under .runtime/memecoin/artifacts/<sha>/ and its metadata becomes the manifest's
# sidecar object, which memecoin_runtime_verify then re-hashes. SNIPER_DEPLOY_SKIP_TPU_BUILD=1 opts out
# (RPC-only auto_free); SNIPER_DEPLOY_TPU_BUILD_COMMAND overrides the builder (tests inject a fake).
SIDECAR_JSON="null"
if [ "${SNIPER_DEPLOY_SKIP_TPU_BUILD:-0}" != "1" ]; then
  ARTIFACT_ROOT="$RUNTIME_ROOT/artifacts"
  mkdir -p "$ARTIFACT_ROOT"
  TPU_BUILD_CMD="${SNIPER_DEPLOY_TPU_BUILD_COMMAND:-bash \"$RELEASE/scripts/build_memecoin_tpu_sidecar.sh\" \"\$@\"}"
  bash -c "$TPU_BUILD_CMD" -- "$RELEASE" "$ARTIFACT_ROOT" "$SHA" \
    || fail "TPU sidecar build failed (old runner untouched)"
  TPU_META="$ARTIFACT_ROOT/$SHA/metadata.json"
  [ -f "$TPU_META" ] || fail "TPU sidecar metadata missing after build"
  SIDECAR_JSON="$(cat "$TPU_META")"
fi

$PY - "$CANDIDATE_MANIFEST" "$SHA" "$RELEASE" "$STATE_ROOT" "$NOW" "$OLD_SHA" "$OLD_RELEASE" "$SIDECAR_JSON" "$PROVIDER_PREFLIGHT" <<'PY'
import hashlib, json, os, sys
path, sha, release, state, now, old_sha, old_release, sidecar_json, provider_preflight = sys.argv[1:]
payload = {"sha": sha, "release_path": os.path.realpath(release),
           "state_root": os.path.realpath(state), "deployed_at": now,
           "previous_sha": old_sha or None, "previous_release_path": old_release or None}
lifi_path = os.path.join(release, "sniper", "execution", "lifi_4663_manifest.json")
with open(lifi_path, encoding="utf-8") as handle:
    lifi_manifest = json.load(handle)
if not isinstance(lifi_manifest, dict) or lifi_manifest.get("chain_id") != "4663":
    raise SystemExit("release LI.FI manifest has the wrong chain")
canonical = json.dumps(lifi_manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
payload["lifi_manifest_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
try:
    sidecar = json.loads(sidecar_json)
except Exception:
    sidecar = None
if isinstance(sidecar, dict):
    payload["sidecar"] = sidecar
if provider_preflight:
    with open(provider_preflight, encoding="utf-8") as handle:
        proof = json.load(handle)
    payload["solana_provider"] = {"profile": "shyft", **{k: proof[k] for k in
        ("source_env_sha256", "staged_env_sha256", "fomo_env_sha256", "allocation_record_sha256")}}
with open(path, "w") as handle:
    json.dump(payload, handle, sort_keys=True)
    handle.flush(); os.fsync(handle.fileno())
PY

LIFI_REACTIVATION_CANARY=""
LIFI_REACTIVATED=0
LIFI_ENV_ROLLBACK=""
LIFI_ENV_CHANGED=0

lifi_reactivation_canary() {
  env PYTHONPATH="$RELEASE" SNIPER_STATE_ROOT="$STATE_ROOT" DB_PATH="$DB" "$PY" \
    - "$CANDIDATE_MANIFEST" <<'PY'
import json
import os
import sys

mode = os.getenv("MEMECOIN_LIFI_MODE")
if mode is None:
    for name in (".env", ".fomo.env"):
        try:
            lines = open(os.path.join(os.environ["SNIPER_STATE_ROOT"], name), encoding="utf-8")
        except OSError:
            continue
        with lines:
            for line in lines:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, item = line.split("=", 1)
                    if key.strip() == "MEMECOIN_LIFI_MODE":
                        mode = item.strip().strip("'\"")
                        break
        if mode is not None:
            break
if (mode or "disabled").lower() != "authenticated_funded":
    raise SystemExit
from sniper.util.envfiles import load_env_files

load_env_files(os.environ["SNIPER_STATE_ROOT"])
from sniper.execution.evm_wallet import load_keypair
from sniper.persistence.db_core import get_connection
from sniper.settings import memecoin as cfg

if cfg.memecoin_lifi_mode() != "authenticated_funded":
    raise SystemExit
with open(sys.argv[1], encoding="utf-8") as handle:
    candidate = json.load(handle)
active_hash = os.getenv("MEMECOIN_LIFI_ACTIVE_MANIFEST_HASH", "")
if candidate.get("lifi_manifest_sha256") != active_hash:
    raise SystemExit
_manifest, manifest_hash = cfg.load_lifi_manifest()
if manifest_hash != active_hash:
    raise SystemExit
account = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR)
with get_connection() as conn:
    activation = cfg.validate_lifi_config(conn=conn, custody_wallet=account.address)
print(activation["canary_lifecycle_id"])
PY
}

LIFI_REACTIVATION_CANARY="$(lifi_reactivation_canary)" \
  || fail "could not inspect the compatible LI.FI activation"

verify_candidate_release() {
  local manifest_path="$CANDIDATE_MANIFEST"
  [ "$LIFI_REACTIVATED" -eq 0 ] || manifest_path="$MANIFEST"
  step "verify_start lifi_reactivated=$LIFI_REACTIVATED"
  local provider_args=()
  if [ -n "${PROVIDER_ENV:-}" ] && [ "${PROVIDER_APPLIED:-0}" -eq 0 ]; then
    provider_args=(--provider-env "$PROVIDER_ENV" --provider-preflight "$PROVIDER_PREFLIGHT")
    verify_provider_stage
  fi
  if [ -n "${SNIPER_DEPLOY_VERIFY_COMMAND:-}" ]; then
    bash -c "$SNIPER_DEPLOY_VERIFY_COMMAND" -- "$RELEASE" "$STATE_ROOT" "$manifest_path" \
      || fail "release verifier hook failed"
  elif [ -n "$LIFI_REACTIVATION_CANARY" ] && [ "$LIFI_REACTIVATED" -eq 0 ]; then
    MEMECOIN_LIFI_MODE=disabled "$PY" "$DEFAULT_SOURCE/scripts/memecoin_runtime_verify.py" \
      --code-root "$RELEASE" --state-root "$STATE_ROOT" --manifest "$manifest_path" \
      "${provider_args[@]}" \
      || fail "release verification failed"
  else
    "$PY" "$DEFAULT_SOURCE/scripts/memecoin_runtime_verify.py" --code-root "$RELEASE" \
      --state-root "$STATE_ROOT" --manifest "$manifest_path" \
      "${provider_args[@]}" \
      || fail "release verification failed"
  fi
  step "verify_end"
}

validate_funded_contracts() {
  env PYTHONPATH="$RELEASE" "$PY" "$RELEASE/scripts/validate_memecoin_funded_contracts.py" \
    --code-root "$RELEASE" --state-root "$STATE_ROOT" --db "$DB" \
    || fail "funded contract reconciliation failed"
}

source_mirror_rollback_safe() {
  [ -n "$OLD_RELEASE" ] || return 0
  "$PY" "$DEFAULT_SOURCE/scripts/memecoin_runtime_verify.py" --code-root "$OLD_RELEASE" \
    --state-root "$STATE_ROOT" --source-mirror-only
}

# Verify code/config once before stopping anything. A coordinated activation verifies again after its
# atomic env update; funded DB reconciliation itself must wait for that new cutoff.
verify_candidate_release
if [ "$ACTIVATE_ROLLOVER" -eq 0 ] && [ "$ACTIVATE_BAND_POLICY" -eq 0 ]; then
  validate_funded_contracts
fi

if [ "$DRY_RUN" -eq 1 ]; then
  rm -f "$CANDIDATE_MANIFEST"
  cleanup_provider_stage
  trap - EXIT
  echo "dry-run OK: sha=$SHA pending=$PENDING release=$RELEASE backup=not-created"
  exit 0
fi

# Back up only after every read-only validation passes and only for a deploy that will mutate live
# state. An explicit operator may defer the copy when backup I/O itself blocks an urgent cutover;
# that mode preserves existing backups and is recorded in the deploy result.
BACKUP="deferred"
if [ "$DEFER_BACKUP" -eq 0 ]; then
  mkdir -p "$STATE_ROOT/data/backups" || fail "could not create backup directory"
  BACKUP_TS="$("$PY" -c '
import json, sys, time
from datetime import datetime, timezone
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        offset = float(json.load(fh).get("offset_s", 0.0))
except Exception:
    offset = 0.0
print(datetime.fromtimestamp(time.time() - offset, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
' "$STATE_ROOT/data/clock_offset.json")" \
    || fail "could not generate corrected backup timestamp"
  BACKUP="$STATE_ROOT/data/backups/sniper_tracker-predeploy-${SHA:0:12}-${BACKUP_TS}-$$.db"
  $PY - "$DB" "$BACKUP" <<'PY' || { rm -f "$BACKUP"; fail "consistent database backup failed"; }
import os, sqlite3, sys
source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    result = target.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise SystemExit(f"backup integrity_check failed: {result}")
finally:
    target.close()
    source.close()
os.chmod(sys.argv[2], 0o600)
PY
else
  echo "WARNING: predeploy backup deferred by explicit operator flag"
fi

# Record what completed, after the optional copy and integrity check. No backup hash or restore
# claim: neither is performed here, and the default deferred path must remain free of backup I/O.
$PY - "$CANDIDATE_MANIFEST" "$BACKUP" "$STATE_ROOT/data/clock_offset.json" <<'PY' \
  || fail "could not record backup evidence"
import json, os, sys, time
from datetime import datetime, timezone
manifest, backup, clock_path = sys.argv[1:]
evidence = {"status": "deferred", "path": None, "size_bytes": None,
            "completed_at": None, "integrity_check": None, "restore_verification": "unverified"}
if backup != "deferred":
    if not os.path.isfile(backup):
        raise SystemExit("completed backup is missing")
    try:
        with open(clock_path, encoding="utf-8") as handle:
            offset = float(json.load(handle).get("offset_s", 0.0))
    except Exception:
        offset = 0.0
    evidence.update(status="completed", path=os.path.realpath(backup),
                    size_bytes=os.path.getsize(backup), integrity_check="ok",
                    completed_at=datetime.fromtimestamp(time.time() - offset, timezone.utc).isoformat())
with open(manifest, encoding="utf-8") as handle:
    payload = json.load(handle)
payload["backup"] = evidence
with open(manifest, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True)
    handle.flush(); os.fsync(handle.fileno())
PY

ROLLOVER_ENV="$STATE_ROOT/.env"
ROLLOVER_ENV_BACKUP=""
ROLLOVER_ENV_CHANGED=0
ROLLOVER_OLD_RUNNER_STOPPED=0
CLUSTER_WORKER_STARTED=0
CLUSTER_WORKER_PID=""
DEPLOY_KILL_OWNED=0
DEPLOY_KILL_PATH=""
DEPLOY_KILL_TOKEN=""

restore_rollover_env() {
  if [ "$ROLLOVER_ENV_CHANGED" -eq 1 ] && [ -f "$ROLLOVER_ENV_BACKUP" ]; then
    cp -p "$ROLLOVER_ENV_BACKUP" "$ROLLOVER_ENV"
    ROLLOVER_ENV_CHANGED=0
  fi
}

restore_lifi_env() {
  if [ "$LIFI_ENV_CHANGED" -eq 1 ] && [ -f "$LIFI_ENV_ROLLBACK" ]; then
    cp -p "$LIFI_ENV_ROLLBACK" "$ROLLOVER_ENV"
    LIFI_ENV_CHANGED=0
  fi
}

restore_provider_env() {
  [ "$PROVIDER_CHANGED" -eq 0 ] || [ -f "$PROVIDER_BACKUP" ] || return 1
  if [ "$PROVIDER_CHANGED" -eq 1 ] && [ -f "$PROVIDER_BACKUP" ]; then
    "$PY" - "$PROVIDER_BACKUP" "$STATE_ROOT/.env" "$PROVIDER_OLD_MODE" "$PROVIDER_ENV" <<'PY'
import os, sys, tempfile
backup, destination, mode, staged = sys.argv[1:]
previous = open(backup, "rb").read()
if open(destination, "rb").read() not in (previous, open(staged, "rb").read()):
    raise SystemExit("provider environment changed during cutover; manual recovery required")
fd, temporary = tempfile.mkstemp(prefix=".provider-restore.", dir=os.path.dirname(destination))
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(previous)
        handle.flush(); os.fsync(handle.fileno())
    os.chmod(temporary, int(mode, 8))
    os.replace(temporary, destination)
finally:
    if os.path.exists(temporary): os.unlink(temporary)
PY
    [ "$?" -eq 0 ] || return 1
    PROVIDER_CHANGED=0
  fi
}

stop_provider_processes() {
  # Rollback only: exact code/state identity and PID start time prevent foreign kills/PID reuse.
  "$PY" - "$DEFAULT_SOURCE" "$RELEASE" "$STATE_ROOT" "${PROVIDER_ORIGINAL_IDENTITIES:-}" <<'PY'
import json, os, signal, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.memecoin_runtime_processes import _python_entry_script
release, state = (Path(p).resolve() for p in sys.argv[2:4])
scripts = {release / "scripts/memecoin_runner_loop.py",
           release / "scripts/research/onchain_wallet_watch.py",
           release / "scripts/memecoin_rh_feed_shadow_loop.py"}
original = {tuple(row) for row in json.loads(Path(sys.argv[4]).read_text())} if sys.argv[4] else set()
def identity(process):
    try:
        fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z": return None
        args = [s.decode() for s in (process / "cmdline").read_bytes().split(b"\0") if s]
        if not args or not Path(args[0]).name.startswith("python"): return None
        script = _python_entry_script(args)
        if not script: return None
        path = Path(script)
        path = (path if path.is_absolute() else (process / "cwd").resolve() / path).resolve()
        if path not in scripts: return None
        env = dict(s.decode().split("=", 1) for s in
                   (process / "environ").read_bytes().split(b"\0") if b"=" in s)
        if Path(env.get("SNIPER_STATE_ROOT", "/nonexistent")).resolve() != state: return None
        return (path, fields[19])
    except (OSError, UnicodeError, ValueError, IndexError): return None
owned = {p: identity(p) for p in Path("/proc").iterdir() if p.name.isdigit()}
owned = {p: stamp for p, stamp in owned.items() if stamp is not None
         and (int(p.name), str(stamp[0]), stamp[1]) not in original}
for process, stamp in owned.items():
    fd = None
    try:
        fd = os.pidfd_open(int(process.name))
        if identity(process) == stamp: signal.pidfd_send_signal(fd, signal.SIGTERM)
    except ProcessLookupError: pass
    finally:
        if fd is not None: os.close(fd)
deadline = time.monotonic() + 5
while any(identity(process) == stamp for process, stamp in owned.items()):
    if time.monotonic() >= deadline: raise SystemExit("candidate provider processes did not stop")
    time.sleep(.05)
PY
}

provider_runner_pids() {
  "$PY" - "$DEFAULT_SOURCE" "$STATE_ROOT" "$1" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.memecoin_runtime_verify import _owned_script_rows
rows = _owned_script_rows(sys.argv[2], "scripts/memecoin_runner_loop.py")
expected = str(Path(sys.argv[3]).resolve())
if any(path != expected for _pid, path in rows) or len(rows) > 1:
    raise SystemExit("funded state runner path drift")
for pid, _path in rows: print(pid)
PY
}

capture_provider_originals() {
  PROVIDER_ORIGINAL_IDENTITIES="$(mktemp "$STATE_ROOT/data/.provider-original.XXXXXX")" || return 1
  "$PY" - "$DEFAULT_SOURCE" "$STATE_ROOT" > "$PROVIDER_ORIGINAL_IDENTITIES" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from scripts.memecoin_runtime_verify import _owned_script_rows, _process_start_time
rows = []
for suffix in ('scripts/memecoin_runner_loop.py', 'scripts/research/onchain_wallet_watch.py',
               'scripts/memecoin_rh_feed_shadow_loop.py'):
    for pid, path in _owned_script_rows(sys.argv[2], suffix):
        birth = _process_start_time(pid)
        if birth is None: raise SystemExit('original provider process identity unavailable')
        rows.append([pid, path, birth])
print(json.dumps(rows))
PY
}

prepare_provider_rh_cutover() {
  PROVIDER_RH_RECORD="$(mktemp "$STATE_ROOT/data/.provider-rh.XXXXXX")" || return 1
  "$PY" "$RELEASE/scripts/memecoin_runtime_verify.py" --code-root "$RELEASE" \
    --state-root "$STATE_ROOT" --process-record-script scripts/memecoin_rh_feed_shadow_loop.py \
    --record-expected-root "${OLD_RELEASE:-$SOURCE_ROOT}" > "$PROVIDER_RH_RECORD" || return 1
  PROVIDER_RH_STOPPED=1
  "$PY" "$RELEASE/scripts/memecoin_runtime_verify.py" --code-root "$RELEASE" \
    --state-root "$STATE_ROOT" --stop-process-record "$PROVIDER_RH_RECORD"
}

start_provider_rh() {
  local code="$1"
  if [ "$LAUNCH_VIA_CRON" -eq 1 ]; then
    wait_for_provider_rh_cron_launch "$code"
    return $?
  fi
  SNIPER_STATE_ROOT="$STATE_ROOT" SNIPER_PYTHON="$PY" MEMECOIN_RH_FEED_WATCHDOG_LOCK_HELD=1 \
    bash "$code/scripts/memecoin_rh_feed_shadow_watchdog.sh" 7>&- 8>&- 9>&-
}

retake_provider_rh_watchdog_lock() {
  if ! { exec 7> "$STATE_ROOT/data/memecoin_rh_feed_shadow.watchdog.lock"; flock -w 120 7; }; then
    PROVIDER_RECOVERY_REQUIRED=1
    echo "RH cron handoff could not re-take its watchdog lock; recovery hold retained" >&2
    return 1
  fi
  PROVIDER_RH_CRON_HANDOFF=0
}

wait_for_provider_rh_cron_launch() {
  local code="$1" started deadline identity pid="" birth="" ready=0
  started="$(date +%s)"
  deadline=$((started + ${MEMECOIN_DEPLOY_CRON_WAIT_S:-135}))
  # A direct setsid launch still inherits an app scope. Only cron supplies system.slice.
  # Release RH's lock only; the deploy and funded-runner watchdog locks remain held.
  # A joint handoff (the forward cutover below) already released the lock and logged the step.
  [ "${PROVIDER_RH_CRON_HANDOFF:-0}" -eq 1 ] || step "rh_launch_handed_to_cron release=$code"
  PROVIDER_RH_CRON_HANDOFF=1
  exec 7>&-
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if identity="$("$PY" - "$DEFAULT_SOURCE" "$code" "$STATE_ROOT" "$started" 2>/dev/null <<'PY'
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.memecoin_runtime_verify import _owned_script_rows, _process_start_time, verify_rh_process
code, state, started = Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve(), float(sys.argv[4])
try:
    suffix = 'scripts/memecoin_rh_feed_shadow_loop.py'
    rows = _owned_script_rows(state, suffix)
    if len(rows) != 1 or rows[0][1] != str(code / suffix): raise SystemExit(1)
    pid, path = rows[0]
    birth = _process_start_time(pid)
    if birth is None or verify_rh_process(code, state): raise SystemExit(1)
    beat = json.loads((state / 'data/memecoin_rh_feed_shadow.heartbeat').read_text())
    stamp = beat.get('ts')
    if (beat.get('pid') != pid or type(stamp) not in (int, float) or stamp < started
            or not 0 <= time.time() - stamp <= 15): raise SystemExit(1)
    if not Path('/proc', str(pid), 'cgroup').read_text().strip().startswith('0::/system.slice/'):
        raise SystemExit(1)
    if _process_start_time(pid) != birth or _owned_script_rows(state, suffix) != rows:
        raise SystemExit(1)
    print(pid, birth)
except (OSError, ValueError, TypeError, KeyError):
    raise SystemExit(1)
PY
)"; then
      read -r pid birth <<< "$identity"
      ready=1
      break
    fi
    sleep 1
  done
  retake_provider_rh_watchdog_lock || return 1
  if [ "$ready" -eq 1 ]; then
    step "rh_launched_by_cron pid=$pid start=$birth release=$code"
    return 0
  fi
  echo "RH cron did not launch a healthy pinned system.slice process within ${MEMECOIN_DEPLOY_CRON_WAIT_S:-135}s" >&2
  return 1
}

# PRE-EXISTING PROVIDER TERMS (2026-10-16, Alpha GO). 3a2b415a8 was refused after the switch on
# shyft_runtime_prepare_unready, a RabbitStream outage the OLD runner had too (vendor down since ~18:20Z,
# before the cutover), and its rollback then failed the same gate on the restored release, so the
# entry hold stayed until a manual CLEAR (18:29:49-18:37:54Z). The old runner's --shyft-runtime-only
# terms, read just before the trip, are the baseline: a term already failing then pages once and does
# not refuse; any other term still refuses. No baseline (not Shyft, no old release, a snapshot that
# failed or was unreadable) = strict, exactly as before.
snapshot_shyft_baseline() {
  SHYFT_BASELINE=""
  { [ "${SHYFT_RUNTIME:-0}" -eq 1 ] && [ -n "${OLD_RELEASE:-}" ] && [ -d "$OLD_RELEASE" ]; } || return 0
  local file="$STATE_ROOT/data/.deploy_shyft_baseline.$$.json" out=""
  # Exit 1 means terms are failing, the case this exists for: never an abort under set -e.
  out="$("$PY" "$RELEASE/scripts/memecoin_runtime_verify.py" --code-root "$OLD_RELEASE" \
    --state-root "$STATE_ROOT" --shyft-runtime-only 2>/dev/null)" || true
  if printf '%s' "$out" | "$PY" -c 'import json, sys
terms = json.load(sys.stdin)["errors"]
assert isinstance(terms, list) and all(isinstance(t, str) for t in terms)
json.dump(terms, open(sys.argv[1], "w"))
print(",".join(terms) or "none")' "$file" > "$file.terms" 2>/dev/null; then
    SHYFT_BASELINE="$file"
    step "shyft_baseline terms=$(cat "$file.terms" 2>/dev/null || true)"
  else
    rm -f "$file"
    step "shyft_baseline terms=unknown (strict)"
  fi
  rm -f "$file.terms" 2>/dev/null || true
}

# The --shyft-runtime-only gate for code root $1, net of the baseline in scope $2: "vendor" (post-switch:
# only VENDOR_TERMS the old runner already failed are excused; a process, graph, generation, config or
# TPU term of ours refuses even if pre-existing, Gamma B1) or "all" (rollback: the restored old release
# against its own pre-deploy terms is the status quo). 0: no term, or only excused ones (named in
# SHYFT_PREEXISTING for the page); 1: any other term, or an answer that cannot be read. Without a
# baseline it is the old strict gate, unchanged.
shyft_runtime_gate() {
  local out="" rc=0 terms="" failure=""
  if [ -z "${SHYFT_BASELINE:-}" ]; then
    out="$("$PY" "$RELEASE/scripts/memecoin_runtime_verify.py" \
      --code-root "$1" --state-root "$STATE_ROOT" --shyft-runtime-only 2>/dev/null)" || rc=$?
  else
    out="$("$PY" "$RELEASE/scripts/memecoin_runtime_verify.py" --code-root "$1" --state-root "$STATE_ROOT" \
      --shyft-runtime-only --baseline-errors "$SHYFT_BASELINE" --baseline-scope "${2:-vendor}" 2>/dev/null)" \
      || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    terms="$(printf '%s' "$out" | "$PY" -c 'import json, re, sys
terms = json.load(sys.stdin)["errors"]
assert isinstance(terms, list) and terms and all(isinstance(t, str) and re.fullmatch(r"[a-z0-9_]+", t) for t in terms)
print(",".join(terms))' 2>/dev/null)" || terms=unreadable
    failure="code_root=$1 scope=${2:-vendor} terms=$terms"
    if [ "${SHYFT_LAST_FAILURE:-}" != "$failure" ]; then
      step "shyft_runtime_gate_waiting $failure"
      SHYFT_LAST_FAILURE="$failure"
    fi
    return 1
  fi
  SHYFT_LAST_FAILURE=""
  [ -n "${SHYFT_BASELINE:-}" ] || return 0
  SHYFT_PREEXISTING="$(printf '%s' "$out" | "$PY" -c \
    'import json, sys; print(",".join(json.load(sys.stdin)["preexisting"]))' 2>/dev/null)" || return 1
  return 0
}

# One Telegram page per deploy when a gate passed only on preexisting terms. Best-effort: a failed send
# is a line in the deploy output and never changes the verdict. SENT, NOT QUEUED (Alpha 2026-10-16: the
# 18:58Z page never reached Telegram): memecoin_alerts.send_alert only enqueues on a dispatcher daemon
# thread that a one-shot exits before, and this one-shot never loaded .env/.fomo.env, so the send was
# disabled. As the watchdog's alert(): load the state root's env files, send synchronously, exit 3 when
# it was not delivered so the failure line shows. send_telegram_audited also writes the page's
# memecoin_telegram_delivery_audit row (kind operational:deploy), which only the dispatcher wrote before. Called after the entry gate's CLEAR, so a
# slow send never lengthens the entries-down window.
page_shyft_preexisting() {
  { [ -n "${SHYFT_PREEXISTING:-}" ] && [ "${SHYFT_PAGED:-0}" -eq 0 ]; } || return 0
  SHYFT_PAGED=1
  step "shyft_preexisting terms=$SHYFT_PREEXISTING context=$1"
  # Bounded: send_telegram sleeps the per-chat gap and any 429 retry_after and waits its timeout per chat.
  # Exit 4: the operator's hard off switch (MEMECOIN_TG_DISPATCH_ENABLED=0, read after the state env):
  # nothing is sent and no row written, and the line says so instead of "failed" (Alpha 2026-10-16).
  local rc=0
  timeout 20 env PYTHONSAFEPATH=1 PYTHONPATH="$DEFAULT_SOURCE" SNIPER_STATE_ROOT="$STATE_ROOT" "$PY" -c \
    "import sys; from sniper.util.envfiles import load_env_files; load_env_files(sys.argv[1]); from sniper.services import memecoin_telegram as tg; sys.exit(4 if tg.dispatch_off() else 0 if tg.send_telegram_audited(sys.argv[2], kind='operational:deploy')[0] else 3)" \
    "$STATE_ROOT" "🔴 memecoin deploy | deploy ${SHA:-?} $1 with provider term(s) the old runner already failed before the cutover: $SHYFT_PREEXISTING. Not refused for them; check the vendor." \
    >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 4 ]; then
    echo "shyft preexisting page skipped: dispatch off (verdict unchanged)" >&2
  elif [ "$rc" -ne 0 ]; then
    echo "shyft preexisting page failed (verdict unchanged)" >&2
  fi
  return 0
}

provider_runtime_ready() {
  [ "${SHYFT_RUNTIME:-0}" -eq 0 ] && return 0
  local handover="$STATE_ROOT/data/onchain_watch.handover.json" candidate pinned birth
  if [ -f "$handover" ]; then
    # Deploy holds fd 8 again; advance the exact recorded handover without another watchdog pass.
    "$PY" "$RELEASE/scripts/memecoin_runtime_verify.py" --code-root "$RELEASE" \
      --state-root "$STATE_ROOT" --retire-watcher-record "$handover" >/dev/null 2>&1 || return 1
    candidate="$("$PY" -c \
      "import json,sys; r=json.load(open(sys.argv[1])); print(r['candidate'][0], r['candidate'][2])" \
      "$handover" 2>/dev/null)" || return 1
    read -r pinned birth <<< "$candidate"
    "$PY" "$RELEASE/scripts/memecoin_runtime_verify.py" --code-root "$RELEASE" \
      --state-root "$STATE_ROOT" --watcher-only --watcher-candidate-pid "$pinned" \
      --watcher-candidate-birth "$birth" >/dev/null 2>&1 || return 1
    rm -f "$handover" || return 1
  fi
  shyft_runtime_gate "$RELEASE" vendor
}

verify_provider_rollback() {
  local deadline=$(( $(date +%s) + 60 )) old_profile
  old_profile="$(grep -E '^MEMECOIN_SOLANA_INFRA_PROVIDER=' "$STATE_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '"'"'"' ')"
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if "$PY" "$DEFAULT_SOURCE/scripts/memecoin_runtime_verify.py" --code-root "$OLD_RELEASE" \
        --state-root "$STATE_ROOT" --rollback-only --startup-since "$PROVIDER_ROLLBACK_STARTED"; then
      ROLLBACK_RUNNER_VERIFIED=1           # the restored runner itself is up (rollback() reads this)
      if [ "${PROVIDER_RH_STOPPED:-0}" -eq 0 ] || "$PY" "$RELEASE/scripts/memecoin_runtime_verify.py" \
          --code-root "$OLD_RELEASE" --state-root "$STATE_ROOT" --rh-process-only; then
        if [ "$old_profile" != shyft ] || shyft_runtime_gate "$OLD_RELEASE" all; then
          return 0
        fi
      fi
    fi
    sleep .25
  done
  [ -z "${SHYFT_LAST_FAILURE:-}" ] || step "shyft_runtime_gate_failed $SHYFT_LAST_FAILURE"
  return 1
}

# Hand the runner launch to the cron watchdog and wait for it (see LAUNCH_VIA_CRON). $1 is the
# release whose runner must come up. Returns 0 once a runner from $1 is alive, healthy and under
# system.slice; 1 after MEMECOIN_DEPLOY_CRON_WAIT_S (default 135 s: TWO cron runs, since cron
# fires at :01 and a handoff before ~:53 saw only one run in 100 s; plus ~5-10 s of watchdog launch
# and verify. On 2026-09-27 the one run refused on a test's fake runner in /tmp and the deploy fell
# back to an app-scope launch). The watchdog lock is released for the wait -- the cron
# watchdog takes it with `flock -n` and skips while the deploy holds it -- and re-taken before
# returning, so the rest of the deploy holds it as before. The deploy lock (fd 9) is never released.
wait_for_cron_launch() {
  local release="$1" started deadline pid cmdline cg health pidfile
  pidfile="${MEMECOIN_PIDFILE:-$STATE_ROOT/data/memecoin_runner.pid}"
  started="$(date +%s)"
  deadline=$((started + ${MEMECOIN_DEPLOY_CRON_WAIT_S:-135}))
  step "runner_launch_handed_to_cron release=$release"
  exec 8>&-
  while [ "$(date +%s)" -lt "$deadline" ]; do
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
      cg="$(cat "/proc/$pid/cgroup" 2>/dev/null || true)"
      health="$(PYTHONPATH="$release" SNIPER_STATE_ROOT="$STATE_ROOT" "$PY" -c \
        "import sys,time; from sniper.services.memecoin_runner import read_heartbeat,runner_launch_health; print(runner_launch_health(read_heartbeat(),float(sys.argv[1]),time.time())['reason'])" \
        "$started" 2>/dev/null || true)"
      if [[ "$cmdline" == *"$release/scripts/memecoin_runner_loop.py"* ]] \
          && [[ "$cg" == "0::/system.slice/"* ]] && [ "$health" = "ok" ]; then
        exec 8>"$WATCHDOG_LOCK"
        flock -w 120 8 || echo "WARNING: could not re-take the watchdog lock after the cron launch" >&2
        step "runner_launched_by_cron pid=$pid"
        return 0
      fi
    fi
    sleep 1
  done
  exec 8>"$WATCHDOG_LOCK"
  flock -w 120 8 || echo "WARNING: could not re-take the watchdog lock after the cron wait" >&2
  return 1
}

# THE TRIP LANDS JUST BEFORE THE CRON TICK (2026-09-28, Alpha). On the cron-handoff path the deploy
# trips the entry gate, hands the launch to cron about 4 s later, and cron fires only at the next
# minute: the 06:36/06:42/06:47Z deploys each stood entries down about 46-52 s (trip 06:47:19.6,
# launched 06:48:11.3). Tripping when the second of the minute is in [48, 52] puts the handoff just
# before the tick. Prints the seconds to wait from second-of-minute $1: 0 inside the window, up to
# 48 before it, and up to the next minute's 48 after it (a trip at :58 would hand off after the tick
# and wait a whole minute). The wait comes BEFORE the trip, so entries keep flowing through it.
# CRON_TRIP_WAIT_BEGIN
cron_trip_wait_s() {
  local s=$((10#${1:-0}))
  if [ "$s" -ge 48 ] && [ "$s" -le 52 ]; then
    echo 0
  elif [ "$s" -lt 48 ]; then
    echo $((48 - s))
  else
    echo $((108 - s))
  fi
}
# CRON_TRIP_WAIT_END

cron_launch_failed_loudly() {
  CRON_LAUNCH_FALLBACK=1
  echo "!!! CRON DID NOT LAUNCH THE RUNNER within ${MEMECOIN_DEPLOY_CRON_WAIT_S:-135}s." \
       "Launching it DIRECTLY, inside this shell's scope: a crash of this session will stop it." >&2
}

restart_old_rollover_runner() {
  if [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ] && [ -n "$OLD_RELEASE" ] \
      && [ -d "$OLD_RELEASE" ]; then
    if [ "$LAUNCH_VIA_CRON" -eq 1 ] && wait_for_cron_launch "$OLD_RELEASE"; then
      return 0
    fi
    [ "$LAUNCH_VIA_CRON" -eq 0 ] || cron_launch_failed_loudly
    if ! \
      SNIPER_STATE_ROOT="$STATE_ROOT" MEMECOIN_WATCHDOG_LOCK_HELD=1 bash "$OLD_RELEASE/scripts/memecoin_watchdog.sh" 7>&- 8>&- 9>&-; then
      { [ -z "$PROVIDER_ENV" ] && [ "${SHYFT_RUNTIME:-0}" -eq 0 ]; } || return 1
    fi
  fi
}

clear_deploy_entry_gate() {
  if [ "$DEPLOY_KILL_OWNED" -eq 1 ] && [ -n "$DEPLOY_KILL_PATH" ]; then
    if ! env PYTHONSAFEPATH=1 PYTHONPATH="$DEFAULT_SOURCE" SNIPER_STATE_ROOT="$STATE_ROOT" "$PY" -c \
      "import sys; from sniper.services.memecoin_arming import clear_kill_switch; cleared = clear_kill_switch(source='runtime-deploy', expected_token=sys.argv[1]); print('deploy hold released' if cleared else 'deploy hold changed; current pause preserved')" "$DEPLOY_KILL_TOKEN" \
    ; then
      return 1
    fi
    DEPLOY_KILL_OWNED=0
  fi
  return 0
}

pre_switch_rollover_rollback() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    if [ "$ROLLOVER_OLD_RUNNER_STOPPED" -eq 1 ] && ! source_mirror_rollback_safe; then
      echo "source mirror rollback refused; configuration, entry hold and recovery files preserved" >&2
      trap - EXIT
      exit "$rc"
    fi
    restore_rollover_env
    if [ "$ROLLOVER_OLD_RUNNER_STOPPED" -eq 1 ]; then
      restart_old_rollover_runner
    fi
    if [ "$CLUSTER_WORKER_STARTED" -eq 1 ] && [[ "$CLUSTER_WORKER_PID" =~ ^[0-9]+$ ]]; then
      if [ -r "/proc/$CLUSTER_WORKER_PID/cmdline" ] \
          && tr '\0' ' ' < "/proc/$CLUSTER_WORKER_PID/cmdline" \
             | grep -Fq "$RELEASE/scripts/memecoin_cluster_gate_worker.py"; then
        kill "$CLUSTER_WORKER_PID" 2>/dev/null || true
      fi
    fi
  fi
  rm -f "$ROLLOVER_ENV_BACKUP" 2>/dev/null || true
  trap - EXIT
  exit "$rc"
}

if [ "$ACTIVATE_CLUSTER_GATE" -eq 1 ]; then
  [ -f "$ROLLOVER_ENV" ] || fail "cluster activation requires $ROLLOVER_ENV"
  trap pre_switch_rollover_rollback EXIT
  if [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ]; then
    wait_for_exit_drain "line 574"
    RUNNER_PIDS="$($PY "$SOURCE_ROOT/scripts/memecoin_runtime_processes.py" \
      --expected "$OLD_RELEASE/scripts/memecoin_runner_loop.py" | awk -F '\t' '{print $1}')"
    for OLD_PID in $RUNNER_PIDS; do
      kill "$OLD_PID" 2>/dev/null || true
      for _ in $(seq 1 20); do kill -0 "$OLD_PID" 2>/dev/null || break; sleep 0.25; done
      kill -0 "$OLD_PID" 2>/dev/null \
        && fail "old runner pid $OLD_PID did not stop for cluster activation"
    done
    ROLLOVER_OLD_RUNNER_STOPPED=1
  fi
  release_unsigned_reservations_after_runner_stop \
    || fail "could not release unsigned reservations after runner stop"
  release_unsigned_close_claims_after_runner_stop \
    || fail "could not release unsigned close claims after runner stop"
  PENDING="$(pending_reservations)" \
    || fail "could not recheck pending reservations before cluster activation"
  [ "$PENDING" = "0" ] || fail "$PENDING pending reservation(s) block cluster activation"
  if [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ]; then
    CLUSTER_PREVIOUS_PID="$(cat "$STATE_ROOT/data/memecoin_cluster_gate.pid" 2>/dev/null || true)"
    SNIPER_STATE_ROOT="$STATE_ROOT" bash "$RELEASE/scripts/memecoin_cluster_gate_watchdog.sh" \
      || fail "cluster worker did not start"
    CLUSTER_WORKER_PID="$(cat "$STATE_ROOT/data/memecoin_cluster_gate.pid" 2>/dev/null || true)"
    if [ "$CLUSTER_WORKER_PID" != "$CLUSTER_PREVIOUS_PID" ]; then
      CLUSTER_WORKER_STARTED=1
    fi
    CLUSTER_HEARTBEAT="$STATE_ROOT/data/memecoin_cluster_gate.heartbeat"
    CLUSTER_SOCKET="$STATE_ROOT/data/memecoin_cluster_gate.sock"
    CLUSTER_HEALTHY=0
    for _ in $(seq 1 20); do
      if [ -S "$CLUSTER_SOCKET" ] && "$PY" - "$CLUSTER_HEARTBEAT" <<'PY'
import os, sys, time
path = sys.argv[1]
raise SystemExit(0 if os.path.isfile(path) and time.time() - os.path.getmtime(path) <= 3 else 1)
PY
      then
        CLUSTER_HEALTHY=1
        break
      fi
      sleep 1
    done
    [ "$CLUSTER_HEALTHY" -eq 1 ] \
      || fail "cluster worker socket or heartbeat is not healthy"
  fi
  ROLLOVER_ENV_BACKUP="$STATE_ROOT/data/.env.cluster.previous.$$"
  cp -p "$ROLLOVER_ENV" "$ROLLOVER_ENV_BACKUP" \
    || fail "could not back up cluster activation environment"
  "$PY" - "$ROLLOVER_ENV" <<'PY' || fail "could not activate cluster environment"
import os
import stat
import sys
import tempfile

path = sys.argv[1]
key = "MEMECOIN_REAL_CLUSTER_GATE_ENABLED"
with open(path) as handle:
    lines = handle.readlines()
out = []
written = False
for line in lines:
    stripped = line.strip()
    line_key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
    if line_key == key and not stripped.startswith("#"):
        if not written:
            out.append("MEMECOIN_REAL_CLUSTER_GATE_ENABLED=1\n")
            written = True
    else:
        out.append(line)
if not written:
    out.append("MEMECOIN_REAL_CLUSTER_GATE_ENABLED=1\n")
mode = stat.S_IMODE(os.stat(path).st_mode)
fd, temporary = tempfile.mkstemp(prefix=".env.cluster.", dir=os.path.dirname(path) or ".")
try:
    with os.fdopen(fd, "w") as handle:
        handle.writelines(out)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
  ROLLOVER_ENV_CHANGED=1
  verify_candidate_release
  validate_funded_contracts
fi

# TIMECAP LIFT ACTIVATION (2026-08-26). Same atomic pattern as the other activations: stop the
# runner FIRST, recheck pending reservations AFTER the stop, write the env key, revalidate the
# release, and let the existing pre-switch trap roll env/runtime/process state back on failure.
#
# NO NEW CUTOFF TIMESTAMP IS WRITTEN, and that is deliberate. The cohort boundary is the FROZEN
# CONTRACT itself: a position opened before this activation carries its old policy_id and its 780s
# max_hold in immutable hashed bytes and keeps them whatever the env later says. A parallel cutoff
# would be a second copy of a boundary the contracts already define exactly - redundant state that
# can drift from the thing it describes. MEMECOIN_VOLDEATH_ARM_MIN_OPENED_AT is NOT touched, so the
# pre-14:05Z cohort stays excluded from the silence rail exactly as it is today.
if [ "$ACTIVATE_TIMECAP_LIFT" -eq 1 ]; then
  [ -f "$ROLLOVER_ENV" ] || fail "timecap-lift activation requires $ROLLOVER_ENV"
  trap pre_switch_rollover_rollback EXIT
  if [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ]; then
    wait_for_exit_drain "line 675"
    RUNNER_PIDS="$($PY "$SOURCE_ROOT/scripts/memecoin_runtime_processes.py" \
      --expected "$OLD_RELEASE/scripts/memecoin_runner_loop.py" | awk -F '\t' '{print $1}')"
    for OLD_PID in $RUNNER_PIDS; do
      kill "$OLD_PID" 2>/dev/null || true
      for _ in $(seq 1 20); do kill -0 "$OLD_PID" 2>/dev/null || break; sleep 0.25; done
      kill -0 "$OLD_PID" 2>/dev/null \
        && fail "old runner pid $OLD_PID did not stop for timecap-lift activation"
    done
    ROLLOVER_OLD_RUNNER_STOPPED=1
  fi
  release_unsigned_reservations_after_runner_stop \
    || fail "could not release unsigned reservations after runner stop"
  release_unsigned_close_claims_after_runner_stop \
    || fail "could not release unsigned close claims after runner stop"
  PENDING="$(pending_reservations)" \
    || fail "could not recheck pending reservations before timecap-lift activation"
  [ "$PENDING" = "0" ] || fail "$PENDING pending reservation(s) block timecap-lift activation"
  ROLLOVER_ENV_BACKUP="$STATE_ROOT/data/.env.timecap.previous.$$"
  cp -p "$ROLLOVER_ENV" "$ROLLOVER_ENV_BACKUP" \
    || fail "could not back up timecap-lift activation environment"
  "$PY" - "$ROLLOVER_ENV" <<'PY' || fail "could not activate timecap-lift environment"
import os
import stat
import sys
import tempfile

path = sys.argv[1]
key = "MEMECOIN_TIMECAP_LIFT_ENABLED"
with open(path) as handle:
    lines = handle.readlines()
out = []
written = False
for line in lines:
    stripped = line.strip()
    line_key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
    if line_key == key and not stripped.startswith("#"):
        if not written:
            out.append("MEMECOIN_TIMECAP_LIFT_ENABLED=1\n")
            written = True
    else:
        out.append(line)
if not written:
    out.append("MEMECOIN_TIMECAP_LIFT_ENABLED=1\n")
mode = stat.S_IMODE(os.stat(path).st_mode)
fd, temporary = tempfile.mkstemp(prefix=".env.timecap.", dir=os.path.dirname(path) or ".")
try:
    with os.fdopen(fd, "w") as handle:
        handle.writelines(out)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
  ROLLOVER_ENV_CHANGED=1
  verify_candidate_release
  validate_funded_contracts
fi

if [ "$ACTIVATE_ROLLOVER" -eq 1 ] || [ "$ACTIVATE_BAND_POLICY" -eq 1 ]; then
  [ -f "$ROLLOVER_ENV" ] || fail "rollover activation requires $ROLLOVER_ENV"
  trap pre_switch_rollover_rollback EXIT
  if [ "$ACTIVATE_BAND_POLICY" -eq 1 ]; then
    EXISTING_CONTRACT_CUTOFF="$("$PY" - "$ROLLOVER_ENV" <<'PY'
import sys

value = ""
with open(sys.argv[1]) as handle:
    for line in handle:
        stripped = line.strip()
        if stripped.startswith("MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT="):
            value = stripped.split("=", 1)[1].strip()
print(value)
PY
)"
    [ -n "$EXISTING_CONTRACT_CUTOFF" ] \
      || fail "band policy activation requires an existing funded contract cutoff"
  fi
  if [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ]; then
    wait_for_exit_drain "line 756"
    RUNNER_PIDS="$($PY "$SOURCE_ROOT/scripts/memecoin_runtime_processes.py" \
      --expected "$RELEASE/scripts/memecoin_runner_loop.py" | awk -F '\t' '{print $1}')"
    for OLD_PID in $RUNNER_PIDS; do
      kill "$OLD_PID" 2>/dev/null || true
      for _ in $(seq 1 20); do kill -0 "$OLD_PID" 2>/dev/null || break; sleep 0.25; done
      kill -0 "$OLD_PID" 2>/dev/null && fail "old runner pid $OLD_PID did not stop for rollover anchor"
    done
    ROLLOVER_OLD_RUNNER_STOPPED=1
  fi
  release_unsigned_reservations_after_runner_stop \
    || fail "could not release unsigned reservations after runner stop"
  release_unsigned_close_claims_after_runner_stop \
    || fail "could not release unsigned close claims after runner stop"
  PENDING="$(pending_reservations)" || fail "could not recheck pending reservations after runner stop"
  [ "$PENDING" = "0" ] || fail "$PENDING pending reservation(s) block rollover cutover"
  ROLLOVER_ENV_BACKUP="$STATE_ROOT/data/.env.rollover.previous.$$"
  cp -p "$ROLLOVER_ENV" "$ROLLOVER_ENV_BACKUP" || fail "could not back up rollover environment"
  CUTOFF="${MEMECOIN_ROLLOVER_CUTOFF_OVERRIDE:-}"
  if [ -z "$CUTOFF" ]; then
    CUTOFF="$(env PYTHONPATH="$RELEASE" SNIPER_STATE_ROOT="$STATE_ROOT" \
      SNIPER_CLOCK_OFFSET_FILE="$STATE_ROOT/data/clock_offset.json" "$PY" -c \
      'from sniper.util.clock import real_utc_now_iso; print(real_utc_now_iso())')" \
      || fail "could not generate corrected rollover cutoff"
  fi
  ACTIVATION_KIND="rollover"
  [ "$ACTIVATE_BAND_POLICY" -eq 0 ] || ACTIVATION_KIND="band_policy"
  "$PY" - "$ROLLOVER_ENV" "$CUTOFF" "$ACTIVATION_KIND" <<'PY' \
    || fail "could not activate rollover environment"
import os
import stat
import sys
import tempfile

path, cutoff, activation_kind = sys.argv[1:]
rollover_updates = {
    "MEMECOIN_MAX_CONCURRENT": "7",
    "MEMECOIN_MAX_TOTAL_EXPOSURE_USD": "700",
    "MEMECOIN_REAL_TP_PCT": "0",
    "MEMECOIN_REAL_DERISK_PCT": "40",
    "MEMECOIN_REAL_LEGACY_TP_PCT": "60",
    "MEMECOIN_REAL_TRAIL_PCT": "30",
    "MEMECOIN_REAL_TRAIL_ARM_PCT": "15",
    "MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT": cutoff,
    "MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT": cutoff,
    "MEMECOIN_FUNDED_CLASSIFICATION_VERSION": "1",
    "MEMECOIN_FUNDED_REGIME_RISK_SCALE": "0.75",
    "MEMECOIN_FUNDED_TAPE_WINDOW_S": "900",
    "MEMECOIN_FUNDED_TAPE_MIN_OBSERVATIONS": "3",
    "MEMECOIN_FUNDED_TAPE_MIN_SPAN_S": "30",
    "MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT": "20",
    "MEMECOIN_FUNDED_POST_PEAK_GIVEBACK_PCT": "30",
    "MEMECOIN_FUNDED_REGIME_BUCKET_S": "900",
    "MEMECOIN_FUNDED_REGIME_BASELINE_BUCKETS": "23",
    "MEMECOIN_FUNDED_REGIME_MIN_NONEMPTY_BUCKETS": "8",
    "MEMECOIN_FUNDED_REGIME_MIN_BASELINE_BUYS": "30",
    "MEMECOIN_FUNDED_REGIME_MAX_STALENESS_S": "180",
    "MEMECOIN_REAL_NANO_DERISK_PCT": "40",
    "MEMECOIN_REAL_NANO_TRAIL_PCT": "40",
    "MEMECOIN_REAL_NANO_SL_PCT": "60",
    "MEMECOIN_REAL_MICRO_DERISK_PCT": "50",
    "MEMECOIN_REAL_MICRO_TRAIL_PCT": "50",
    "MEMECOIN_REAL_MICRO_SL_PCT": "60",
    "MEMECOIN_REAL_MID_DERISK_PCT": "30",
    "MEMECOIN_REAL_MID_TRAIL_PCT": "35",
    "MEMECOIN_REAL_MID_SL_PCT": "60",
}
band_policy_updates = {
    "MEMECOIN_FOLLOW3050_ALL_WALLETS": "1",
    "MEMECOIN_REAL_BAND_POLICY_BANDS": "nano,micro",
    "MEMECOIN_REAL_PARTIAL_DERISK_PCT": "20",
    "MEMECOIN_REAL_PARTIAL_DERISK_FRACTION": "0.5",
    "MEMECOIN_REAL_PARTIAL_TRAIL_PCT": "30",
    "MEMECOIN_REAL_NANO_SL_PCT": "60",
    "MEMECOIN_REAL_MICRO_SL_PCT": "60",
    "MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT": cutoff,
}
updates = band_policy_updates if activation_kind == "band_policy" else rollover_updates
with open(path) as handle:
    lines = handle.readlines()
seen = set()
out = []
for line in lines:
    stripped = line.strip()
    key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
    if key in updates:
        if key not in seen:
            out.append(f"{key}={updates[key]}\n")
            seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}\n")
mode = stat.S_IMODE(os.stat(path).st_mode)
fd, temporary = tempfile.mkstemp(prefix=".env.rollover.", dir=os.path.dirname(path) or ".")
try:
    with os.fdopen(fd, "w") as handle:
        handle.writelines(out)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
  ROLLOVER_ENV_CHANGED=1
  verify_candidate_release
  validate_funded_contracts
fi

if [ "${MEMECOIN_DEPLOY_FAIL_STAGE:-}" = "pre_switch" ]; then
  rm -f "$CANDIDATE_MANIFEST"
  fail "injected pre-switch failure"
fi

if [ -f "$MANIFEST" ]; then cp -p "$MANIFEST" "$OLD_MANIFEST"; else : > "$OLD_MANIFEST"; fi
SWITCHED=0
SUCCESS=0
rollback() {
  rc=$?
  if [ "${PROVIDER_RH_CRON_HANDOFF:-0}" -eq 1 ] && ! retake_provider_rh_watchdog_lock; then
    echo "RH handoff interrupted without watchdog ownership; entry hold and recovery artifacts preserved" >&2
    trap - EXIT
    exit "$rc"
  fi
  if [ "$SUCCESS" -eq 0 ] && { [ "$SWITCHED" -eq 1 ] || [ "$ROLLOVER_OLD_RUNNER_STOPPED" -eq 1 ]; } \
      && ! source_mirror_rollback_safe; then
    echo "source mirror rollback refused; candidate runtime, configuration, entry hold and recovery files preserved" >&2
    trap - EXIT
    [ "$rc" -ne 0 ] || rc=1
    exit "$rc"
  fi
  if [ "$SUCCESS" -eq 0 ] && { [ -n "$PROVIDER_ENV" ] || [ "${SHYFT_RUNTIME:-0}" -eq 1 ]; } \
      && [ "${PROVIDER_CANDIDATE_LAUNCHED:-0}" -eq 1 ] \
      && [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ]; then
    stop_provider_processes || PROVIDER_RECOVERY_REQUIRED=1
  fi
  restore_lifi_env
  restore_rollover_env
  restore_provider_env || PROVIDER_RECOVERY_REQUIRED=1
  if [ "$SUCCESS" -eq 0 ] && [ "$SWITCHED" -eq 1 ]; then
    echo "post-switch failure; restoring previous memecoin release" >&2
    if [ -n "$OLD_RELEASE" ] && [ -d "$OLD_RELEASE" ]; then
      ln -s "$OLD_RELEASE" "$CURRENT.rollback.$$"
      mv -Tf "$CURRENT.rollback.$$" "$CURRENT"
    else
      rm -f "$CURRENT"
    fi
    if [ -s "$OLD_MANIFEST" ]; then mv -f "$OLD_MANIFEST" "$MANIFEST"; else rm -f "$MANIFEST"; fi
    if [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ] && [ -n "$OLD_RELEASE" ] \
        && [ "$PROVIDER_RECOVERY_REQUIRED" -eq 0 ]; then
      [ -n "$PROVIDER_ENV" ] || [ "${SHYFT_RUNTIME:-0}" -eq 1 ] \
        || pkill -f -- "$RELEASE/scripts/memecoin_runner_loop.py" 2>/dev/null || true
      PROVIDER_ROLLBACK_STARTED="$(date +%s)"
      if [ "${PROVIDER_RH_STOPPED:-0}" -eq 1 ]; then
        start_provider_rh "$OLD_RELEASE" || PROVIDER_RECOVERY_REQUIRED=1
      fi
      if [ "$LAUNCH_VIA_CRON" -eq 1 ] && wait_for_cron_launch "$OLD_RELEASE"; then
        :
      else
        [ "$LAUNCH_VIA_CRON" -eq 0 ] || cron_launch_failed_loudly
        if ! \
          SNIPER_STATE_ROOT="$STATE_ROOT" MEMECOIN_WATCHDOG_LOCK_HELD=1 bash "$OLD_RELEASE/scripts/memecoin_watchdog.sh" 7>&- 8>&- 9>&-; then
          { [ -z "$PROVIDER_ENV" ] && [ "${SHYFT_RUNTIME:-0}" -eq 0 ]; } || PROVIDER_RECOVERY_REQUIRED=1
        fi
      fi
      if { [ -n "$PROVIDER_ENV" ] || [ "${SHYFT_RUNTIME:-0}" -eq 1 ]; } \
          && [ "$PROVIDER_RECOVERY_REQUIRED" -eq 0 ]; then
        verify_provider_rollback || PROVIDER_RECOVERY_REQUIRED=1
      fi
    elif [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ] && [ -z "$PROVIDER_ENV" ] \
        && [ "${SHYFT_RUNTIME:-0}" -eq 0 ]; then
      # First pinned cutover has no prior release. Relaunch the known clean source checkout only
      # as an emergency rollback; the watchdog itself never has this fallback.
      SOURCE_MATCH="$SOURCE_ROOT/scripts/memecoin_runner_loop.py"
      SOURCE_RUNNERS="$($PY "$SOURCE_ROOT/scripts/memecoin_runtime_processes.py" \
        --expected "$SOURCE_MATCH" 2>/dev/null || true)"
      SOURCE_PIDS="$(printf '%s\n' "$SOURCE_RUNNERS" | awk -F '\t' 'NF {print $1}')"
      if [ -z "$SOURCE_PIDS" ]; then
        nohup env PYTHONPATH="$SOURCE_ROOT" SNIPER_STATE_ROOT="$STATE_ROOT" \
          "$PY" -u "$SOURCE_MATCH" >> "$STATE_ROOT/logs/memecoin_runner.log" 2>&1 &
        echo $! > "$STATE_ROOT/data/memecoin_runner.pid"
      elif [ "$(printf '%s\n' "$SOURCE_PIDS" | awk 'NF {n++} END {print n+0}')" -eq 1 ]; then
        printf '%s\n' "$SOURCE_PIDS" > "$STATE_ROOT/data/memecoin_runner.pid"
      fi
    fi
  elif [ "$SUCCESS" -eq 0 ] && [ "$ROLLOVER_OLD_RUNNER_STOPPED" -eq 1 ]; then
    if { [ -n "$PROVIDER_ENV" ] || [ "${SHYFT_RUNTIME:-0}" -eq 1 ]; } \
        && [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ]; then
      if [ "$PROVIDER_RECOVERY_REQUIRED" -eq 0 ]; then
        PROVIDER_ROLLBACK_STARTED="$(date +%s)"
        if [ "${PROVIDER_RH_STOPPED:-0}" -eq 1 ]; then
          start_provider_rh "$OLD_RELEASE" || PROVIDER_RECOVERY_REQUIRED=1
        fi
        if [ "$PROVIDER_RECOVERY_REQUIRED" -eq 0 ] && [ -n "$OLD_RELEASE" ] && restart_old_rollover_runner; then
          verify_provider_rollback || PROVIDER_RECOVERY_REQUIRED=1
        else
          PROVIDER_RECOVERY_REQUIRED=1
        fi
      fi
    else
      restart_old_rollover_runner
    fi
  fi
  if [ "$PROVIDER_RECOVERY_REQUIRED" -eq 0 ]; then
    clear_deploy_entry_gate || true
    page_shyft_preexisting "rolled back" || true      # after the clear: a slow send never holds entries
    rm -f "$DEPLOY_INTENT" 2>/dev/null || true
  else
    if [ "${ROLLBACK_RUNNER_VERIFIED:-0}" -eq 1 ]; then
      # THE RESTORED RUNNER IS VERIFIED (Alpha 2026-10-16): nothing is half-applied, so the intent
      # file would only raise a false "deploy did not land" alarm, and the two process records have
      # no reader once this script exits (they serve stop_provider_processes and the RH cutover of
      # this run). The entry hold stays (the runner pages it past the grace), and so does the private
      # copy of the pre-deploy .env, the one file a manual recovery needs when restore_provider_env failed.
      rm -f "$DEPLOY_INTENT" "${PROVIDER_ORIGINAL_IDENTITIES:-}" "${PROVIDER_RH_RECORD:-}" 2>/dev/null || true
    fi
    echo "provider rollback incomplete; entry hold and private recovery environment preserved" >&2
  fi
  rm -f "$CANDIDATE_MANIFEST" "$OLD_MANIFEST" "$CURRENT.next.$$" \
    "$ROLLOVER_ENV_BACKUP" "$LIFI_ENV_ROLLBACK" "${SHYFT_BASELINE:-}" 2>/dev/null || true
  cleanup_provider_stage
  trap - EXIT
  exit "$rc"
}
trap rollback EXIT
# Signals exit with their conventional status so `rollback` reports a FAILURE rather than rc=0.
# Measured 2026-09-02, and it corrects my own first diagnosis: bash already runs the EXIT trap on
# SIGTERM here, so the rollback (and its `ROLLOVER_OLD_RUNNER_STOPPED` restart branch) was always
# reached -- it just exited 0, which reads as success to anything checking the status. These three
# lines fix the status, NOT the outage. The 14:56Z deploy that left the runner down was killed
# harder than a trap can catch; only $DEPLOY_INTENT covers that, and that is the real fix here.
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

# Recheck peer environment drift after validation and before entry admission is held.
verify_provider_stage
if [ -n "$PROVIDER_ENV" ] || [ "$(grep -E '^MEMECOIN_SOLANA_INFRA_PROVIDER=' "$STATE_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '"'"'"' ')" = shyft ]; then
  SHYFT_RUNTIME=1
  if [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != 1 ]; then
    exec 7> "$STATE_ROOT/data/memecoin_rh_feed_shadow.watchdog.lock"
    flock -w 30 7 || fail "RH watchdog cutover lock unavailable"
    capture_provider_originals || fail "could not capture original provider identities"
  fi
fi

# The baseline before the cron-aligned wait and the trip: no added entries-down time, and a vendor that
# dies during the drain then refuses (the conservative side).
if [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ]; then
  snapshot_shyft_baseline
fi
if [ "$LAUNCH_VIA_CRON" -eq 1 ] && [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ]; then
  TRIP_SECOND="$(date +%S)"
  TRIP_WAIT_S="$(cron_trip_wait_s "$TRIP_SECOND")"
  step "cron_aligned_trip_wait second=$TRIP_SECOND wait_s=$TRIP_WAIT_S"
  sleep "$TRIP_WAIT_S"
fi
step "entry_gate_trip"
# Stop new entry admission, let the current entry finish, then stop the runner. This closes the
# validation-to-stop race without stopping exits during validation.
if [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ]; then
  DEPLOY_KILL_PATH="$(env PYTHONSAFEPATH=1 PYTHONPATH="$DEFAULT_SOURCE" SNIPER_STATE_ROOT="$STATE_ROOT" "$PY" -c \
    'from sniper.services.memecoin_arming import _kill_path; print(_kill_path())')" \
    || fail "could not resolve the entry gate"
  DEPLOY_KILL_TOKEN="$(env PYTHONSAFEPATH=1 PYTHONPATH="$DEFAULT_SOURCE" SNIPER_STATE_ROOT="$STATE_ROOT" "$PY" -c \
    "from sniper.services.memecoin_arming import trip_kill_switch; print(trip_kill_switch(source='runtime-deploy', only_if_clear=True) or '')")" \
    || fail "could not stop entry admission"
  if [ -n "$DEPLOY_KILL_TOKEN" ]; then
    DEPLOY_KILL_OWNED=1
  fi
  PENDING="$(pending_reservations)" || fail "could not read reservations during entry drain"
  for _ in $(seq 1 130); do
    [ "$PENDING" = "0" ] && break
    sleep 1
    PENDING="$(pending_reservations)" || fail "could not read reservations during entry drain"
  done
  [ "$PENDING" = "0" ] || fail "$PENDING pending reservation(s) did not drain"
  require_flat_book
  # DECLARE THE INTENT BEFORE TOUCHING THE RUNNER. A trap cannot survive SIGKILL, and a
  # half-completed deploy is otherwise INDISTINGUISHABLE FROM SUCCESS: the watchdog relaunches from
  # `current`, which still points at the OLD release, so the runner comes back healthy on the wrong
  # code and nothing anywhere says the deploy did not land. This file is the only evidence that
  # survives an unkillable death. `memecoin_overnight_watch.runner_health` alarms on it; the
  # rollback and the success path both remove it.
  printf '%s\n' "$SHA" > "$DEPLOY_INTENT" 2>/dev/null || true
  step "entries_drained"
  if [ "$ROLLOVER_OLD_RUNNER_STOPPED" -eq 0 ]; then
    EXPECTED_RUNNER="${OLD_RELEASE:-$SOURCE_ROOT}/scripts/memecoin_runner_loop.py"
    wait_for_exit_drain "line 954"
    if [ -n "$PROVIDER_ENV" ] || [ "$SHYFT_RUNTIME" -eq 1 ]; then
      RUNNER_PIDS="$(provider_runner_pids "$EXPECTED_RUNNER")" || fail "funded state runner changed before stop"
    else
      RUNNER_PIDS="$($PY "$SOURCE_ROOT/scripts/memecoin_runtime_processes.py" \
        --expected "$EXPECTED_RUNNER" | awk -F '\t' '{print $1}')"
    fi
    for OLD_PID in $RUNNER_PIDS; do
      kill "$OLD_PID" 2>/dev/null || true
      for _ in $(seq 1 20); do kill -0 "$OLD_PID" 2>/dev/null || break; sleep 0.25; done
      kill -0 "$OLD_PID" 2>/dev/null && fail "old runner pid $OLD_PID did not stop"
    done
    ROLLOVER_OLD_RUNNER_STOPPED=1
  fi
  step "old_runner_stopped"
fi
[ "${MEMECOIN_DEPLOY_FAIL_STAGE:-}" != "old_stop" ] || fail "injected old_stop failure"
release_unsigned_reservations_after_runner_stop \
  || fail "could not release unsigned reservations before cutover"
release_unsigned_close_claims_after_runner_stop \
  || fail "could not release unsigned close claims before cutover"
UNRESOLVED_SUBMISSIONS="$(unresolved_submission_reservations)" \
  || fail "could not recheck submitted reservations before cutover"
[ "$UNRESOLVED_SUBMISSIONS" = "0" ] \
  || fail "$UNRESOLVED_SUBMISSIONS signed or ambiguous reservation(s) block cutover"
PENDING="$(pending_reservations)" || fail "could not recheck reservations before cutover"
[ "$PENDING" = "0" ] || fail "$PENDING pending reservation(s) block cutover"
require_flat_book

if [ "$SHYFT_RUNTIME" -eq 1 ] && [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != 1 ]; then
  prepare_provider_rh_cutover || fail "old RH provider process did not stop"
  step "old_rh_provider_stopped"
fi

if [ -n "$PROVIDER_ENV" ]; then
  # The old runner is stopped and signed/ambiguous reservations have been reconciled.
  verify_provider_stage
  PROVIDER_BACKUP="$(mktemp "$STATE_ROOT/data/.provider-rollback.XXXXXX")"
  PROVIDER_OLD_MODE="$(stat -c %a "$STATE_ROOT/.env")"
  install -m 600 "$STATE_ROOT/.env" "$PROVIDER_BACKUP" || fail "could not preserve provider environment"
  PROVIDER_CHANGED=1
  "$PY" - "$PROVIDER_ENV" "$STATE_ROOT/.env" "$PROVIDER_OLD_MODE" <<'PY' \
    || fail "could not apply provider stage"
import os, sys, tempfile
source, destination, mode = sys.argv[1:]
fd, temporary = tempfile.mkstemp(prefix=".provider-apply.", dir=os.path.dirname(destination))
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(open(source, "rb").read())
        handle.flush(); os.fsync(handle.fileno())
    os.chmod(temporary, int(mode, 8))
    os.replace(temporary, destination)
finally:
    if os.path.exists(temporary): os.unlink(temporary)
PY
  PROVIDER_APPLIED=1
  [ "${MEMECOIN_DEPLOY_FAIL_STAGE:-}" != "env_write" ] || fail "injected env_write failure"
fi

SWITCHED=1
mv -f "$CANDIDATE_MANIFEST" "$MANIFEST"
[ "${MEMECOIN_DEPLOY_FAIL_STAGE:-}" != "manifest_switch" ] || fail "injected manifest_switch failure"
ln -s "$RELEASE" "$CURRENT.next.$$"
mv -Tf "$CURRENT.next.$$" "$CURRENT"
step "switched"

if [ -n "$LIFI_REACTIVATION_CANARY" ]; then
  LIFI_ENV_ROLLBACK="$STATE_ROOT/data/.env.lifi.rollback.$$"
  cp -p "$ROLLOVER_ENV" "$LIFI_ENV_ROLLBACK" \
    || fail "could not preserve the LI.FI environment for rollback"
  LIFI_ENV_CHANGED=1
  step "lifi_activate_start"
  env PYTHONPATH="$RELEASE" SNIPER_STATE_ROOT="$STATE_ROOT" DB_PATH="$DB" \
    "$PY" "$RELEASE/scripts/memecoin_lifi_activate.py" \
      --canary-lifecycle "$LIFI_REACTIVATION_CANARY" --release-sha "$SHA" --confirm \
    || fail "could not reactivate LI.FI for the candidate release"
  LIFI_REACTIVATED=1
  step "lifi_activate_end"
  verify_candidate_release
fi

[ "${MEMECOIN_DEPLOY_FAIL_STAGE:-}" != "post_switch" ] || { echo "injected post-switch failure" >&2; false; }

if [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ]; then
# JOINT_CRON_LAUNCH_BEGIN
  CUTOVER_STARTED="$(date +%s)"
  PIDFILE="${MEMECOIN_PIDFILE:-$STATE_ROOT/data/memecoin_runner.pid}"
  # ONE CRON TICK FOR THE FEED AND THE RUNNER (2026-10-15, Alpha). Handing the feed to cron first
  # and the runner only after the feed came up put the runner handoff just after a minute tick:
  # every deploy since 2026-10-14 waited 83-84 s from trip to runner launch (22.5-26 s before).
  # Through cron, the feed's lock is now released together with the runner's and the feed is
  # awaited after the runner. A feed that does not come up still fails the deploy; the rollback
  # stops every candidate process (PROVIDER_CANDIDATE_LAUNCHED), the new runner included. The
  # rollback paths keep feed-then-runner.
  JOINT_RH_CRON=0
  if [ "$SHYFT_RUNTIME" -eq 1 ]; then
    PROVIDER_CANDIDATE_LAUNCHED=1
    if [ "$LAUNCH_VIA_CRON" -eq 1 ]; then
      JOINT_RH_CRON=1
    else
      start_provider_rh "$RELEASE" || fail "new RH provider process failed"
    fi
  fi
  # Discover by resolved script path so a stale or missing PID file cannot leave an old runner live.
  wait_for_exit_drain "line 999"
  if [ -n "$PROVIDER_ENV" ] || [ "$SHYFT_RUNTIME" -eq 1 ]; then
    RUNNER_PIDS="$(provider_runner_pids "$RELEASE/scripts/memecoin_runner_loop.py")" || fail "funded state runner changed before launch"
  else
    RUNNER_PIDS="$($PY "$SOURCE_ROOT/scripts/memecoin_runtime_processes.py" \
      --expected "$RELEASE/scripts/memecoin_runner_loop.py" | awk -F '\t' '{print $1}')"
  fi
  for OLD_PID in $RUNNER_PIDS; do
    kill "$OLD_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$OLD_PID" 2>/dev/null || break; sleep 0.25; done
    kill -0 "$OLD_PID" 2>/dev/null && fail "old runner pid $OLD_PID did not stop"
  done
  # The stopped runner cannot sign after this point. Release only reservations with no durable
  # submission identity; signed or ambiguous transactions stay reserved for chain recovery.
  release_unsigned_reservations_after_runner_stop \
    || fail "could not release unsigned reservations after runner stop"
  release_unsigned_close_claims_after_runner_stop \
    || fail "could not release unsigned close claims after runner stop"
  rm -f "$PIDFILE"
  step "new_runner_launch"
  [ -z "$PROVIDER_ENV" ] || PROVIDER_CANDIDATE_LAUNCHED=1
  if [ "$JOINT_RH_CRON" -eq 1 ]; then
    # Set before the release so an interrupted wait re-takes the feed's lock in the rollback.
    PROVIDER_RH_CRON_HANDOFF=1
    step "rh_launch_handed_to_cron release=$RELEASE with_runner=1"
    exec 7>&-
  fi
  # Outside system.slice the cron watchdog launches it (see LAUNCH_VIA_CRON); the direct call below
  # is then only the loud fallback. The SHA verify_candidate_release has just passed; the watchdog
  # skips its own repeat verify for exactly this release and removes the variable before anything
  # else runs.
  if [ "$LAUNCH_VIA_CRON" -eq 1 ] && wait_for_cron_launch "$RELEASE"; then
    :
  else
    [ "$LAUNCH_VIA_CRON" -eq 0 ] || cron_launch_failed_loudly
    SNIPER_STATE_ROOT="$STATE_ROOT" MEMECOIN_WATCHDOG_LOCK_HELD=1 MEMECOIN_WATCHDOG_RELEASE_VERIFIED="$SHA" bash "$CURRENT/scripts/memecoin_watchdog.sh" 7>&- 8>&- 9>&- \
      || { echo "new pinned watchdog failed" >&2; false; }
  fi
  if [ "$JOINT_RH_CRON" -eq 1 ]; then
    start_provider_rh "$RELEASE" || fail "new RH provider process failed"
  fi
# JOINT_CRON_LAUNCH_END
  VERIFIED=0
  for _ in $(seq 1 60); do
    NEW_PID="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [[ "$NEW_PID" =~ ^[0-9]+$ ]] && kill -0 "$NEW_PID" 2>/dev/null; then
      CMDLINE="$(tr '\0' ' ' < "/proc/$NEW_PID/cmdline" 2>/dev/null || true)"
      HEALTH="$(PYTHONPATH="$RELEASE" SNIPER_STATE_ROOT="$STATE_ROOT" "$PY" -c \
        "import sys,time; from sniper.services.memecoin_runner import read_heartbeat,runner_launch_health; print(runner_launch_health(read_heartbeat(),float(sys.argv[1]),time.time())['reason'])" \
        "$CUTOVER_STARTED" \
        2>/dev/null || true)"
      if [[ "$CMDLINE" == *"$RELEASE/scripts/memecoin_runner_loop.py"* ]] && [ "$HEALTH" = "ok" ] && provider_runtime_ready; then
        VERIFIED=1; break
      fi
    fi
    sleep 1
  done
  [ "$VERIFIED" -eq 1 ] || {
    [ -z "${SHYFT_LAST_FAILURE:-}" ] || step "shyft_runtime_gate_failed $SHYFT_LAST_FAILURE"
    echo "new runner failed PID/path/heartbeat verification" >&2; false;
  }
  step "new_runner_verified"
fi

[ "${MEMECOIN_DEPLOY_FAIL_STAGE:-}" != "new_start" ] || fail "injected new_start failure"

clear_deploy_entry_gate || fail "could not restore entry admission"
step "entry_gate_clear"
SUCCESS=1
rm -f "$DEPLOY_INTENT" 2>/dev/null || true   # the deploy landed; nothing left to alarm about
ROLLOVER_ENV_CHANGED=0
LIFI_ENV_CHANGED=0
PROVIDER_CHANGED=0
cleanup_provider_stage
rm -f "$OLD_MANIFEST" "$ROLLOVER_ENV_BACKUP" "$LIFI_ENV_ROLLBACK"
rm -f "${SHYFT_BASELINE:-}" 2>/dev/null || true
trap - EXIT
# THE LANDED PAGE, AFTER THE ROLLBACK TRAP IS GONE (Gamma LOW-1 on bb1047f29, MEDIUM on f58402084): the
# send can block up to its 20 s bound, and a TERM inside the trap's reach would either roll back a landed
# release (before SUCCESS=1) or restore the pre-deploy .env under it (before the *_CHANGED resets). It
# reads only SHYFT_PREEXISTING, and it comes after the clear, so it never holds entries either.
page_shyft_preexisting "landed"
# A RELEASE-PINNED WORKER IS CUT OVER HERE, NEVER BY ITS WATCHDOG (see the call site below). Stop by
# PIDFILE only, and only a process running `script` from ANOTHER release; the watchdog relaunches it
# from `current`. Non-fatal throughout: these lanes observe only.
worker_running() {
  kill -0 "$1" 2>/dev/null || return 1
  local stat
  stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
  stat="${stat##*) }"
  [ "${stat%% *}" != "Z" ]
}
stop_stale_worker() {
  local label="$1" pidfile="$2" script="$3" pid cmd
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if ! [[ "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  if [[ "$cmd" != *"$script"* ]]; then
    echo "WARNING: $pidfile pid $pid is not the $label; left alone" >&2
    return 0
  fi
  if [[ "$cmd" == *"$RELEASE/$script"* ]]; then
    echo "$label pid $pid already on $SHA; not restarting"
    return 0
  fi
  kill "$pid" 2>/dev/null || true
  # A KILLED WORKER IS NOT ALIVE WHILE IT WAITS TO BE REAPED (2026-10-15, Gamma full run). The
  # worker is detached, so its reaper is the user's systemd; under load a zombie answered kill -0
  # for the whole 5 s and the deploy warned "did not stop". A zombie (stat state Z) counts as
  # stopped, and the wait is 10 s.
  for _ in $(seq 1 40); do worker_running "$pid" || break; sleep 0.25; done
  if worker_running "$pid"; then
    echo "WARNING: $label pid $pid did not stop; it stays on the old release" >&2
  else
    echo "$label pid $pid stopped; watchdog will relaunch it on $SHA"
  fi
}

if [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ]; then
  # AFTER THE CLEAR, ON PURPOSE (2026-09-27). This stop used to sit inside the entry-gated window,
  # between the new runner's verification and the CLEAR, adding its kill and up to 5 s of polling
  # to every deploy's stand-down for a lane that trades nothing. Here the deploy has already
  # succeeded and its rollback trap is cleared, so nothing below can undo it.
  #
  # CUT THE SOLANA TAPE WORKER OVER TOO. Nothing else does it: its watchdog refuses on purpose
  # ("Cutting a worker over to a new release is the deploy's job, not this watchdog's" —
  # memecoin_sol_tape_watchdog.sh), and this script never handled it, so the worker ran whatever
  # release it started on. Found 2026-08-25 three days and four deploys stale.
  #
  # STOP ONLY. The per-minute watchdog relaunches it from `current` within ~60s, and its discovery
  # is release-INDEPENDENT so it cannot start a second worker on the same tape DB. Starting it here
  # would race that. Measured on the manual cutover: relaunched in 25s.
  #
  # Stopped by PIDFILE, never by pattern — `pkill -f <sha>` has killed a deploy's own shell here
  # before. Non-fatal throughout: the tape is observation-only and a funded deploy must not fail
  # because a research lane would not stop.
  # THE SOLANA COHORT STREAM TOO (Alpha, 2026-10-09): its watchdog is the same shape, and the deploy
  # of e1beb1c23 left it on 7fd644c1 until it was SIGTERMed by hand.
  stop_stale_worker "sol tape worker" "$STATE_ROOT/data/memecoin_sol_tape.pid" "scripts/memecoin_sol_tape_worker.py"
  stop_stale_worker "sol cohort worker" "$STATE_ROOT/data/memecoin_sol_cohort.pid" "scripts/memecoin_sol_cohort_stream.py"
fi

# Publish the rollback pointer BEFORE pruning, and only after SUCCESS with the trap cleared. Written
# atomically via a temp symlink + `mv -Tf`, the same way CURRENT is switched, so a crash mid-write
# leaves the old pointer rather than a half-made one. Skipped on a first-ever deploy (no OLD_RELEASE)
# and when the old release directory is already gone, because a pointer to nothing is worse than an
# absent one -- it reads as a rollback target right up until you need it.
if [ -n "${OLD_RELEASE:-}" ] && [ -d "$OLD_RELEASE" ] && [ "$OLD_RELEASE" != "$RELEASE" ]; then
  ln -s "$OLD_RELEASE" "$PREVIOUS.next.$$" 2>/dev/null \
    && mv -Tf "$PREVIOUS.next.$$" "$PREVIOUS" 2>/dev/null \
    || rm -f "$PREVIOUS.next.$$"
fi
prune_old_releases || true    # after SUCCESS + trap cleared: a prune hiccup never undoes the deploy
[ "$DEFER_BACKUP" -eq 1 ] || prune_old_backups || true
echo "deployed memecoin runtime old=${OLD_SHA:-none} new=$SHA release=$RELEASE backup=$BACKUP"
# Only memecoin_deploy_pinned.sh used to deploy the web, so every direct deploy and every --activate-*
# mode left .runtime/web on an old SHA (web e37d439e while the runner ran 1e0a3a045, 2026-10-02).
# Every RESTARTED runner deploy now deploys the same SHA to the web, once. A web failure never rolls
# the runner back (the runner is live and correct; the web script restores its own previous release)
# and ends this script non-zero below, so no caller reads it as a clean deploy. The harness never
# cycles a process: the web script's cycle finds the LIVE dashboard by its argv.
WEB_DEPLOY_FAILED=0
if [ "$RESTART" -eq 1 ]; then
  # The runner is live and the cutover is over: hand the watchdog its lock back first, so a web deploy
  # of up to a minute (import smoke, cycle, come-back wait) never holds off the runner's watchdog. The
  # deploy lock (fd 9) stays held, so no second deploy starts meanwhile; the web script gets neither.
  exec 8>&-
  WEB_ARGS=("$SHA")
  [ "$ALLOW_LOCAL_COMMIT" -eq 0 ] || WEB_ARGS+=(--allow-local-commit)
  [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" = "1" ] && WEB_ARGS+=(--no-cycle)
  if bash "$WEB_DEPLOY_SCRIPT" "${WEB_ARGS[@]}" 8>&- 9>&-; then
    echo "web deployed: $SHA"
  else
    WEB_DEPLOY_FAILED=1
    echo "!!! runner deployed, web deploy FAILED (sha=$SHA): the web is still on its previous release." \
         "Re-run: scripts/deploy_web_runtime.sh ${WEB_ARGS[*]}" >&2
  fi
fi
if [ "${NEW_PID:-}" ] && [ -r "/proc/$NEW_PID/cgroup" ]; then
  echo "runner pid $NEW_PID cgroup $(cat "/proc/$NEW_PID/cgroup")"
fi
# The deploy itself landed, but its runner lives in a scope a session crash can stop. Exit non-zero
# so no caller reads this as a clean deploy; the cron watchdog does not move a running runner.
if [ "$CRON_LAUNCH_FALLBACK" -eq 1 ]; then
  echo "!!! DEPLOYED, BUT THE RUNNER WAS LAUNCHED DIRECTLY INSIDE THE CALLER'S SCOPE (cron did not" \
       "launch it). Stop it and let cron relaunch it from system.slice." >&2
  exit 3
fi
[ "$WEB_DEPLOY_FAILED" -eq 0 ] || exit 4
