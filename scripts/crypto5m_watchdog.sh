#!/usr/bin/env bash
# Crypto-5m process watchdog: restart any cohort/capture process that has died.
# Safe: only relaunches when the pidfile PID is dead AND no matching process already runs
# (reattaches to a stray instead of spawning a duplicate). Read-only research processes; no orders.
set -u
cd /home/bigpapi/Desktop/Sniper || exit 1
PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
TS=$(date -u +%FT%TZ)
log() { echo "$TS $*" >> logs/crypto5m_watchdog.log; }

ensure() {
  local name=$1 pidfile=$2 match=$3 launch=$4
  local pid; pid=$(cat "$pidfile" 2>/dev/null || true)
  # Duplicate cull (added 2026-07-07 after review): ensure() used to reattach to the FIRST match
  # but never killed a second, so a manual-launch-vs-cron race could leave TWO writers on one
  # campaign DB. maker_windows is PK-idempotent but maker_fills is append-only, so overlap
  # contaminates that campaign's research data. Keep the pidfile pid if alive, else the oldest
  # (lowest pid); kill the rest before anything else.
  local matches; matches=$(pgrep -f "$match" | sort -n)
  local nmatch; nmatch=$(printf '%s\n' "$matches" | grep -c . || true)
  if [ "${nmatch:-0}" -gt 1 ]; then
    local keep
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null && printf '%s\n' "$matches" | grep -qx "$pid"; then
      keep=$pid
    else
      keep=$(printf '%s\n' "$matches" | head -1)
    fi
    local m
    for m in $matches; do
      [ "$m" = "$keep" ] || { kill "$m" 2>/dev/null; log "$name CULLED duplicate pid=$m (kept $keep)"; }
    done
    echo "$keep" > "$pidfile"
    return
  fi
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then return; fi
  local existing; existing=$(pgrep -f "$match" | head -1 || true)
  if [ -n "$existing" ]; then echo "$existing" > "$pidfile"; log "$name reattached pid=$existing"; return; fi
  setsid nohup $launch >>"logs/${name}.log" 2>&1 </dev/null &
  sleep 4
  local real; real=$(pgrep -f "$match" | head -1 || true)
  echo "${real:-}" > "$pidfile"
  log "$name RESTARTED pid=${real:-none}"
}

# Run the pre-registered kill test before restarting a challenger — bad campaigns must stop
# accumulating on their own instead of relying on a human to notice (codex finding 2026-07-06).
# rc=2 (kill_recommended at n>=200, CI-upper<0) -> pause: do not restart. Any other rc fails OPEN.
guard_then_ensure() {
  local name=$1 pidfile=$2 match=$3 launch=$4 db=$5 campaign=$6
  $PY scripts/crypto5m_campaign_guard.py \
    --paper-db "$db" --campaign "$campaign" \
    --min-windows 200 --interim-min-windows 60 --action pause \
    --pid "$pidfile" --marker "${db%.sqlite}.kill.json" \
    >> logs/crypto5m_campaign_guard.log 2>&1
  local rc=$?
  if [ "$rc" -eq 2 ]; then
    log "$name PAUSED by campaign guard"
    return
  fi
  if [ "$rc" -ne 0 ]; then log "$name campaign_guard rc=$rc; fail-open"; fi
  ensure "$name" "$pidfile" "$match" "$launch"
}

# forward-paper RETIRED 2026-07-04: verdict final at n=673/rule — every taker rule lost to cost
# (C0 random −19.5% ROI = the vig; C1 z<1). Data kept in btc5m_forward_v0.sqlite. To revive:
#   ensure "btc5m_forward_v0" "data/crypto5m/btc5m_forward_v0.pid" \
#     "run_polymarket_crypto5m_forward_paper.py --paper-db data/crypto5m/btc5m_forward_v0.sqlite" \
#     "$PY -u scripts/run_polymarket_crypto5m_forward_paper.py --paper-db data/crypto5m/btc5m_forward_v0.sqlite --campaign-id btc5m-forward-paper-v0 --continuous --poll-interval 5"

# CAPTURE STOPPED 2026-07-08 — the crypto5m maker fleet is fully retired (no realizable edge; the
# price_change/cancel-aware replay confirmed the whole line was a naive-fill fantasy). The capture's
# only consumers were the makers + that confirmation, both done. Heavy feed (~6M price_change rows/2.3h),
# no consumer -> stopped. The parser FIX stays (committed 69bd231; reusable if a maker is ever revived).
# Revive = uncomment.
# ensure "crypto5m_capture" "data/crypto5m_capture.pid" \
#   "run_polymarket_crypto5m_capture.py --continuous" \
#   "$PY -u scripts/run_polymarket_crypto5m_capture.py --continuous --capture-duration 300 --max-concurrent-windows 2 --db-path data/crypto5m_capture.sqlite"

# quant-shadow (research sim) STOPPED 2026-06-25 — we are LIVE PAPER TRADING only, no backtesting. To revive:
#   ensure "crypto5m_quant_shadow_v4" "data/crypto5m/btc5m_quant_shadow_v4.pid" \
#     "run_polymarket_crypto5m_quant_shadow.py --paper-db data/crypto5m/btc5m_quant_shadow_v4.sqlite" \
#     "$PY -u scripts/run_polymarket_crypto5m_quant_shadow.py --paper-db data/crypto5m/btc5m_quant_shadow_v4.sqlite --campaign-id btc5m-quant-shadow-v4 --continuous --poll-interval 5"

# scalper RETIRED 2026-07-04 (one algo only): taker −$208/503 z=−2.9; its read lives on INSIDE the
# maker (directional_pull + jump-widen). Code+tests kept. To revive:
#   ensure "crypto5m_active_trader" "data/crypto5m/active_trader_v0.pid" \
#     "run_polymarket_crypto5m_active_trader.py --paper-db data/crypto5m/active_trader_v0.sqlite" \
#     "$PY -u scripts/run_polymarket_crypto5m_active_trader.py --paper-db data/crypto5m/active_trader_v0.sqlite --continuous --poll-interval 2"

# ============================================================================
# CRYPTO5M MAKER FLEET RETIRED 2026-07-08 — NO REALIZABLE EDGE.
# Cancel-aware queue replay (price_change/cancel stream, accurate book reconstruction)
# showed the whole maker line is a naive-fill fantasy: frozen-f full realizable PnL
# = +$0.004/fill (dead zero across ALL depths, N=151); deep-only-v1 = null (trades-only
# +0.09/fill CI incl 0 at N=259, clean-middle slightly negative). The paper '+$2/w vs
# control' was entirely a front-of-queue fill artifact. All 6 campaigns (regime-v1,
# frozen-f, v33-learner, deep-only-v1, sweep-1e5, sweep-5e5) stopped + respawns disabled.
# Capture + retention below stay (data feed; reusable). Histories immutable in the DBs.
# Revival = uncomment the relevant stanza. Verdict: finding-crypto5m-floor-endogeneity-2026-07-07.
# ============================================================================
# Maker engine ENABLED 2026-07-04. Current campaign maker-regime-v1 applies the regime router actively:
# same-token print fills, exchange-time rows, quote snapshots for replay, and defensive mid-chop sizing.
# Paper only, no orders. maker-unified-v4 rows remain historical and must not pool into this campaign.
# $PY scripts/crypto5m_campaign_guard.py \
#   --paper-db data/crypto5m/maker_v0.sqlite \
#   --campaign maker-regime-v1 \
#   --min-windows 200 \
#   --interim-min-windows 60 \
#   --action pause \
#   --pid data/crypto5m/maker_v0.pid \
#   --marker data/crypto5m/maker_v0.kill.json \
#   >> logs/crypto5m_campaign_guard.log 2>&1
# guard_rc=$?
# if [ "$guard_rc" -eq 2 ]; then
#   log "crypto5m_maker PAUSED by campaign guard"
# else
#   if [ "$guard_rc" -ne 0 ]; then log "crypto5m_campaign_guard rc=$guard_rc; fail-open"; fi
#   ensure "crypto5m_maker" "data/crypto5m/maker_v0.pid" \
#     "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_v0.sqlite" \
#     "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_v0.sqlite --pid data/crypto5m/maker_v0.pid --campaign maker-regime-v1 --continuous --poll 1.0 --maker-regime-router active"
# fi

# RETIRED 2026-07-06 (v3.2 launch): adaptive V1 was the paired-worst campaign (z=-2.06 vs v3 on
# common windows) and is superseded by v2/v3.1. History immutable in maker_adaptive_v1.sqlite.
# ensure "crypto5m_adaptive_maker" "data/crypto5m/maker_adaptive_v1.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v1.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v1.sqlite --pid data/crypto5m/maker_adaptive_v1.pid --campaign maker-adaptive-paper-v1 --continuous --poll 1.0 --maker-adaptive-paper --adaptive-control-campaign maker-regime-v1 --adaptive-epsilon 0.10 --adaptive-seed 20260705"

# RETIRED 2026-07-07 (portfolio review): on the forward common set (N=137) v2 was NEGATIVE absolute
# (-0.18/w) and only beat control because control is underwater there. Its lesson (a per-fill-reward
# learner does not beat the fixed rule) is banked; history immutable in maker_adaptive_v2.sqlite.
# Revival = uncomment. Telemetry retention below still covers the DB.
# guard_then_ensure "crypto5m_adaptive_maker_v2" "data/crypto5m/maker_adaptive_v2.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v2.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v2.sqlite --pid data/crypto5m/maker_adaptive_v2.pid --campaign maker-adaptive-paper-v2 --continuous --poll 1.0 --maker-adaptive-paper --adaptive-learner-version v2 --adaptive-control-campaign maker-regime-v1 --adaptive-epsilon 0.10 --adaptive-seed 20260705" \
#   "data/crypto5m/maker_adaptive_v2.sqlite" "maker-adaptive-paper-v2"

# RETIRED 2026-07-06 (v3.2 launch): V3 is fully superseded by V3.1 (same brain, but V3's fair-blend
# and disable/promote loops were inert). History immutable in maker_adaptive_v3.sqlite.
# ensure "crypto5m_adaptive_maker_v3" "data/crypto5m/maker_adaptive_v3.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v3.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v3.sqlite --pid data/crypto5m/maker_adaptive_v3.pid --campaign maker-adaptive-paper-v3 --continuous --poll 1.0 --maker-adaptive-v3 --adaptive-control-campaign maker-adaptive-paper-v2 --adaptive-epsilon 0.10 --adaptive-seed 20260705"

# RETIRED 2026-07-07 (portfolio review): forward common set (N=137) v3.1 was NEGATIVE absolute
# (-0.08/w). The full v3 learner brain (fair-blend + disable/prefer gates) did not beat the fixed
# rule; superseded by the v3.2f/frozen/v3.3L line. History immutable in maker_adaptive_v3_1.sqlite.
# guard_then_ensure "crypto5m_adaptive_maker_v31" "data/crypto5m/maker_adaptive_v3_1.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v3_1.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v3_1.sqlite --pid data/crypto5m/maker_adaptive_v3_1.pid --campaign maker-adaptive-paper-v3.1 --continuous --poll 1.0 --maker-adaptive-v3 --adaptive-control-campaign maker-adaptive-paper-v2 --adaptive-epsilon 0.10 --adaptive-seed 20260706" \
#   "data/crypto5m/maker_adaptive_v3_1.sqlite" "maker-adaptive-paper-v3.1"

# RETIRED 2026-07-07 (portfolio review): forward common set (N=137) v3.2r was +0.12/w — the markout-
# reward lever gave a fixed learner no lift over control that frozen didn't already exceed, and its
# markout lever is inert once frozen (folded into v3.3). History immutable in maker_adaptive_v3_2r.sqlite.
# guard_then_ensure "crypto5m_adaptive_maker_v32r" "data/crypto5m/maker_adaptive_v3_2r.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v3_2r.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v3_2r.sqlite --pid data/crypto5m/maker_adaptive_v3_2r.pid --campaign maker-adaptive-paper-v3.2r --continuous --poll 1.0 --maker-adaptive-v3 --adaptive-reward-version maker-reward-v3.2 --adaptive-control-campaign maker-adaptive-paper-v3.1 --adaptive-epsilon 0.10 --adaptive-seed 20260706" \
#   "data/crypto5m/maker_adaptive_v3_2r.sqlite" "maker-adaptive-paper-v3.2r"

# V3.2f OLD LEARNER RETIRED 2026-07-07 — no lift vs frozen-f (paired -$89/272, ns; the censored-reward
# failure banked). Its lesson is captured; frozen-f is the champion. History immutable in the DB.
# guard_then_ensure "crypto5m_adaptive_maker_v32f" "data/crypto5m/maker_adaptive_v3_2f.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v3_2f.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v3_2f.sqlite --pid data/crypto5m/maker_adaptive_v3_2f.pid --campaign maker-adaptive-paper-v3.2f --continuous --poll 1.0 --maker-adaptive-v3 --vol-floor 0.00002 --adaptive-control-campaign maker-adaptive-paper-v3.1 --adaptive-epsilon 0.10 --adaptive-seed 20260706" \
#   "data/crypto5m/maker_adaptive_v3_2f.sqlite" "maker-adaptive-paper-v3.2f"

# V3.2f-FROZEN ablation ENABLED 2026-07-06 — the learner-harm test: identical to v3.2f (vol floor
# 2e-5) but the learner is held at launch priors (--adaptive-frozen: default fair blend, deterministic
# prior arm, epsilon forced 0, no learner-state updates). Pre-registration:
# docs/superpowers/specs/2026-07-06-crypto5m-learner-tournament-preregistration.md. If frozen beats
# learning-v3.2f paired at the gate, the learner is mis-learning (noise-following on ~2 rewards/context).
# guard_then_ensure "crypto5m_adaptive_maker_v32f_frozen" "data/crypto5m/maker_adaptive_v3_2f_frozen.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v3_2f_frozen.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v3_2f_frozen.sqlite --pid data/crypto5m/maker_adaptive_v3_2f_frozen.pid --campaign maker-adaptive-paper-v3.2f-frozen --continuous --poll 1.0 --maker-adaptive-v3 --vol-floor 0.00002 --adaptive-frozen --adaptive-control-campaign maker-adaptive-paper-v3.2f --adaptive-seed 20260706" \
#   "data/crypto5m/maker_adaptive_v3_2f_frozen.sqlite" "maker-adaptive-paper-v3.2f-frozen"

# V3.3-LEARNER ENABLED 2026-07-06 — the rebuilt learner racing in parallel (pre-registered
# entrant; spec Branch B built early instead of waiting for the gate). Window-level PAIRED
# reward vs frozen-f (reward = own window pnl - frozen-f same-window pnl: not-quoting through
# a window the baseline monetized is negative), frozen fair (no Brier), sufficiency-gated
# hierarchy (n>=30 decayed windows), 36h recency decay, evidence-gated deviation from the
# prior arm. Same vol floor as the f-line.
# guard_then_ensure "crypto5m_maker_v33_learner" "data/crypto5m/maker_v33_learner.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_v33_learner.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_v33_learner.sqlite --pid data/crypto5m/maker_v33_learner.pid --campaign maker-v33-learner-paper --continuous --poll 1.0 --maker-adaptive-v33 --vol-floor 0.00002 --v33-baseline-db data/crypto5m/maker_adaptive_v3_2f_frozen.sqlite --v33-baseline-campaign maker-adaptive-paper-v3.2f-frozen --adaptive-seed 20260706" \
#   "data/crypto5m/maker_v33_learner.sqlite" "maker-v33-learner-paper"

# DEEP-ONLY-V1 ENABLED 2026-07-07 — the profit-eater fix (3-way agreed: Claude+Codex review). Same frozen
# engine + vol floor 2e-5, but --deep-only-min-depth 0.03 REMOVES the top-of-book/near-touch entry bids
# (proven adverse-selection losers: near-touch bids -$141..-$291 across campaigns) and keeps ONLY the deep
# resting bids (proven stale-seller edge +$230..+$481). NOTE: this ISOLATES the edge; it does NOT prove
# live viability (deep fills are exactly where queue priority is weakest — realizability needs a live test).
# Review: docs/superpowers/specs/2026-07-07-crypto5m-maker-continuous-floor-and-learner-design.md + session.
# guard_then_ensure "crypto5m_maker_deep_only_v1" "data/crypto5m/maker_deep_only_v1.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_deep_only_v1.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_deep_only_v1.sqlite --pid data/crypto5m/maker_deep_only_v1.pid --campaign maker-deep-only-v1 --continuous --poll 1.0 --maker-adaptive-v3 --adaptive-frozen --vol-floor 0.00002 --deep-only-min-depth 0.03 --adaptive-control-campaign maker-adaptive-paper-v3.2f-frozen --adaptive-seed 20260707" \
#   "data/crypto5m/maker_deep_only_v1.sqlite" "maker-deep-only-v1"

# REGIME-GATED-V1/V2 RETIRED 2026-07-07 (same day) — the clean-feature recompute
# (docs/superpowers/specs/2026-07-07-crypto5m-floor-clean-feature-prereg.md) REFUTED the crossover
# premise these rested on. With a neutral Polymarket-book odds feature (verified independent + outcome-
# calibrated), pre-registered FDR analysis found NO ~0.20 crossover and NO wide-in-contested advantage;
# tight floor (2e-5) >= wide (1e-4) in every regime. v1/v2 only differ from frozen-f by using the wide
# floor in some windows -> dominated by construction; direct forward v1-vs-frozen was ~0 ex-best-window
# (one lucky +$17.20 window carried the headline), and v1/v2 classify on the engine fair which disagrees
# with the true market regime 41% of the time. Histories immutable in the two DBs. Revival = uncomment.
# guard_then_ensure "crypto5m_maker_regime_gated_v1" "data/crypto5m/maker_regime_gated_v1.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_regime_gated_v1.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_regime_gated_v1.sqlite --pid data/crypto5m/maker_regime_gated_v1.pid --campaign maker-regime-gated-v1 --continuous --poll 1.0 --maker-adaptive-v3 --adaptive-frozen --regime-gated-floor 0.00002 0.0001 --adaptive-control-campaign maker-adaptive-paper-v3.2f-frozen --adaptive-seed 20260707" \
#   "data/crypto5m/maker_regime_gated_v1.sqlite" "maker-regime-gated-v1"
# guard_then_ensure "crypto5m_maker_regime_gated_v2" "data/crypto5m/maker_regime_gated_v2.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_regime_gated_v2.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_regime_gated_v2.sqlite --pid data/crypto5m/maker_regime_gated_v2.pid --campaign maker-regime-gated-v2 --continuous --poll 1.0 --maker-adaptive-v3 --adaptive-frozen --regime-gated-ramp 0.00002 0.0001 0.15 0.25 --adaptive-control-campaign maker-adaptive-paper-v3.2f-frozen --adaptive-seed 20260707" \
#   "data/crypto5m/maker_regime_gated_v2.sqlite" "maker-regime-gated-v2"

# FLOOR SWEEP 2026-07-07 — fixed-floor frozen-engine campaigns to trace the dose-response the
# counterfactual limit forbids backtesting (spec §2/§6). Same engine as frozen-f, only --vol-floor
# differs. Combined with frozen-f (2e-5) these give a {1e-5,2e-5,5e-5,5e-4} curve. Research-only;
# NOT on the fleet board. Analyzed offline via scripts.
# guard_then_ensure "crypto5m_maker_sweep_1e5" "data/crypto5m/maker_sweep_1e5.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_sweep_1e5.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_sweep_1e5.sqlite --pid data/crypto5m/maker_sweep_1e5.pid --campaign maker-sweep-floor-1e5 --continuous --poll 1.0 --maker-adaptive-v3 --adaptive-frozen --vol-floor 0.00001 --adaptive-control-campaign maker-adaptive-paper-v3.2f-frozen --adaptive-seed 20260707" \
#   "data/crypto5m/maker_sweep_1e5.sqlite" "maker-sweep-floor-1e5"

# guard_then_ensure "crypto5m_maker_sweep_5e5" "data/crypto5m/maker_sweep_5e5.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_sweep_5e5.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_sweep_5e5.sqlite --pid data/crypto5m/maker_sweep_5e5.pid --campaign maker-sweep-floor-5e5 --continuous --poll 1.0 --maker-adaptive-v3 --adaptive-frozen --vol-floor 0.00005 --adaptive-control-campaign maker-adaptive-paper-v3.2f-frozen --adaptive-seed 20260707" \
#   "data/crypto5m/maker_sweep_5e5.sqlite" "maker-sweep-floor-5e5"

# SWEEP 5e-4 RETIRED 2026-07-07 — decisively worse than 2e-5 (paired -$143/48, CI[-4.83,-1.18]).
# The widest floor is bad; 1e-5/5e-5 keep running to finish the floor curve (2e-5 looks optimal).
# guard_then_ensure "crypto5m_maker_sweep_5e4" "data/crypto5m/maker_sweep_5e4.pid" \
#   "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_sweep_5e4.sqlite" \
#   "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_sweep_5e4.sqlite --pid data/crypto5m/maker_sweep_5e4.pid --campaign maker-sweep-floor-5e4 --continuous --poll 1.0 --maker-adaptive-v3 --adaptive-frozen --vol-floor 0.0005 --adaptive-control-campaign maker-adaptive-paper-v3.2f-frozen --adaptive-seed 20260707" \
#   "data/crypto5m/maker_sweep_5e4.sqlite" "maker-sweep-floor-5e4"

# RETENTION: the capture tape is a live order-flow feed for the engine (no backtest hoard) — book/quote
# spam kept ~2h. EXCEPTION (2026-07-06): last_trade_price PRINTS kept 72h — they are the exogenous
# real-time flow measure needed to validate the flow-scaled-participation lever offline (v3.3 draft
# spec) before building it; ~30k rows/h, cheap. Cutoff derived from the data's OWN latest timestamp
# (NOT `date`) because the host clock is skewed +7h while exchange_ts is real exchange time.
$PY - <<'PRUNE' >/dev/null 2>&1 || true
import sqlite3
c = sqlite3.connect("data/crypto5m_capture.sqlite", timeout=10)
mx = c.execute("SELECT MAX(exchange_ts) FROM crypto5m_book_events").fetchone()[0]
if mx:
    c.execute("DELETE FROM crypto5m_book_events WHERE exchange_ts < ? AND COALESCE(event_type,'') != 'last_trade_price'",
              (mx - 2 * 3600 * 1000,))
    c.execute("DELETE FROM crypto5m_book_events WHERE exchange_ts < ? AND event_type = 'last_trade_price'",
              (mx - 72 * 3600 * 1000,))
    c.commit()
c.close()
PRUNE

# Maker telemetry retention: maker_quotes appends ~10k rows/hour at 1s poll and maker_regime_decisions
# ~2k/hour — unbounded they add ~75MB/day. Keep 7 days (plenty for regime-eval replay jobs; fills and
# windows — the actual eval data — are never pruned). Same data-derived cutoff rule as above: these
# tables carry exchange-time seconds, and the host wall clock is +7h skewed.
$PY - <<'PRUNE' >/dev/null 2>&1 || true
import os
import sqlite3
# ALL campaign DBs, not just the control — the adaptive DBs append the same telemetry at the same
# rate and were growing unbounded (v2 hit 194MB in ~1.5 days; codex finding 2026-07-06).
DBS = [
    "data/crypto5m/maker_v0.sqlite",
    "data/crypto5m/maker_adaptive_v1.sqlite",
    "data/crypto5m/maker_adaptive_v2.sqlite",
    "data/crypto5m/maker_adaptive_v3.sqlite",
    "data/crypto5m/maker_adaptive_v3_1.sqlite",
    "data/crypto5m/maker_adaptive_v3_2r.sqlite",
    "data/crypto5m/maker_adaptive_v3_2f.sqlite",
    "data/crypto5m/maker_adaptive_v3_2f_frozen.sqlite",
    "data/crypto5m/maker_v33_learner.sqlite",
    "data/crypto5m/maker_regime_gated_v1.sqlite",
    "data/crypto5m/maker_regime_gated_v2.sqlite",
    "data/crypto5m/maker_sweep_1e5.sqlite",
    "data/crypto5m/maker_sweep_5e5.sqlite",
    "data/crypto5m/maker_sweep_5e4.sqlite",
    "data/crypto5m/maker_deep_only_v1.sqlite",
]
for db in DBS:
    if not os.path.exists(db):
        continue
    c = sqlite3.connect(db, timeout=10)
    for t in ("maker_quotes", "maker_regime_decisions"):
        try:
            mx = c.execute(f"SELECT MAX(ts) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if mx:
            c.execute(f"DELETE FROM {t} WHERE ts < ?", (mx - 7 * 86400,))
    c.commit()
    # The web UI polls these DBs every few seconds; overlapping readers starve SQLite's
    # auto-checkpoint and the WAL grows unbounded (maker_v0 hit 288MB on 2026-07-07,
    # slowing every read past the UI timeout). TRUNCATE when it succeeds, else the
    # PASSIVE fallback still copies frames so the next quiet moment can truncate.
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass
    c.close()
PRUNE
