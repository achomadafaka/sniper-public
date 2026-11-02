#!/usr/bin/env python3
"""memecoin_runner_loop.py — sustained fomo copy-mirror poll loop (paper).

Loads credentials from .fomo.env, polls fomo /feed on an interval, and drives
the copy engine against curated traders. Writes to the same SQLite DB the web
server reads (WAL mode — no blocking). Proactively refreshes the Privy token.

Run:  python3 scripts/memecoin_runner_loop.py
Stop: Ctrl-C (or pkill -f memecoin_runner_loop)

Env (via .fomo.env or shell):
  FOMO_REFRESH_TOKEN / FOMO_ACCESS_TOKEN / FOMO_CA_ID  — Privy credentials
  MEMECOIN_POLL_MS      — poll interval (default 1000ms; clamped to a 1000ms WAF floor)
  MEMECOIN_FOLLOW_SLOW_ENABLED — watch-only 300s follow lane (default 0 = killed; see settings)
  MEMECOIN_COPY_SIZE_USD — fixed paper size per entry (default 100)
"""
import math
import os
import faulthandler
import queue
import signal
import sys
import threading
import time
import traceback
import zlib
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if __name__ == "__main__":
    # One write() per print() (sniper/util/line_print.py): before any other sniper import.
    from sniper.util.line_print import install
    install()

# load BOTH .env and .fomo.env (does not override real shell env). One loader shared with the smoke
# scripts so the real-money arm flag + creds are found wherever the operator puts them — putting them
# in .env used to strand the arm silently (runner booted paper-only).
from sniper.util.envfiles import load_env_files
from sniper.util.state_paths import configure_memecoin_state_env, state_root
_STATE_ROOT = str(state_root())
load_env_files(_STATE_ROOT)
configure_memecoin_state_env()

from sniper.persistence.db_core import get_connection, get_readonly_connection
from sniper.persistence import db_memecoin as m
from sniper.feeds.fomo_feed import FomoAuth
from sniper.services.memecoin_runner import (
    run_once, write_heartbeat, bracket_pass, follow_sync_pass, acquire_run_lock,
    start_paper_bracket, paper_bracket_thread_enabled, paper_bracket_status,
    classify_fomo_error, handle_auth_page, refresh_due, publish_sweep, refresh_idle_heartbeat,
)
from sniper.services.memecoin_submission_status import write_submission_health
from sniper.services.memecoin_real_runner import (
    build_all_contexts, context_for, make_real_hook, make_confirm_for, make_confirm_proof_for,
    make_confirmed_nonce_for,
    make_pending_nonce_for, make_proof_snapshot_for,
    make_token_balance_for,
    make_real_buy_fn, make_venue_exit_fn, make_venue_partial_exit_fn, make_venue_source_partial_exit_fn,
    recover_source_partials, check_wallet_demotion, exit_sweep_timing,
    settle_derisk_for_manual_close, RailDeriskSettler, persist_hot_controller_state, real_exit_sweep, lp_rail_status, real_reconcile, reconcile_scopes,
    check_kill_switch_alert, check_daily_loss_alert, check_gas_alert,
    recover_stale_reservations, rpc_pool_status_for_contexts, tpu_status_for_contexts,
    solana_volume_status_for_contexts,
    start_lifi_recovery_worker, lifi_alert_baseline_ready, lifi_status_for_contexts,
)
from sniper.execution.solana_quote import dexscreener_stats
# Module scope on purpose: BOTH the fast-lane worker and the main loop's bracket gate read this.
# It was function-local to _fastlane_worker, so the bracket gate raised NameError every cycle.
from sniper.settings.memecoin import MEMECOIN_FASTLANE_DECIDES

from sniper.settings.memecoin import MEMECOIN_POLL_MS as POLL_MS   # single source; WAF floor 1000ms
SIZE_USD = float(os.getenv("MEMECOIN_COPY_SIZE_USD", "100"))
# HOW OFTEN THE V4 POOLKEY SEED READS THE TAPE (2026-09-24). The tape registers a pool from its
# `Initialize` event, before the first swap, so this only has to be shorter than the window a
# freshly graduated coin trades in. Measured cost of one pass on the live store: 0.15 ms to open it
# read-only and 133.6 ms for the 109 rows of a twelve-minute span, and the cursor keeps a steady
# pass to a handful of rows.
_V4_KEY_SEED_PERIOD_S = 10.0


def connect_observer_main(db_path=None):
    """Main-ledger reader for long-lived observation threads; never pins WAL while idle."""
    return get_readonly_connection(db_path)


def wait_for_idle_heartbeat(rest_s, *, provider, funded_only, wait_fn, heartbeat_fn,
                            clock=time.monotonic):
    """Keep the work deadline/J7 wake; publish only during Shyft funded-only idle time."""
    if provider != 'shyft' or not funded_only:
        return bool(wait_fn(rest_s))
    started = clock()
    deadline, next_tick = started + max(0.0, rest_s), started + 1.0
    while True:
        now = clock()
        if now >= deadline:
            return False
        if wait_fn(min(deadline, next_tick) - now):
            return True  # A queued relay gets the work cycle before any publication.
        if clock() >= deadline:
            return False
        try:
            heartbeat_fn()
        except Exception:
            pass  # Publication is best-effort; never extend/reset the work deadline.
        next_tick = started + math.floor(clock() - started) + 1.0


def _p(msg):
    # timestamped stdout (host clock, matching the watchdog log's `date -u`) so a death can be
    # attributed to a time — the log was previously un-timestamped, making 5x/day deaths opaque.
    print("%s %s" % (datetime.now(timezone.utc).isoformat(), msg), flush=True)


def enqueue_fomo_activity(queues, payload, *, received_at=None, log_fn=_p):
    """Put one socket payload on a wallet-stable worker queue."""
    if not queues:
        raise ValueError("at least one Fomo activity queue is required")
    wallet = str((payload or {}).get("userId") or "")
    index = zlib.crc32(wallet.encode("utf-8")) % len(queues)
    try:
        queues[index].put_nowait((payload, received_at))
    except queue.Full:
        log_fn("[warn] Fomo activity queue full; socket receive waits to preserve the signal")
        queues[index].put((payload, received_at))
    return index


def register_thread_dump(file=None):
    """Expose a non-terminating operator thread dump on SIGUSR2 for live pin diagnosis."""
    faulthandler.register(signal.SIGUSR2, file=file or sys.stderr, all_threads=True)


def register_death_logger(file=None, log_fn=_p):
    """Name the thing that ends the runner.

    The runner installed no SIGTERM handler and never enabled faulthandler, so BOTH death classes
    exited without a single line: a plain signal terminates Python silently, and a native crash in
    an extension prints nothing. The watchdog could then only ever report "pinned runner not
    running". On 2026-08-23 that hid 13 deaths at random cycle counts (7907, 169, 1477, 97, 103,
    37, 109, 25, 55, 127) with no OOM, no disk pressure, and no watchdog kill to explain them.

    faulthandler covers the crash class (SIGSEGV/SIGBUS/SIGFPE/SIGABRT/SIGILL -> traceback); the
    handler covers the signal class. Both write to the runner log. Neither changes behavior: the
    handler restores the default disposition and re-raises, so the exit status the parent sees is
    unchanged.
    """
    faulthandler.enable(file=file or sys.stderr, all_threads=True)

    def _log_then_die(signum, _frame):
        try:
            log_fn("[memecoin] runner received %s — exiting" % signal.Signals(signum).name)
        finally:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(_sig, _log_then_die)


def finish_db_cycle(conn, *, failed=False, log_fn=_p):
    """End one long-lived worker iteration before it sleeps or performs more network I/O."""
    try:
        if failed:
            conn.rollback()
        else:
            conn.commit()
        return True
    except Exception:
        if not failed:
            try:
                conn.rollback()
            except Exception:
                pass
        log_fn("[err] DB cycle %s failed:\n%s"
               % ("rollback" if failed else "commit", traceback.format_exc()))
        return False


def follow_sync_without_snapshot(conn, auth, *, pass_fn=follow_sync_pass):
    """Never carry the runner's prior SQLite work into the Fomo graph HTTP sweep."""
    conn.commit()
    return pass_fn(conn, auth)


def first_cycle_line(phases, published_offset_s):
    """The first cycle's phase seconds and when its exit sweep reached the heartbeat.

    Once per launch (2026-09-27). A deploy's entry gate waits on this cycle's exit sweep, and the
    periodic `cycles=` report cannot show it: its totals start accumulating after this cycle, so
    cycle 1 always printed `phase{}`. Measurement only."""
    return "[memecoin] first cycle: %s published=%s" % (
        " ".join("%s=%.3f" % (k, float(phases[k])) for k in sorted(phases)),
        "no" if published_offset_s is None else "%.3f" % published_offset_s)


def finish_bracket_cycle(candidate_engine, cycle, *, now_fn, log_fn=_p):
    if candidate_engine is not None:
        try:
            candidate_engine.sweep(now_fn())
        except Exception:
            log_fn("[err] strategy v0 paper sweep failed:\n" + traceback.format_exc())
    return cycle


# Follow-sync observability state (2026-07-18). A single skip is transient and fail-closed (the next
# ~5min cycle retries) — but a healthy no-op logged NOTHING, so a long healthy stretch was
# indistinguishable from a dead sync, and a persistent outage looked like the same lone WARN as a
# blip. That misread a ~3h 07-16 Fomo outage + made the sync look dead for 2 days. Track consecutive
# skips to escalate a real outage, and emit a throttled health heartbeat so "healthy" is visible.
_follow_sync_state = {"consecutive_skips": 0, "healthy_runs": 0}
_FOLLOW_SYNC_SKIP_ALERT_AFTER = 6      # ~30min at the 5min cadence -> a real, persistent outage
_FOLLOW_SYNC_HEARTBEAT_EVERY = 12      # ~hourly: prove the sync is alive + the invariant holds


def report_follow_sync(result, *, log_fn=_p, state=None):
    """Surface armed-follow repair + sync HEALTH without leaking auth or response payloads."""
    state = _follow_sync_state if state is None else state
    result = result or {}
    if result.get("skipped"):
        state["consecutive_skips"] = int(state.get("consecutive_skips", 0)) + 1
        n = state["consecutive_skips"]
        reason = result.get("reason") or "unknown"
        if n >= _FOLLOW_SYNC_SKIP_ALERT_AFTER:
            log_fn("[err] Fomo follow sync DOWN %d consecutive cycles (~%dmin): %s — armed-follow "
                   "invariant NOT being maintained" % (n, n * 5, reason))
        else:
            log_fn("[warn] Fomo follow sync skipped: %s (%d in a row)" % (reason, n))
        return
    # healthy pass: note recovery from a skip streak, then repair/failure, then a throttled heartbeat.
    if state.get("consecutive_skips", 0) >= 3:
        log_fn("[memecoin] Fomo follow sync RECOVERED after %d skipped cycle(s)"
               % state["consecutive_skips"])
    state["consecutive_skips"] = 0
    repaired = int(result.get("armed_followed") or 0)
    already = int(result.get("armed_already_followed") or 0)
    if repaired:
        log_fn("[memecoin] Fomo follow repair: followed %d missing armed wallet(s) "
               "(%d already followed)" % (repaired, already))
    failed = int(result.get("armed_follow_failed") or 0)
    if failed:
        ids = ",".join(
            str(uid).replace("\n", "").replace("\r", "")[:80]
            for uid in (result.get("armed_follow_failures") or [])
        )
        log_fn("[warn] Fomo follow repair failed: count=%d ids=%s" % (failed, ids))
    if not repaired and not failed:
        state["healthy_runs"] = int(state.get("healthy_runs", 0)) + 1
        if state["healthy_runs"] % _FOLLOW_SYNC_HEARTBEAT_EVERY == 1:
            log_fn("[memecoin] Fomo follow sync ok: %d armed wallet(s) followed, invariant holds"
                   % already)


def start_execution_measurement(contexts, *, journals=(), status=None, stop=None):
    """Public-key-only independent observer; startup/config failures never reach entry."""
    from sniper.settings import memecoin as cfg
    status = status if status is not None else {}
    if not cfg.MEMECOIN_EXECUTION_OBSERVER_ENABLED:
        status.update(state='disabled')
        return None
    context = (contexts or {}).get('solana') or {}
    pubkey = context.get('pubkey')
    if pubkey is None:
        status.update(state='unknown', reason='public_context_unavailable')
        return None
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    from sniper.services.memecoin_solana_swap_marks import ws_url_from_read_rpc
    from uuid import uuid4
    source = next(iter(journals), None)
    if source is not None:
        boot_id, domain = source.boot_id, source.clock_domain
    else:
        from pathlib import Path
        try:
            boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:
            boot_id = 'unknown-boot:' + str(uuid4())
        domain = 'process-monotonic:' + str(os.getpid())
    journal = ReceiptJournal(producer_run_id='observer:' + str(uuid4()),
                             boot_id=boot_id, clock_domain=domain)
    status.update(state='starting')
    try:
        from sniper.services.memecoin_solana_provider import infra_provider
        if infra_provider() == 'shyft':
            from sniper.services.memecoin_shyft_signal import live_config
            token, urls = live_config()
            url = next((u for u in urls if 'grpc.fra.shyft.to' in u), urls[0])
            from sniper.services.memecoin_execution_observer import start_yellowstone_execution_observer
            start_yellowstone_execution_observer(custody_pubkey=str(pubkey), url=url, token=token,
                journal=journal, status=status, stop=stop if stop is not None else threading.Event())
        else:
            url = os.getenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL', '').strip()
            custody = (cfg.MEMECOIN_SOLANA_RPC_URL, *cfg.MEMECOIN_SOLANA_RPC_POOL_URLS)
            if not url or url.rstrip('/') in {value.rstrip('/') for value in custody if value}:
                raise ValueError('independent_research_url_unavailable')
            ws_url = ws_url_from_read_rpc(url)
            if not ws_url or not ws_url.startswith('wss://'):
                raise ValueError('invalid_research_ws_url')
            from sniper.services.memecoin_execution_observer import start_execution_observer
            start_execution_observer(custody_pubkey=str(pubkey), ws_url=ws_url,
                journal=journal, status=status, stop=stop if stop is not None else threading.Event())
    except Exception as exc:
        reason = str(exc) if str(exc) in ('independent_research_url_unavailable',
            'invalid_research_ws_url') else 'observer_start_failed'
        status.update(state='unknown', reason=reason)
        journal.record_gap(component='processed', reason=reason, connection_generation=0,
                           mono_start=None, mono_end=None, lost_count=None)
    return journal


def report_evm_context_gaps(contexts, *, allowlist=None, log_fn=_p, alert_fn=None):
    """One startup diagnostic; never runs in the per-event hot path."""
    from sniper.services.memecoin_real_runner import missing_evm_context_chains
    gaps = missing_evm_context_chains(contexts, allowlist=allowlist)
    if not gaps:
        return []
    log_fn("[warn] funded EVM config gap: no context for allowlisted chain(s) %s"
           % ",".join(gaps))
    if alert_fn is None:
        from sniper.services.memecoin_alerts import alert_config_gap as alert_fn
    alert_fn(gaps)
    return gaps


def run_evm_gas_topup_pass(conn, *, contexts, now_iso, topup_fn=None, gas_alert_state=None):
    """Run the EVM gas maintenance pass (WETH <-> native gas) on each MEMECOIN_EVM_GAS_TOPUP_CHAINS
    chain, in order, on this one thread. With `gas_alert_state`, each chain's own result then feeds
    the EVM GAS-LOW / GAS-OK page. One chain (the default, 4663) returns its result unchanged;
    several return {"state": <chain:state of each non-quiet chain>, "chains": {...}}.
    ponytail: chains run in series, so a slow read on one delays the next chain's pass by up to
    its read budget; a thread per chain if that ever shows in the pass times."""
    from sniper.services.memecoin_evm_gas_topup import QUIET_STATES
    from sniper.settings import memecoin as _mc
    chains = _mc.memecoin_evm_gas_topup_chains()
    results = {chain: _run_chain_gas_pass(conn, chain, contexts=contexts, now_iso=now_iso,
                                          topup_fn=topup_fn, gas_alert_state=gas_alert_state)
               for chain in chains}
    if len(results) == 1:
        return next(iter(results.values()))
    loud = {c: r for c, r in results.items() if r.get("state") not in QUIET_STATES}
    return {"state": ",".join("%s:%s" % (c, r.get("state")) for c, r in loud.items())
            or "above_low_water", "chains": loud}


def _run_chain_gas_pass(conn, chain, *, contexts, now_iso, topup_fn, gas_alert_state):
    context = (contexts or {}).get(chain)
    if not context:
        return {"state": "context_unavailable", "chain_id": chain}
    from sniper.services.memecoin_evm_gas_topup import (
        LEGACY_GAS_PASS_CHAIN, gas_pass_bounds, run_evm_gas_topup_pass as _topup)
    from sniper.settings import memecoin as _mc
    float_state = None
    if chain != LEGACY_GAS_PASS_CHAIN:
        if chain == "1":
            levels = _mc.evm_gas_topup_levels_for(chain)
            if levels is None:
                return {"state": "levels_unset", "chain_id": chain}
            bounds = dict(low_water_wei=levels["low_wei"], target_wei=levels["target_wei"],
                          min_weth_reserve_wei=levels["min_weth_reserve_wei"],
                          daily_cap_wei=levels["daily_cap_wei"], hold_under_reservation=True,
                          hold_until_receipt_s=float(_mc.MEMECOIN_EVM_GAS_TOPUP_HOLD_S))
            if chain in _mc.memecoin_evm_native_spendable_chains():
                bounds["balance_bound_base"] = dict(bounds, native_spendable=True)
        else:
            bounds = gas_pass_bounds(chain)
        if chain == "8453" and chain in _mc.memecoin_evm_native_float_chains():
            levels = _mc.evm_gas_topup_levels_for(chain)
            if levels is None:
                float_state = "levels_unset"
            else:
                base_bounds = bounds
                bounds = dict(bounds, low_water_wei=max(bounds["low_water_wei"], levels["low_wei"]),
                              target_wei=max(bounds["target_wei"], levels["target_wei"]),
                              min_weth_reserve_wei=levels["min_weth_reserve_wei"],
                              daily_cap_wei=levels["daily_cap_wei"], balance_bound_base=base_bounds)
        if chain == "56":                     # the BSC lane's native float, only while it is live
            from sniper.services.memecoin_bsc_fast_lane import lane_float_bounds
            base_bounds = bounds
            bounds = lane_float_bounds(bounds)
            if bounds["target_wei"] > base_bounds["target_wei"]:
                bounds = dict(bounds, balance_bound_base=base_bounds)
        if chain == "8453":                   # the Base lane's native float, only while it is live
            from sniper.services.memecoin_bsc_fast_lane import lane_float_bounds
            # Capped against the gas-only bounds, as the 8453 native float already is.
            base_bounds = bounds.get("balance_bound_base") or bounds
            raised = lane_float_bounds(
                {k: v for k, v in bounds.items() if k != "balance_bound_base"},
                mode=_mc.memecoin_base_fast_lane_mode(), float_wei=_mc.MEMECOIN_BASE_FAST_LANE_FLOAT_WEI,
                refill_below_wei=0)
            if raised["target_wei"] > bounds["target_wei"]:
                bounds = dict(raised, balance_bound_base=base_bounds)
        result = (topup_fn or _topup)(conn, context=context, now_iso=now_iso, **bounds)
        if float_state:
            result = dict(result, native_float_state=float_state)
        _check_evm_gas_alert(conn, result, gas_alert_state, now_iso)
        return result
    from sniper.settings import memecoin as _mc
    from sniper.settings.memecoin import (
        MEMECOIN_EVM_GAS_TOPUP_DAILY_CAP_WEI,
        MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_WEI,
    )
    # NATIVE FLOAT REFILL (2026-09-28): on a float chain, refill below the refill mark instead of
    # LOW. Outside LOW < mark < TARGET it is ignored: at or above TARGET the planner would refuse
    # every top-up and starve exits of gas.
    low_water_wei = _mc.MEMECOIN_EVM_GAS_TOPUP_LOW_WEI
    refill_wei = getattr(_mc, "MEMECOIN_EVM_NATIVE_FLOAT_REFILL_BELOW_WEI", 0)
    if ("4663" in _mc.memecoin_evm_native_float_chains()
            and low_water_wei < refill_wei < _mc.MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI):
        low_water_wei = refill_wei
    # THE FLOAT TAKES AT MOST HALF THE LIQUID CAPITAL (2026-10-15, Alpha (B)). The 0.08 ETH target
    # held ~90% of a ~$212 book as native, and the gate sizes 4663 on WETH alone, so the clip was
    # the $21 left in WETH. The 56/8453 cap (gas need plus half of native + WETH) keeps a full clip
    # in WETH for every route while native routes still skip the unwrap. The capped pass wraps as
    # well as unwraps, so it also takes the 1/56/8453 holds (Gamma HOLD): no move while a 4663
    # reservation is unsettled, and the wallet lock is kept until its own receipt.
    bound_kw = ({"balance_bound_base": gas_pass_bounds(chain), "hold_under_reservation": True,
                 "hold_until_receipt_s": float(_mc.MEMECOIN_EVM_GAS_TOPUP_HOLD_S)}
                if chain in _mc.memecoin_evm_float_liquid_cap_chains() else {})
    result = (topup_fn or _topup)(
        conn, context=context, now_iso=now_iso,
        low_water_wei=low_water_wei,
        target_wei=_mc.MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI,
        min_weth_reserve_wei=MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_WEI,
        daily_cap_wei=MEMECOIN_EVM_GAS_TOPUP_DAILY_CAP_WEI,
        **bound_kw,
    )
    _check_evm_gas_alert(conn, result, gas_alert_state, now_iso)
    return result


def _check_evm_gas_alert(conn, result, gas_alert_state, now_iso):
    if gas_alert_state is not None:
        try:
            from datetime import datetime as _dt
            from sniper.services import memecoin_real_runner as _rr
            # The pass's own corrected-UTC instant: attempts are stamped on that clock, and the
            # host clock has run seconds off it, which would skip refusals at the cursor.
            now_ts = _dt.fromisoformat(str(now_iso).replace("Z", "+00:00")).timestamp()
            _rr.check_evm_gas_alert(conn, result, gas_alert_state, now_ts=now_ts)
        except Exception:
            _p("[err] EVM gas alert failed:\n" + traceback.format_exc())


def report_copy_arm_policy(*, enabled, log_fn=_p):
    """State the per-wallet copy-arm policy ONCE at startup, so activation is verifiable.

    The flag is read at import (like every setting), so the value that matters is the one present
    when THIS process started — and until now nothing emitted it. `/proc/<pid>/environ` cannot
    answer it either, because the runner loads `.env` itself rather than inheriting it, so an
    operator who set the flag after a deploy had no way to tell the runner was still using band
    rails. That is the same silent-strand class as the real-money arm flag (see the load_env_files
    note at the top of this module) and as MEMECOIN_POLL_MS (2026-08-03).
    """
    log_fn("[memecoin] per-wallet copy-arm exit policy: %s"
           % ("ON — a source wallet's best paper arm supplies funded exit rails"
              if enabled else "OFF — every funded entry uses its coin-band default rails"))
    return bool(enabled)


def report_paper_arms(arms, *, log_fn=_p):
    """State which paper arms this process loaded, ONCE at startup.

    Same silent-strand class as the copy-arm policy above, and with a shorter fuse. `MEMECOIN_ARMS`
    is read at import from the environment the runner loads itself, so `/proc/<pid>/environ` cannot
    answer it and an operator who edits `.env` after a deploy cannot tell which arm the running
    process actually holds. The value changed twice in two days (`real_shadow` to `funded_clone` on
    2026-09-24) and every change silently redefines what a paper row means, because the arm decides
    which exit rails, fill and sizing the row was produced under.

    An empty list is the case worth stating loudest: it means no paper arm opens a position at all,
    which in the ledger is indistinguishable from an arm that simply found no signal.
    """
    names = [str(a) for a in (arms or []) if a]
    log_fn("[memecoin] paper arms: %s"
           % (", ".join(names) if names else "NONE — no paper arm opens positions"))
    return names


def funded_authority_counts(conn, contexts):
    """Return the approved wallet count for each active funded chain."""
    from sniper.persistence.db_memecoin import real_approved_user_ids

    chains = sorted(
        (str(chain) for chain in (contexts or {})),
        key=lambda chain: (chain != "solana", len(chain), chain),
    )
    return {
        chain: len(real_approved_user_ids(conn, network_id=chain))
        for chain in chains
    }


REFRESH_EVERY_S = 45 * 60  # proactive Privy refresh well under the ~1h expiry
# Also refresh whenever the token itself is this close to death, regardless of the interval.
# `last_refresh` is seeded at process start, so a runner inheriting an already-old token would
# otherwise wait the full interval while the token expires under it — and once it expires Privy
# will not mint a new app token at all (2026-08-03 deadlock, needed a browser re-login).
REFRESH_MARGIN_S = 15 * 60
# Privy re-issues the SAME token until it is near expiry, so a refresh inside the margin
# can legitimately be a no-op. Retry on this cadence rather than every poll: each attempt
# rotates the single-use refresh token, and spinning risks losing the chain.
REFRESH_RETRY_BACKOFF_S = 60.0
ACTIVITY_PERIOD_S = float(os.getenv("MEMECOIN_FOLLOW_ACTIVITY_PERIOD_S", "300"))  # per-follow coverage poll cadence
# Blind-price cadence for DexScreener-blind EVM chains (4663). Slower than the 15s dex sweep; the
# goal is a usable price series for exit-policy fitting, not a real-time tape. 0 = off.
EVM_PRICE_PERIOD_S = float(os.getenv("MEMECOIN_EVM_PRICE_PERIOD_S", "60"))



def flush_j7_receipts(feed, state, *, now_fn=time.monotonic, interval_s=5.0, log_fn=_p):
    """Start one receipt write after funded work, at most once per interval.

    The feed performs database work on its daemon thread. Feed status retains loss counters.
    This helper counts thread-start failures and returns without a database wait.
    """
    if feed is None:
        return 0
    now = now_fn()
    if now - float(state.get("at") or 0.0) < float(interval_s):
        return 0
    state["at"] = now
    try:
        return feed.flush_receipts()
    except Exception as exc:  # noqa: BLE001 - a measurement must never reach the loop
        state["failed"] = int(state.get("failed") or 0) + 1
        if state["failed"] in (1, 10, 100):
            log_fn("[memecoin] j7 receipt flush failed (%d): %s"
                   % (state["failed"], str(exc)[:160]))
        return 0


_J7_RECEIPT_FLUSH = {"at": 0.0}


def _j7_feed_or_none_cached():
    """The live J7 feed singleton, or None. Never constructs one: the runner already started it."""
    try:
        from sniper.feeds import j7_feed
        return j7_feed._SINGLETON
    except Exception:  # noqa: BLE001 - the flush is optional
        return None


def evm_price_due(*, cycle, last_run, period_s):
    """Whether the slow blind-price pass should run. A non-positive period disables it."""
    return float(period_s) > 0 and float(cycle) - float(last_run) > float(period_s)


def start_operator_digest_worker(
        *, enabled, period_s, worker_fn, thread_factory, log_fn=None):
    """Start the digest off-thread; never call ``worker_fn`` from the funded loop."""
    try:
        period_s = float(period_s)
    except (TypeError, ValueError):
        return False
    if not enabled or not math.isfinite(period_s) or period_s <= 0:
        return False
    try:
        thread_factory(
            target=worker_fn,
            name="memecoin-operator-digest",
            daemon=True,
        ).start()
    except Exception as exc:
        if log_fn is not None:
            log_fn("[warn] operator digest disabled after thread startup failure: %s" % exc)
        return False
    return True


_POSITION_POOL_RPC = os.environ.get("MEMECOIN_POSITION_POOL_RPC",
                                    "https://robinhood-rpc.publicnode.com")


def _resolve_position_pools_tick(mconn, tc, *, chain="4663", limit=2):
    """One bounded fill-anchored position->pool resolution pass. Never raises.

    Mint-keyed resolution answers for 1 of 25 live funded positions; the ENTRY RECEIPT names the
    pool by construction, so this fills a position-keyed store the resolver reads when the mint
    cannot say. Runs at the END of the poll iteration deliberately: the RPC must never sit between
    a signal arriving and an order going out.

    `limit=2` bounds one iteration. The */2 cron reconciler passes 25 and sweeps whatever this
    misses — a hook that fails silently is a gap nobody sees, so it is not the only writer.

    Uses the FREE endpoint, not custody: this is bookkeeping, not funded work, and it must not
    spend the quota the exit quotes depend on. Already-resolved positions cost no receipt fetch.
    """
    try:
        import sniper.execution.evm_v4_quote as Q
        from sniper.services.memecoin_position_pool_worker import resolve_pending_position_pools
        # Imported HERE, not relied on from module scope: main() imports real_utc_now_iso into its
        # own locals, so a module-level reference raises NameError — which the except below would
        # swallow, leaving the hook permanently dead and silent. Caught pre-ship, 2026-09-02.
        from sniper.util.clock import real_utc_now_iso
        rpc = _POSITION_POOL_RPC
        return resolve_pending_position_pools(
            mconn, tc, chain,
            receipt_fn=lambda tx: (Q._rpc(rpc, "eth_getTransactionReceipt", [tx])
                                   or {}).get("result") or {},
            key_fn=lambda pool_id: Q._pool_key(chain, pool_id, rpc),
            now_iso=real_utc_now_iso(), limit=limit)
    except Exception:
        # The worker itself never raises; this guards the imports and the RPC seam. A resolution
        # failure must never take down the loop that runs the exit sweep.
        return None


def start_paper_workers(*, funded_only, period_s, connect, now_fn, log_fn=_p):
    """Choose paper workers once, before any funded dispatch thread starts."""
    from sniper.services import memecoin_paper_ticks
    from sniper.services import memecoin_paper_fanout as _paper_fanout_mod
    if funded_only:
        try:
            memecoin_paper_ticks.start_exit_decision_writer(connect=connect)
        except Exception as exc:
            log_fn("[warn] funded decision mark writer NOT started (%s: %s)"
                   % (type(exc).__name__, exc))
        log_fn("[memecoin] funded only: paper workers and research capture disabled")
        return None
    try:
        from sniper.settings import memecoin as cfg
        memecoin_paper_ticks.start(now_fn=now_fn)
        log_fn("[memecoin] paper exit ticks: engine started (new study-arm entries: %s)"
               % cfg.MEMECOIN_PAPER_EXIT_ENGINE)
    except Exception as exc:
        log_fn("[memecoin] paper exit ticks: engine NOT started (%s: %s); v1 positions wait for the "
               "14-day backstop" % (type(exc).__name__, exc))
    if _paper_fanout_mod.enabled():
        _paper_fanout_mod.install(_paper_fanout_mod.PaperFanout(
            connect=connect, now_iso_fn=now_fn))
        _paper_fanout_mod.active().ensure_started()
        log_fn("[memecoin] paper fan-out: own thread started (queue %d, late after %.0fs)"
               % (_paper_fanout_mod.MAX_QUEUE, _paper_fanout_mod.MAX_LAG_S))
    from sniper.services.memecoin_research_capture import ResearchCaptureWorker
    capture = ResearchCaptureWorker(period_s=period_s, connect=connect, now_iso=now_fn, log=log_fn)
    threading.Thread(target=capture.run_forever, name="research-capture", daemon=True).start()
    log_fn("[memecoin] research capture: own thread started (period %.0fs)" % period_s)
    return capture


def main():
    # single-instance lock FIRST — a second runner would double-submit real swaps. Keep the
    # handle for the process lifetime (exiting releases it). Watchdog + a manual launch racing is
    # documented history; the flock makes a double-runner impossible, not just unlikely.
    _lock = acquire_run_lock(os.getenv(
        "MEMECOIN_RUN_LOCK", str(state_root() / "data" / "memecoin_runner.lock")))
    if _lock is None:
        sys.exit("another memecoin runner holds the lock — refusing to start (double-runner = double-submit)")
    register_thread_dump()
    register_death_logger()
    conn = get_connection()
    m.ensure_memecoin_tables(conn)
    from sniper.settings.memecoin import MEMECOIN_FUNDED_ONLY as _FUNDED_ONLY
    from sniper.services.memecoin_solana_provider import infra_provider
    _infra_provider = infra_provider()
    auth = FomoAuth()
    from sniper.settings.memecoin import MEMECOIN_SIGNAL_SOURCE as _SIGNAL_SOURCE
    _J7_SOURCE = _SIGNAL_SOURCE == "j7"
    if _J7_SOURCE:
        # J7Tracker relays the tracked fomo roster over its wallet-tracker socket; fomo itself is
        # never called (three accounts banned by 2026-09-09). `auth` stays constructed so every
        # fomo-shaped signature downstream is unchanged; nothing dereferences it on this path.
        from sniper.feeds.j7_feed import j7_feed_or_none
        _j7_feed = j7_feed_or_none(log_fn=_p)      # raises without a token: fail-closed at start
        _p("[memecoin] signal source: J7 wallet-tracker socket (fomo HTTP/WS lanes off)")
    else:
        _j7_feed = None
        if not (auth.refresh or auth.access):
            sys.exit("no fomo credentials — populate .fomo.env (FOMO_REFRESH_TOKEN required)")
        try:
            auth.refresh_token()  # start with a fresh access token
        except Exception as e:
            _p("[warn] initial refresh failed (%s); continuing with provided access token" % e)

    # DORMANT real-money leg — constructed ONLY when armed (MEMECOIN_REAL_ENABLED) with creds present
    # (funded keypair file + Solana RPC url). None when disarmed -> the paper runner is byte-identical.
    # This wiring goes live on the NEXT deliberate restart, not before.
    from sniper.util.clock import real_utc_now_iso
    runner_started_at = real_utc_now_iso()
    # The restart blackout's START (memecoin_onchain_lane.LANE_DOWN, 2026-10-07): the previous runner's
    # last heartbeat, read before this process writes its own and before any lane thread starts.
    from sniper.services.memecoin_onchain_lane import LANE_DOWN
    from sniper.services.memecoin_runner import read_heartbeat
    _p(LANE_DOWN.open(read_heartbeat(), started_at=runner_started_at, now=time.time()))
    from sniper.settings.memecoin import (
        MEMECOIN_TRADE_SIZE_USD, MEMECOIN_REAL_ENTRY_MAX_AGE_S, MEMECOIN_RECONCILE_PERIOD_S,
        MEMECOIN_REAL_ENABLED, MEMECOIN_EVM_ALLOWLIST,
        MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT)
    if MEMECOIN_REAL_ENABLED:
        if not MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT:
            sys.exit(
                "armed runner requires MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT — refusing start")
        from sniper.services.memecoin_funded_reconcile import reconcile_severity
        # Pass the SWEEP'S OWN PARSE. Byte integrity is not parseability: four `out_of_scope`
        # contracts were authentic and consistent and still unreadable by the exit sweep, so those
        # positions ran with no rails at all (44703, 2026-08-26). Armed startup is the gate that
        # guards LIVE risk continuously, so it is the one that must ask the harder question.
        from sniper.services.memecoin_funded_policy import frozen_policy_from_position
        contract_errors, contract_debt = reconcile_severity(
            conn, cutover_at=MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT,
            policy_parse=frozen_policy_from_position,
        )
        if contract_errors:
            sys.exit("funded contract reconciliation failed: " + "; ".join(contract_errors))
        if contract_debt:
            # Terminal audit debt (closed position / consumed reservation with a bad contract)
            # no longer bricks armed startup (2026-07-21 outage class) — but it must never pass
            # silently: page once at startup and repeat in the log until an operator repairs it
            # (the DEPLOY validator still refuses to ship on top of it).
            _p("[memecoin] ⚠ funded contract AUDIT DEBT (startup proceeds, deploy will refuse): "
               + "; ".join(contract_debt))
            try:
                from sniper.services.memecoin_telegram import send_telegram
                head = "; ".join(contract_debt[:3])
                more = f" (+{len(contract_debt) - 3} more)" if len(contract_debt) > 3 else ""
                send_telegram("memecoin funded-contract AUDIT DEBT (runner started anyway): "
                              + head + more)
            except Exception:
                _p("[warn] audit-debt telegram page failed (startup continues)")
        _p("[memecoin] funded contract reconciliation OK cutoff=%s (debt=%d)"
           % (MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT, len(contract_debt)))
    try:
        # An attempt a restart killed mid-decision never finishes and its alert never resolves.
        _interrupted = m.close_interrupted_entry_attempts(
            conn, started_at=runner_started_at, now_iso=real_utc_now_iso())
        if _interrupted:
            _p("[memecoin] closed %d entry attempt(s) interrupted by the last restart" % _interrupted)
    except Exception:
        _p("[warn] interrupted entry-attempt sweep failed (startup continues)")
    # ROUTE SERIES WRITER (MEMECOIN_ROUTE_SERIES_WRITER_THREAD): its own thread and connection drain
    # the route-quote buffer and write one restart marker per open real position at this start.
    from sniper.settings.memecoin import MEMECOIN_ROUTE_SERIES_WRITER_THREAD
    if MEMECOIN_ROUTE_SERIES_WRITER_THREAD:
        try:
            from sniper.services import memecoin_route_series as _rs_w
            _rs_w.start_writer(started_at=runner_started_at)
            _p("[memecoin] route series writer thread started (drain %.0fs)" % _rs_w.DRAIN_S)
        except Exception:
            _p("[warn] route series writer thread failed to start; maintenance flush stands:\n"
               + traceback.format_exc())
    # start_standing: the one process that signs standing-allowance top-ups (2026-10-15).
    contexts = build_all_contexts(conn=conn, allow_shyft_unavailable=True, start_standing=True)
    from sniper.services.memecoin_shyft_startup import ShyftStartupGate
    _custody_ready = (contexts.get('solana') or {}).get('custody_ready_fn', lambda: True)
    _shyft_startup = ShyftStartupGate(ready_fn=_custody_ready)
    if 'solana' in contexts:
        contexts['solana']['entry_ready_fn'] = lambda: _shyft_startup.ready.is_set() and _custody_ready()
    if '4663' in contexts:
        from sniper.execution.evm_pons_counter_quote import start_cache
        start_cache()
    # EXIT PRICE TAPE (spec 2026-09-30 §3a): started here, before any sweep thread exists, so the
    # module's one tape reference is assigned once. A failure leaves the confirmation hook a no-op.
    from sniper.settings.memecoin import MEMECOIN_EXIT_TAPE_ENABLED as _EXIT_TAPE_ON
    if _EXIT_TAPE_ON:
        try:
            from sniper.services import memecoin_exit_tape as _exit_tape_mod
            _p("[memecoin] exit tape: writer started (%s)" % _exit_tape_mod.start().path)
        except Exception:
            _p("[err] exit tape start failed (exits unaffected):\n" + traceback.format_exc())
    # EVM RPC POOL OBSERVABILITY + SELF-RANKING (2026-09-03). One eth_blockNumber per endpoint per
    # 15s so latency and head lag are known for every pool member (not just the one in use), the
    # heartbeat shows them by HOST, and the operator is paged once when the configured primary has
    # been out-ranked or quarantined for 5 minutes -- a degraded provider becomes a line in the
    # heartbeat and one page, instead of a DOWN page and a hand-edit of .env.
    _evm_pools = {}
    _erp = None                            # stays None if the block below fails; helpers no-op
    try:
        from sniper.execution import evm_rpc_pool as _erp
        from sniper.execution.evm_wallet import _http as _evm_http
        from sniper.settings import memecoin as _cfg
        import json as _ejson
        for _chain in _cfg.MEMECOIN_EVM_ALLOWLIST:
            _primary = _cfg.evm_rpc_for_chain(_chain)
            _urls = _erp.pool_for(_primary) if _primary else []
            if len(_urls) > 1:
                _evm_pools[str(_chain)] = (_primary, _urls)
        _hb_body = _ejson.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})

        def _head_of(url):
            status, j, _d = _evm_http("POST", url, body=_hb_body)
            if status != 200 or not isinstance(j, dict) or not j.get("result"):
                raise RuntimeError("no head")
            return int(str(j["result"]), 16)
        _id_body = _ejson.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []})
        for _chain, (_primary, _urls) in _evm_pools.items():
            def _verify(url, _want=int(str(_chain), 10)):
                status, j, _d = _evm_http("POST", url, body=_id_body)
                return status == 200 and isinstance(j, dict) and int(str(j.get("result") or "0x0"), 16) == _want
            _erp.start_head_sampler(_urls, _head_of, verify_fn=_verify)
    except Exception:
        _p("[warn] evm rpc pool sampler not started:\n" + traceback.format_exc())
    _evm_degraded_since = {}
    _evm_degraded_paged_at = {}

    def _evm_pool_status():
        if _erp is None or not _evm_pools:
            return None
        try:
            return {c: _erp.snapshot(u) for c, (_pri, u) in _evm_pools.items()} or None
        except Exception:
            return None

    def _evm_primary_unhealthy(pri, urls):
        """Why the configured primary is not fit to serve, or None when it is simply out-ranked.

        DESELECTION IS NOT DEGRADATION (2026-09-08). The old test was `chosen != configured`, which
        the docstring called "the whole signal". It is not: this pool RANKS BY MEASURED LATENCY AND
        LAG, so a healthy primary that is merely slower than a peer trips it. Both chains paged at
        11:58Z with every member at zero failures, zero quarantine and verified identity — 4663's
        primary was 157 ms / 5.6 blocks against QuickNode's 55 ms / 0, and 56's was 250 ms against
        publicnode's 63 ms. The pool had picked the better endpoint on both, which is its job.

        A red page that fires on correct behaviour, and whose own text says "this is FYI", teaches
        the operator to ignore the channel that also carries real outages.
        """
        try:
            snap = {m["host"]: m for m in _erp.snapshot(urls)}
        except Exception:
            return None                        # cannot tell -> never page on a guess
        host = pri.split("/")[2] if "//" in pri else pri
        m = snap.get(host)
        if m is None:
            return "primary absent from the pool"
        if m.get("identity") is False:
            return "identity check failed (wrong chain)"
        if (m.get("quarantined_s") or 0) > 0:
            return "quarantined %.0fs" % m["quarantined_s"]
        # Full-history discovery can be limited while current reads/sends remain healthy.
        refused = [method for method in m.get("refused_methods", [])
                   if method != "eth_getLogs:full_range"]
        if refused:
            return "refuses %s" % ",".join(refused[:3])
        age = m.get("last_penalty_age_s")
        if m.get("last_penalty") and age is not None and age <= 300:
            return "penalised %s %.0fs ago" % (m["last_penalty"], age)
        return None                            # healthy, just out-ranked

    def _evm_pool_watch():
        """Page ONCE per 30 min when the configured primary is UNFIT to serve — quarantined, wrong
        chain, refusing methods, or recently penalised. Being out-ranked by a faster peer is the
        pool working and is never paged."""
        if _erp is None or not _evm_pools:
            return
        now = time.monotonic()
        for c, (pri, urls) in _evm_pools.items():
            try:
                chosen = _erp.choose(pri)
            except Exception:
                continue
            why = None if chosen == pri else _evm_primary_unhealthy(pri, urls)
            if why is None:
                _evm_degraded_since.pop(c, None)
                continue
            since = _evm_degraded_since.setdefault(c, now)
            if now - since < 300 or now - _evm_degraded_paged_at.get(c, 0) < 1800:
                continue
            _evm_degraded_paged_at[c] = now
            try:
                from sniper.services import memecoin_alerts as _al
                _al.send_alert("EVM-RPC-DEGRADED",
                               "chain %s: configured primary %s UNFIT for %d min (%s); reads on %s. "
                               "Startup, reads and submission fail over on their own."
                               % (c, pri.split("/")[2], int((now - since) // 60), why,
                                  chosen.split("/")[2]))
            except Exception:
                _p("[warn] EVM-RPC-DEGRADED page failed")
    # ON-CHAIN M5 VOLUME FEED (2026-09-16). The observer's watchdog only proves a PID lives; a dark
    # socket fell back to the provider silently. Heartbeat it and page stale/recovery once each.
    _volume_feed_alerts = {}

    def _onchain_volume_feed():
        try:
            from sniper.services import memecoin_volume_writer as _vw
            from sniper.util.clock import real_utc_now
            return _vw.watch_feeds(_volume_feed_alerts, real_utc_now())
        except Exception:
            return None                        # observability only; never costs a heartbeat

    def _persist_j7_tracked(conn, feed):
        """Store each account's J7 tracked lists. Best-effort; never costs the runner a tick.

        These lists ARE the addressable roster: J7 caps 50 wallets per account and relays trades
        for those only, so a roster row absent from every list can never signal. The socket thread
        must not write to the ledger, so this runs on the heartbeat thread that owns `conn`.
        """
        if feed is None:
            return
        try:
            from sniper.persistence import db_memecoin as _m
            from sniper.feeds.j7_feed import tracked_frames
            # The stamp is the frame's ARRIVAL, not this heartbeat's clock: a cached list on a
            # reconnecting account must not read as fresh (Codex review 2026-09-23, A3). The
            # COUNT travels with the list. When the two disagree the tracked set is unknown for
            # that kind, and the prune refuses rather than reading an unreadable list as an
            # empty roster (2026-09-22: 9 pump entries parsed to zero).
            status = feed.status()
            accounts = status.get("accounts") or [status]
            status = {"accounts": accounts}
            fingerprints = [account["account_fingerprint"] for account in accounts]
            for index, kind, rows, seen, count in tracked_frames(status):
                _m.replace_j7_tracked(conn, index, kind, rows, seen_at=seen,
                                      reported_count=count,
                                      account_fingerprint=fingerprints[index])
            # AND THE LISTS THAT NEVER ARRIVED (Codex recheck 2026-09-23, A3). A configured
            # account that has never delivered a frame wrote no row at all, so completeness had
            # nothing to compare and a fresh account certified a silent one: a wallet belonging to
            # the silent account read as tracked by nobody and was nominated for pruning. The
            # marker is a refusal, not evidence, and `mark_j7_list_unseen` never touches a list
            # that has reported.
            from sniper.feeds.j7_feed import expected_lists
            from sniper.util.clock import real_utc_now_iso as _now_iso
            now_seen = _now_iso()
            for index, kind, has_frame in expected_lists(status):
                if not has_frame:
                    _m.mark_j7_list_unseen(conn, index, kind, at=now_seen,
                                           account_fingerprint=fingerprints[index])
        except Exception:
            _p("[warn] j7 tracked-list persist failed (runner unaffected)")
    lifi_heartbeat_ready = threading.Event()
    start_lifi_recovery_worker(
        contexts, lifi_heartbeat_ready, connection_factory=get_connection,
        now_fn=real_utc_now_iso,
    )
    lifi_alert_state = {"reasons": None}

    def _lifi_status():
        from sniper.services.memecoin_alerts import alert_lifi_startup_transition

        status = lifi_status_for_contexts(conn, contexts, real_utc_now_iso())
        lifi_alert_state["reasons"] = alert_lifi_startup_transition(
            lifi_alert_state["reasons"], status["disable_reasons"],
            baseline_ready=lifi_alert_baseline_ready(contexts, status))
        return status
    from sniper.settings.memecoin import MEMECOIN_ARMS as _BOOT_ARMS
    report_paper_arms([] if _FUNDED_ONLY else _BOOT_ARMS)
    if MEMECOIN_REAL_ENABLED:
        report_evm_context_gaps(contexts, allowlist=MEMECOIN_EVM_ALLOWLIST)
        from sniper.services.memecoin_copy_arm import copy_arm_enabled
        report_copy_arm_policy(enabled=copy_arm_enabled())
    maintenance_contexts = {k: v for k, v in contexts.items() if k != "1"}
    # Rails behind an unknown-confirm partial: every sweep offers the skipped row here and moves on.
    _rail_settler = RailDeriskSettler(contexts, connect=get_connection) if contexts else None
    if _rail_settler is not None:
        _rail_settler.start()
    _reconcile_main_scope, _reconcile_worker_scope = reconcile_scopes(contexts)
    _ethereum_worker = None
    real_hook = None
    _confirm_for = None
    _confirm_proof_for = None
    _confirmed_nonce_for = None
    _confirmed_entry_for = None
    _token_balance_for = None
    _pending_nonce_for = None
    from sniper.services.memecoin_wallet_caps import CACHE as wallet_cap_cache
    from sniper.settings.memecoin import MEMECOIN_REAL_WALLET_CAPS_ENABLED
    if contexts and MEMECOIN_REAL_WALLET_CAPS_ENABLED:
        wallet_cap_cache.start(get_readonly_connection, real_utc_now_iso)
        _p("[memecoin] wallet entry ceilings loaded: %s" % wallet_cap_cache.status())
    # NATIVE FLOAT DRIFT PANEL (operator 2026-10-08): display-only sampler, its own thread, files and
    # public reads; it never raises into this loop and takes no lock the fill path takes.
    if contexts:
        try:
            from sniper.services.memecoin_native_float import start_sampler as _float_start
            _float_evm = next((c.get("address") for k, c in contexts.items()
                               if k != "solana" and isinstance(c, dict) and c.get("address")), None)
            if _float_start(evm_address=_float_evm) is not None:
                _p("[memecoin] native float sampler started (display only)")
        except Exception:
            _p("[err] native float sampler did not start (display only):\n" + traceback.format_exc())
    _proof_snapshot_for = None
    if contexts:
        from sniper.settings.memecoin import (
            MEMECOIN_REAL_SIZING, MEMECOIN_BANKROLL_FRACTION, MEMECOIN_MAX_CONCURRENT)
        real_hook = make_real_hook(conn, contexts, now_fn=real_utc_now_iso,
                                   trade_size=MEMECOIN_TRADE_SIZE_USD,
                                   max_age_s=MEMECOIN_REAL_ENTRY_MAX_AGE_S,
                                   stats_fn=dexscreener_stats)
        _confirm_for = make_confirm_for(contexts)
        _confirm_proof_for = make_confirm_proof_for(contexts)
        _confirmed_nonce_for = make_confirmed_nonce_for(contexts)
        _token_balance_for = make_token_balance_for(contexts)
        _pending_nonce_for = make_pending_nonce_for(contexts)
        _proof_snapshot_for = make_proof_snapshot_for(contexts)

        def _confirmed_entry_for(recovery_conn, row, now_iso):
            ctx = context_for(contexts, row.get("network_id"))
            fn = (ctx or {}).get("recover_confirmed_entry_fn")
            return fn(recovery_conn, row, now_iso) if fn else None
        if "1" in contexts:
            from sniper.services.memecoin_eth_worker import EthereumWorker
            eth_contexts = {"1": contexts["1"]}
            eth_reconciled_at = [0.0]

            def _eth_maintenance(worker_conn):
                now_iso = real_utc_now_iso()
                recover_stale_reservations(
                    worker_conn, confirm_for=_confirm_for, now_iso=now_iso,
                    confirmed_entry_for=_confirmed_entry_for,
                    confirmed_nonce_for=_confirmed_nonce_for,
                    token_balance_for=_token_balance_for, pending_nonce_for=_pending_nonce_for,
                    confirm_proof_for=_confirm_proof_for, proof_snapshot_for=_proof_snapshot_for,
                    network_ids={"1"})
                from sniper.services.memecoin_real_runner import run_entry_retry_lane, _run_dip_hits
                run_entry_retry_lane(worker_conn, _ethereum_worker.hook, now_iso,
                                     network_id="1", include_dips=False)
                _run_dip_hits(worker_conn, _ethereum_worker.hook, now_iso,
                              host="ethereum_worker", network_id="1", hits_lock=_ethereum_worker.dip_lock)
                real_exit_sweep(worker_conn, eth_contexts, now_fn=real_utc_now_iso,
                                lane="ethereum_worker", network_ids={"1"}, derisk_settler=_rail_settler)
                if time.monotonic() - eth_reconciled_at[0] >= 60:
                    real_reconcile(worker_conn, eth_contexts, **_reconcile_worker_scope)
                    eth_reconciled_at[0] = time.monotonic()

            _ethereum_worker = EthereumWorker(real_hook, connect=get_connection, tick=_eth_maintenance)
            _ethereum_worker.start()
            real_hook = _ethereum_worker
            _p("[memecoin] Ethereum entries/exits: own worker, private submission only")
        _clip = ("%.0f%% of venue equity" % (MEMECOIN_BANKROLL_FRACTION * 100)
                 if MEMECOIN_REAL_SIZING == "fraction"
                 else ("equity/%d slots" % MEMECOIN_MAX_CONCURRENT
                       if MEMECOIN_REAL_SIZING == "bankroll"
                       else "$%.0f fixed" % MEMECOIN_TRADE_SIZE_USD))
        _submit_modes = {
            key: {"backend": value.get("submission_backend", "unknown"),
                  "approval_pipeline": bool(value.get("approval_pipeline", False))}
            for key, value in contexts.items()
        }
        _p("[memecoin] ⚠ REAL leg ARMED — venues=%s submit=%s clip=%s slots=%d "
           "authority=%s"
           % (list(contexts.keys()), _submit_modes, _clip, MEMECOIN_MAX_CONCURRENT,
              funded_authority_counts(conn, contexts)))
    else:
        _p("[memecoin] real leg OFF (disarmed or creds missing) — paper only")

    candidate_engine = None
    from sniper.settings.memecoin import MEMECOIN_STRATEGY_V0_ENABLED
    if MEMECOIN_STRATEGY_V0_ENABLED and not _FUNDED_ONLY:
        try:
            from sniper.research.memecoin_strategy.live import CandidateLiveEngine
            from sniper.services.memecoin_capture_v2 import register_capture_cohort
            candidate_engine = CandidateLiveEngine(conn, real_utc_now_iso())

            import threading as _threading

            def _candidate_worker():
                from sniper.persistence.db_core import get_connection as _candidate_connection
                worker_conn = _candidate_connection()
                worker = CandidateLiveEngine(worker_conn, real_utc_now_iso())
                while True:
                    try:
                        now_iso = real_utc_now_iso()
                        result = worker.process_observations(now_iso)
                        for request in result["capture_requests"]:
                            register_capture_cohort(
                                worker_conn, request["network_id"], request["mint"],
                                request["context"], request["observed_at"],
                                request["expires_at"], request["priority"],
                            )
                    except Exception:
                        finish_db_cycle(worker_conn, failed=True)
                        _p("[err] strategy v0 worker failed:\n" + traceback.format_exc())
                    else:
                        finish_db_cycle(worker_conn)
                    time.sleep(1.0)

            _threading.Thread(target=_candidate_worker, daemon=True).start()
            _p("[memecoin] strategy v0 paper candidates active")
        except Exception:
            candidate_engine = None
            _p("[err] strategy v0 disabled after startup failure:\n" + traceback.format_exc())

    n = curated = opened = exits = 0
    real_tally = {}   # cumulative real-hook outcomes since boot (visible in the 30s status line)
    auth_alerted = False   # opaque paging state: falsy healthy, (first, last) tuple while dead
    last_refresh = time.time()
    refresh_retry_after = 0.0
    last_bracket = 0.0
    launch_sweep_published = False   # see publish_sweep: the deploy waits for this stamp
    launch_published_at = None       # when it did, for the first-cycle line
    last_evm_px = 0.0
    # PAPER pricing for DexScreener-blind chains — built regardless of whether the real leg is
    # armed, because the paper tape is the only way 4663 ever becomes measurable.
    from sniper.services.memecoin_evm_price import build_evm_price_for, build_evm_mcap_for
    evm_price_for = build_evm_price_for()
    # market cap for DexScreener-blind chains: price x on-chain totalSupply (cached per
    # mint). Without it every non-Solana row shows a blank mcap and the mcap exit-bands
    # can never fire off-Solana.
    evm_mcap_for = build_evm_mcap_for()
    last_sync = 0.0
    last_cleanup = 0.0
    last_activity = 0.0
    last_log = 0.0
    _phase_tot = {}               # per-phase seconds accumulated since the last report
    _phase_since_n = 0            # cycle counter at the last report, to average over
    _iters = 0                    # TRUE loop iterations; `n` counts successful feed polls only
    _iters_at_report = 0
    _report_wall = 0.0
    last_reconcile = time.monotonic()
    # Rent sweep: reclaim SOL locked in empty token accounts left by closed positions.
    # First pass is deliberately NOT at cycle 0 — startup already has plenty to do, and the rent
    # has been sitting there for days, so one more period changes nothing.
    from sniper.settings.memecoin import (
        MEMECOIN_RENT_SWEEP_ENABLED as _RENT_SWEEP_ENABLED,
        MEMECOIN_RENT_SWEEP_PERIOD_S as _RENT_SWEEP_PERIOD_S,
        MEMECOIN_EVM_GAS_TOPUP_ENABLED as _EVM_GAS_TOPUP_ENABLED,
        MEMECOIN_EVM_GAS_TOPUP_PERIOD_S as _EVM_GAS_TOPUP_PERIOD_S,
        MEMECOIN_GAS_TOPUP_ENABLED as _GAS_TOPUP_ENABLED,
        MEMECOIN_GAS_TOPUP_PERIOD_S as _GAS_TOPUP_PERIOD_S,
        MEMECOIN_SOL_EXEC_BUFFER_CAP_USD as _BUFFER_CAP,
        MEMECOIN_SOL_EXEC_BUFFER_TOPUP_USD as _BUFFER_TOPUP,
        MEMECOIN_SOL_EXEC_BUFFER_DAILY_CAP_USD as _BUFFER_DAILY,
        MEMECOIN_SOL_EXEC_BUFFER_MIN_USDC_USD as _BUFFER_KEEP,
        MEMECOIN_SOL_EXEC_BUFFER_PERIOD_S as _BUFFER_PERIOD,
        MEMECOIN_HOLDINGS_ENABLED as _HOLDINGS_ENABLED,
        MEMECOIN_HOLDINGS_PERIOD_S as _HOLDINGS_PERIOD_S,
        MEMECOIN_RESIDUAL_TAIL_ENABLED as _RESIDUAL_TAIL_ENABLED,
        MEMECOIN_RESIDUAL_TAIL_FEE_BUFFER_PCT as _RESIDUAL_FEE_BUFFER_PCT,
        MEMECOIN_POST_TP_FOLLOWUP_ENABLED as _POST_TP_ENABLED,
        MEMECOIN_POST_TP_FOLLOWUP_PERIOD_S as _POST_TP_PERIOD_S,
        MEMECOIN_REVIVAL_WATCH_ENABLED as _REVIVAL_ENABLED,
        MEMECOIN_VOLDEATH_SHADOW_ENABLED as _VOLDEATH_ENABLED)
    last_rent_sweep = 0.0
    _sol_buffer_box = {"watch": None}     # BufferWatch, set when the buffer worker starts (2026-10-21)
    last_evm_gas_topup = 0.0
    # EVM GAS TOP-UP, OFF THE FUNDED LOOP (2026-09-27): the inline pass took 58.8 s and 40.6 s at
    # 12:35Z. Its own thread and connection; one pass every MEMECOIN_EVM_GAS_TOPUP_PERIOD_S, each
    # run to completion. MEMECOIN_EVM_GAS_TOPUP_THREAD=0 keeps it inline below, as before.
    from sniper.services import memecoin_evm_gas_topup as _egt_mod
    _evm_gas_topup_worker = None
    # GAS-LOW / GAS-OK page state, one entry per chain. Only whichever runs the pass touches it:
    # the worker thread below, or the inline branch when the worker is off -- never both.
    _evm_gas_alert_state = {}
    if _EVM_GAS_TOPUP_ENABLED and contexts and _egt_mod.thread_enabled():
        from sniper.persistence.db_core import get_connection as _egt_connect
        _evm_gas_topup_worker = _egt_mod.EvmGasTopupWorker(
            pass_fn=lambda _c, _now: run_evm_gas_topup_pass(
                _c, contexts=contexts, now_iso=_now, gas_alert_state=_evm_gas_alert_state),
            connect=_egt_connect, now_iso=real_utc_now_iso, period_s=_EVM_GAS_TOPUP_PERIOD_S,
            log=_p)
        threading.Thread(target=_evm_gas_topup_worker.run_forever, name="memecoin-evm-gas-topup",
                         daemon=True).start()
        _p("[memecoin] EVM gas top-up: own thread started (period %.0fs)" % _EVM_GAS_TOPUP_PERIOD_S)
    # EVM GATE BALANCE WARMER (2026-10-15): keeps the early quote's cached sizing inside its window
    # on quiet chains (MEMECOIN_EVM_GATE_BALANCE_WARM_CHAINS, read every pass). Reads only.
    _gate_balance_warmer = None
    from sniper.services.memecoin_real_runner import (
        quote_price_warmer_status as _quote_price_warmer_status)
    if contexts:
        from sniper.services.memecoin_real_runner import GateBalanceWarmer
        _gate_balance_warmer = GateBalanceWarmer(lambda: contexts)
        threading.Thread(target=_gate_balance_warmer.run_forever, name="memecoin-gate-balance-warm",
                         daemon=True).start()
    # EVM-08 INTENT FENCE SWEEP (spec 2026-10-07, revision 2): the single owner that resolves an
    # intent record left by a failed stamp or a crash, on chain proof only, and alerts once a record
    # has fenced its chain for 10 minutes. Own thread: the reconcile pass runs every 120 s, slower
    # than W = 15 s. With no record on disk a pass is one directory listing and no network call.
    if contexts:
        from sniper.execution import exec_lock as _xl
        from sniper.services.memecoin_alerts import send_alert as _xl_alert
        threading.Thread(target=_xl.run_intent_sweep_forever,
                         kwargs={"alert_fn": lambda text: _xl_alert("exec_intent_fence", text)},
                         name="memecoin-intent-sweep", daemon=True).start()
    # EXECUTABLE EXTREMES (2026-09-30): the top and bottom of each closed funded position's
    # exit_decision route series, once a minute, on its own thread and connection. It reads rows the
    # maintenance flush already wrote and sets two research columns to NULL on closed funded rows.
    # No entry, exit or main-loop phase waits for it; one short write transaction per pass.
    if contexts and not _FUNDED_ONLY:
        def _exec_extremes_worker():
            from sniper.persistence.db_core import get_connection as _xx_connect
            xconn = _xx_connect()
            # Give up rather than queue on the funded writer: a held ledger lock makes the pass
            # return busy after 250 ms, and the next pass does the work.
            xconn.execute("PRAGMA busy_timeout=250")
            # No auto-checkpoint on this connection, so write_ms is the lock hold and never a
            # checkpoint that happened to fall on this commit; the other writers checkpoint.
            xconn.execute("PRAGMA wal_autocheckpoint=0")
            while True:
                try:
                    _xx = m.exec_extremes_pass(xconn, real_utc_now_iso())
                    if _xx["written"] or _xx["cleared"] or _xx["busy"]:
                        _p("[memecoin] exec extremes: visited=%d written=%d cleared=%d busy=%s "
                           "read_ms=%.1f lock_wait_ms=%.1f write_ms=%.1f" % (
                               _xx["visited"], _xx["written"], _xx["cleared"], _xx["busy"],
                               _xx["read_ms"], _xx["lock_wait_ms"], _xx["write_ms"]))
                except Exception:
                    try:
                        xconn.rollback()
                    except Exception:
                        pass
                    _p("[err] exec extremes pass failed:\n%s" % traceback.format_exc())
                time.sleep(60.0)
        threading.Thread(target=_exec_extremes_worker, name="memecoin-exec-extremes",
                         daemon=True).start()
        _p("[memecoin] exec extremes: own thread started")
    # OP-STACK FEES (Ink Task 8, 2026-09-29): record each funded transaction's L2 execution, L1 data
    # and operator fee from its canonical receipt, once a minute, on its own thread and connection.
    # It reads transactions already booked; no entry or exit waits for it. Only contexts that carry
    # a fee_reconcile_fn (OP-stack fee chains: Ink) start it, so every other chain is untouched.
    _fee_contexts = {k: v for k, v in (contexts or {}).items() if v.get("fee_reconcile_fn")}
    if _fee_contexts:
        def _op_fee_worker():
            from sniper.persistence.db_core import get_connection as _fee_connect
            fee_conn = _fee_connect()
            while True:
                for _chain, _ctx in _fee_contexts.items():
                    try:
                        _fees = _ctx["fee_reconcile_fn"](fee_conn)
                        if _fees.get("recorded") or _fees.get("unknown"):
                            _p("[memecoin] %s fees: %s" % (_chain, _fees))
                    except Exception:
                        try:
                            fee_conn.rollback()
                        except Exception:
                            pass
                        _p("[err] %s fee pass failed:\n%s" % (_chain, traceback.format_exc()))
                time.sleep(60.0)
        threading.Thread(target=_op_fee_worker, name="memecoin-op-fees", daemon=True).start()
        _p("[memecoin] OP-stack fees: own thread started for %s" % sorted(_fee_contexts))
    # The V4 key seed's own cadence and cursor. The cursor starts one hour back because the tape
    # retains about that long; an older `resolved_at` has no rows left to read anyway.
    last_v4_key_seed = 0.0
    _v4_key_seed_since = {}
    from datetime import datetime as _dt_seed, timedelta as _td_seed, timezone as _tz_seed
    _seed_since_default = (_dt_seed.now(_tz_seed.utc) - _td_seed(hours=1)).isoformat()
    last_gas_topup = 0.0
    last_holdings = 0.0
    last_evm_holdings = {}
    _last_global_feed = 0.0
    from sniper.settings.memecoin import MEMECOIN_GLOBAL_FEED_PERIOD_S as _GLOBAL_FEED_PERIOD_S
    rails_obs = None
    last_post_tp = 0.0
    last_mirror_cf = 0.0
    # Only closes seen from THIS PROCESS START get follow-up schedules. A quote taken now cannot
    # reconstruct the executable route of a past exit, so backfilling would manufacture evidence.
    _post_tp_since = real_utc_now_iso()
    # Residual-tail study: its own watermark, advanced as closes are handed to workers. Same
    # no-backfill rule and for the same reason — a quote taken now cannot reconstruct a past exit.
    _residual_since = real_utc_now_iso()
    _residual_seen = set()
    _rent_keypair = None
    from sniper.settings.memecoin import MEMECOIN_WITHDRAW_SOLANA_ADDRESS as _WD_ADDR
    if (_RENT_SWEEP_ENABLED or _GAS_TOPUP_ENABLED or _HOLDINGS_ENABLED or _WD_ADDR
            or _BUFFER_CAP > 0):
        try:
            from sniper.execution.solana_wallet import load_keypair as _load_kp
            from sniper.settings.memecoin import MEMECOIN_SOLANA_KEYPAIR as _KP_PATH
            _rent_keypair = _load_kp(_KP_PATH)
        except Exception:
            # No keypair, no sweep. It is a chore; it must never block the runner from starting.
            _p("[memecoin] gas maintenance: keypair unavailable, sweep and top-up OFF")
            _rent_keypair = None
    last_snapshot_day = m.latest_snapshot_day(conn)
    # transition state for operational alerts (fire once on crossing, not every cycle)
    _kill_tripped = _daily_tripped = _gas_low = False
    PUMP_PERIOD_S = float(os.getenv("MEMECOIN_PUMP_CAPTURE_PERIOD_S", "30"))
    _p("[memecoin] loop start: poll=%dms size=$%.0f curated=%d"
          % (POLL_MS, SIZE_USD, len(m.curated_user_ids(conn))))
    # Schema/candidate/context startup can write through this long-lived handle. End that unit
    # before background workers and the first feed/provider request start.
    finish_db_cycle(conn)

    # The follow poll uses a background thread and its own DB connection.
    # Bounded workers reduce the fast-lane sweep time without blocking the feed poll.
    _fomo_activity_ws_lock = threading.Lock()
    _fomo_activity_ws = {
        "status": "disabled",
        "connected_at": None,
        "subscribed_at": None,
        "last_payload_at": None,
        "last_error_at": None,
        "reconnect_count": 0,
    }
    _fomo_activity_queues = [queue.Queue(maxsize=512) for _ in range(2)]

    def _fomo_activity_ws_update(**values):
        with _fomo_activity_ws_lock:
            _fomo_activity_ws.update(values)

    def _fomo_activity_ws_snapshot():
        with _fomo_activity_ws_lock:
            return dict(_fomo_activity_ws)

    def _activity_worker():
        from sniper.persistence.db_core import get_connection
        from sniper.services.memecoin_follow_activity import fast_lane_wallets, follow_activity_pass
        from sniper.settings.memecoin import MEMECOIN_FOLLOW_FAST_ENABLED
        aconn = get_connection()
        while True:
            try:
                # real_hook here too: this path was invisible to the real leg, so ~50% of the
                # curated buy tape (286 buys) was untradeable by construction. Its own DB conn is
                # fine — reserve_pending's partial UNIQUE(mint) is the atomic cross-thread race
                # guard, so two threads can never double-open the same coin.
                # SLOW LANE: while the fast lane covers the armed set continuously, this pass
                # skips those wallets (WAF budget; swap-id dedup makes overlap harmless anyway).
                excl = ({w["source_user_id"] for w in fast_lane_wallets(aconn)}
                        if MEMECOIN_FOLLOW_FAST_ENABLED else None)
                ares = follow_activity_pass(aconn, auth, SIZE_USD, stats_fn=dexscreener_stats,
                                            real_hook=real_hook, exclude_uids=excl)
                if ares.get("copied") or ares.get("sells_missed"):
                    _p("[memecoin] follow-activity: copied %d feed-missed buy(s), "
                          "sell-coverage %d/%d seen (checked %d)"
                          % (ares.get("copied", 0), ares.get("sells_seen", 0),
                             ares.get("sells_seen", 0) + ares.get("sells_missed", 0),
                             ares.get("checked", 0)))
            except Exception:
                finish_db_cycle(aconn, failed=True)
                _p("[err] activity pass failed:\n" + traceback.format_exc())
            else:
                finish_db_cycle(aconn)
            time.sleep(ACTIVITY_PERIOD_S)

    def _fast_activity_worker():
        # FAST LANE (2026-07-15 latency cliff): armed wallets only, polled continuously —
        # ~31 wallets × 2s WAF spacing ≈ 62s/sweep + a short idle, so armed-wallet catches land
        # in the +EV <60-70s band (+$79/copy 78% WR honest alpha_b) instead of the 180-600s
        # losing bucket the 300s lane produces by construction. Roster re-read every sweep so
        # arm/revoke applies live. Same swap-id dedup as the slow lane — never double-opens.
        from sniper.persistence.db_core import get_connection
        from sniper.services.memecoin_follow_activity import fast_lane_wallets, follow_activity_pass
        from sniper.settings.memecoin import MEMECOIN_FOLLOW_FAST_IDLE_S
        fconn = get_connection()
        while True:
            try:
                fast = fast_lane_wallets(fconn)
                if fast:
                    from sniper.settings.memecoin import (
                        MEMECOIN_FOLLOW_FAST_FETCH_WORKERS, MEMECOIN_FOLLOW_FAST_SLEEP_S)
                    fres = follow_activity_pass(fconn, auth, SIZE_USD, stats_fn=dexscreener_stats,
                                                real_hook=real_hook, wallets=fast,
                                                sleep_s=MEMECOIN_FOLLOW_FAST_SLEEP_S,
                                                fetch_workers=MEMECOIN_FOLLOW_FAST_FETCH_WORKERS)
                    if fres.get("copied"):
                        _p("[memecoin] follow-fast: copied %d armed-wallet buy(s) (checked %d)"
                           % (fres.get("copied", 0), fres.get("checked", 0)))
            except Exception as exc:
                finish_db_cycle(fconn, failed=True)
                _p("[err] fast activity pass failed (sqlite_errorcode=%s):\n%s"
                   % (getattr(exc, "sqlite_errorcode", None), traceback.format_exc()))
            else:
                finish_db_cycle(fconn)
            time.sleep(max(1.0, float(os.getenv("MEMECOIN_FOLLOW_FAST_IDLE_S",
                                                str(MEMECOIN_FOLLOW_FAST_IDLE_S)))))

    def _fomo_activity_dispatch_worker(work_queue):
        from sniper.feeds.fomo_account import activity_receipt_observation
        from sniper.persistence.db_core import get_connection
        from sniper.services.memecoin_follow_activity import dispatch_trading_activity

        wconn = get_connection()
        while True:
            payload, observed_at = work_queue.get()
            try:
                dispatch_trading_activity(
                    wconn,
                    payload,
                    SIZE_USD,
                    real_hook=real_hook,
                    now_fn=real_utc_now_iso,
                    stats_fn=dexscreener_stats,
                )
            except Exception:
                finish_db_cycle(wconn, failed=True)
                _p("[err] Fomo activity dispatch failed:\n" + traceback.format_exc())
            else:
                finish_db_cycle(wconn)
            finally:
                try:
                    receipt = activity_receipt_observation(
                        payload, observed_at or real_utc_now_iso(),
                    )
                    uid = receipt["source_user_id"]
                    m.insert_feed_observation_v2(
                        wconn, receipt,
                        uid in m.curated_user_ids(wconn),
                        uid in m.real_armed_any_chain_user_ids(wconn),
                    )
                except Exception:
                    finish_db_cycle(wconn, failed=True)
                    _p("[err] Fomo activity receipt failed:\n" + traceback.format_exc())
                work_queue.task_done()

    def _fomo_activity_ws_worker():
        from sniper.feeds.fomo_account import stream_trading_activity
        from sniper.services.memecoin_follow_sync import _default_user_fn

        from sniper.feeds.fomo_feed import fomo_ws_backoff, is_ws_policy_refusal

        topic_id = None
        retry_s = 1.0
        while True:
            session_started = time.time()
            try:
                _fomo_activity_ws_update(
                    status="connecting", connected_at=real_utc_now_iso(),
                )
                topic_id = topic_id or _default_user_fn(auth)
                if not topic_id:
                    raise RuntimeError("Fomo current user ID is unavailable")
                for payload in stream_trading_activity(
                        auth, topic_id,
                        on_subscribed=lambda: _fomo_activity_ws_update(
                            status="subscribed", subscribed_at=real_utc_now_iso()),
                ):
                    _fomo_activity_ws_update(
                        status="active", last_payload_at=real_utc_now_iso())
                    enqueue_fomo_activity(
                        _fomo_activity_queues, payload, received_at=real_utc_now_iso(),
                    )
                    # NOT `retry_s = 1.0` here: a payload proves the socket opened, not that it is
                    # welcome. Resetting per payload meant a socket kicked with `1008 policy
                    # violation` right after its first message retried a second later, which is
                    # how 13 reconnects happened in ten minutes on 2026-09-06. The reset now lives
                    # in fomo_ws_backoff and is earned by SESSION LENGTH.
                raise ConnectionError("Fomo WebSocket ended")
            except Exception as _ws_exc:
                with _fomo_activity_ws_lock:
                    _fomo_activity_ws["status"] = "reconnecting"
                    _fomo_activity_ws["last_error_at"] = real_utc_now_iso()
                    _fomo_activity_ws["reconnect_count"] += 1
                _session_s = time.time() - session_started
                retry_s = fomo_ws_backoff(retry_s, _session_s, _ws_exc)
                # Compute FIRST, then log: the message must name the wait that is about to happen,
                # and it names the session length because that is what earns or denies the reset.
                _p("[warn] Fomo activity WebSocket reconnect in %.0fs "
                   "(session %.0fs, %s):\n%s"
                   % (retry_s, _session_s,
                      "policy refusal" if is_ws_policy_refusal(_ws_exc) else "transport",
                      traceback.format_exc()))
                time.sleep(retry_s)

    # THE ON-CHAIN LANE'S WAKE (2026-09-27). The confirmed watcher (another process) sends a
    # datagram after each committed detection, and the slot lane's handoffs set it in-process, so
    # the lane sweeps at once instead of at its next poll (baseline p50 0.427 s, p90 0.860 s). The
    # poll stays as recovery. Bound only when the lane runs; MEMECOIN_ONCHAIN_LANE_WAKE=0 gives the
    # plain in-process Event. sniper/services/memecoin_onchain_wake.py.
    from sniper.settings.memecoin import MEMECOIN_ONCHAIN_LANE_ENABLED as _OL_ON
    if _OL_ON and real_hook is not None:
        from sniper.services.memecoin_onchain_wake import lane_wake
        _onchain_wake = lane_wake()
        _p("[memecoin] onchain lane wake: %s" % (
            "bound=%s" % _onchain_wake.stats["bound"] if hasattr(_onchain_wake, "stats")
            else "off (poll only)"))
    else:
        _onchain_wake = threading.Event()

    def _onchain_lane_worker():
        # A slow Solana confirmation must not hold up another network's independent wallet.
        # This thread owns the datagram only; each funded network owns its DB consumer and wake.
        from sniper.services.memecoin_onchain_lane import (
            start_network_workers, fanout_network_wake, wait_for_next_sweep, write_lane_heartbeat)
        from sniper.settings.memecoin import (
            MEMECOIN_ONCHAIN_LANE_MAX_AGE_S, MEMECOIN_ONCHAIN_LANE_POLL_S)
        from sniper.services.memecoin_real_runner import real_onchain_sell_mirror
        from sniper.settings.memecoin import onchain_sell_mirror_wallets
        wakes, worker_status, workers = start_network_workers(
            sorted(contexts), conn_factory=get_connection, real_hook=real_hook,
            now_fn=real_utc_now_iso, max_age_s=MEMECOIN_ONCHAIN_LANE_MAX_AGE_S,
            poll_s=MEMECOIN_ONCHAIN_LANE_POLL_S, finish_fn=finish_db_cycle, log_fn=_p,
            sell_hook=lambda c, row: real_onchain_sell_mirror(c, row, contexts, now_fn=real_utc_now_iso,
                                                              lane="onchain_sell_mirror"),
            sell_wallets_fn=onchain_sell_mirror_wallets)
        _p("[memecoin] onchain detection workers started: %s" % sorted(wakes))
        while True:
            wait_for_next_sweep(_onchain_wake, MEMECOIN_ONCHAIN_LANE_POLL_S)
            fanout_network_wake(wakes)
            snapshot = {network: dict(state) for network, state in worker_status.items()}
            for network, worker in zip(wakes, workers):
                snapshot[network]["alive"] = worker.is_alive()
            write_lane_heartbeat(cursor=max((state["cursor"] for state in snapshot.values()), default=0),
                now_iso=real_utc_now_iso(), wake=getattr(_onchain_wake, "stats", None), workers=snapshot)

    from sniper.settings.memecoin import MEMECOIN_ONCHAIN_LANE_ENABLED
    if MEMECOIN_ONCHAIN_LANE_ENABLED and real_hook is not None:
        threading.Thread(target=_onchain_lane_worker, daemon=True).start()
        _p("[memecoin] onchain detection lane: background thread started")
    elif MEMECOIN_ONCHAIN_LANE_ENABLED:
        _p("[memecoin] onchain detection lane: enabled but real leg is OFF — not started")

    # THE 4663 FAST LANE (M2, spec 2026-10-01-memecoin-4663-fast-lane-design.md §2.9). Off unless
    # MEMECOIN_RH_FAST_LANE=runner_shadow; per signal only: it sleeps on its socket in between.
    if contexts:
        from sniper.services.memecoin_rh_fast_lane import start_lane
        start_lane(contexts, log=_p)
        from sniper.services.memecoin_bsc_fast_lane import start_bsc_lane
        start_bsc_lane(contexts, log=_p)
        # THE BASE 8453 FAST LANE (step 1, plan 2026-10-14): off unless MEMECOIN_BASE_FAST_LANE is
        # runner_shadow or live AND its own host is set; never raises into the runner.
        from sniper.services.memecoin_base_fast_lane import start_base_lane
        start_base_lane(contexts, log=_p)

    # DIP HITS ON THEIR OWN THREAD (2026-09-30, operator order 86d053ce). A dip can last under a
    # second, and the main loop's period is 5 s, so a hit decided there was seconds late. The
    # on-chain lane was the other candidate host, but a hit is a full funded decision (seconds on
    # 4663) and would hold that lane's detections for as long. So: one thread, its own connection,
    # woken by the dip watch the moment a row is due, with a 1 s poll for expiries and a lost wake.
    # It calls the same hook the main loop and the on-chain lane already call concurrently.
    _dip_wake = threading.Event()

    def _dip_hit_worker():
        from sniper.persistence.db_core import get_connection
        from sniper.services.memecoin_real_runner import _run_dip_hits
        dconn = get_connection()
        _p("[memecoin] dip hit lane started")
        while True:
            # NOTHING IN HERE MAY END THE THREAD. If it does stop passing anyway, the main loop
            # takes the hits back after memecoin_dip_retry.LANE_STALE_S (5 s late, never lost).
            try:
                _dip_wake.wait(1.0)
                _dip_wake.clear()
                _run_dip_hits(dconn, real_hook, real_utc_now_iso(), host="dip_lane")
                finish_db_cycle(dconn)
            except Exception:
                _p("[err] dip hit lane pass failed:\n" + traceback.format_exc())
                finish_db_cycle(dconn, failed=True)
                time.sleep(1.0)

    def _pinned_reoffer_status():
        try:
            from sniper.services.memecoin_exit_rebroadcast import snapshot
            return snapshot()
        except Exception:
            return None                        # observability only; never costs a heartbeat

    def _dip_lane_status():
        try:
            from sniper.services.memecoin_dip_retry import lane_status
            return lane_status()
        except Exception:
            return None                        # observability only; never costs a heartbeat

    if real_hook is not None:
        from sniper.services.memecoin_dip_retry import expect_lane as _expect_dip_lane
        _expect_dip_lane()                     # from here a lane that never passes is stale too
        threading.Thread(target=_dip_hit_worker, daemon=True, name="memecoin-dip-hits").start()

    # A six-second retry must not wait behind a twenty-second main-loop sweep. Per-chain owners
    # keep a slow Solana confirmation independent of Robinhood/BSC; no new work on fresh buys.
    _entry_retries = None
    if real_hook is not None:
        from sniper.settings.memecoin import MEMECOIN_ENTRY_RETRY_WINDOW_S
        if MEMECOIN_ENTRY_RETRY_WINDOW_S > 0:
            from sniper.services.memecoin_entry_retry_workers import EntryRetryWorkers
            _entry_retries = EntryRetryWorkers(maintenance_contexts, conn_factory=get_connection,
                hook=real_hook, now_fn=real_utc_now_iso, finish_fn=finish_db_cycle)
            _entry_retries.start()
            _p("[memecoin] entry retry workers started: %s" % sorted(contexts))

    _PAPER_BRACKET_THREAD = not _FUNDED_ONLY and paper_bracket_thread_enabled()
    _p("[memecoin] paper bracket: %s" % ("disabled" if _FUNDED_ONLY else
       "own thread" if _PAPER_BRACKET_THREAD else "inline"))
    # PAPER EXIT TICKS (spec 2026-10-08): the onchain_ticks_v1 engine, its own thread and ledger
    # connection, never this loop. Always started: an open v1 position must keep being decided after
    # MEMECOIN_PAPER_EXIT_ENGINE goes back to v0 (rollback affects new entries only).
    _research_capture = start_paper_workers(
        funded_only=_FUNDED_ONLY, period_s=PUMP_PERIOD_S,
        connect=get_connection, now_fn=real_utc_now_iso)
    from sniper.services import memecoin_other_holdings as _oh_worker

    def _other_holdings_status():
        """Worker health plus enabled EVM chains with an actual execution context."""
        from sniper.services.memecoin_evm_holdings import supported_chains
        status = (dict(_oh_worker.worker_status() or {})
                  if _HOLDINGS_ENABLED and _oh_worker.thread_enabled() else {})
        status["evm_ready_chains"] = (sorted(
            chain for chain in supported_chains() if (contexts or {}).get(chain))
            if _HOLDINGS_ENABLED else [])
        return status

    # PAPER FAN-OUT, OFF THE FUNDED THREADS (2026-09-27). Both watchdog kills that day caught this
    # loop inside the paper copy trader's executable quote. Installed once, here, before the main
    # loop and every follow thread that submits to it. MEMECOIN_PAPER_FANOUT_THREAD=0: nothing is
    # installed and both call sites run the fan-out inline, as before.
    from sniper.services import memecoin_paper_fanout as _paper_fanout_mod

    if _J7_SOURCE:
        _p("[memecoin] follow-activity lanes (fomo REST + WS): not started, signal source is J7")
    elif os.getenv("MEMECOIN_FOLLOW_ACTIVITY_ENABLED", "1").strip().lower() in {"1", "true", "yes"}:
        from sniper.settings.memecoin import MEMECOIN_FOLLOW_SLOW_ENABLED
        if MEMECOIN_FOLLOW_SLOW_ENABLED and not _FUNDED_ONLY:
            threading.Thread(target=_activity_worker, daemon=True).start()
            _p("[memecoin] follow-activity SLOW lane (watch-only, 300s): background thread started")
        else:
            # Killed 2026-07-28: 85% of this lane's catches land outside the +EV <60s band by
            # construction. Global /feed still covers these wallets' buys AND sells.
            _p("[memecoin] follow-activity SLOW lane: DISABLED "
               "(MEMECOIN_FOLLOW_SLOW_ENABLED=0 — 85% of its catches were outside the +EV band)")
        if os.getenv("MEMECOIN_FOLLOW_FAST_ENABLED", "1").strip().lower() in {"1", "true", "yes"}:
            if real_hook is not None:
                for worker_index, work_queue in enumerate(_fomo_activity_queues):
                    threading.Thread(
                        target=_fomo_activity_dispatch_worker,
                        args=(work_queue,),
                        name="memecoin-fomo-activity-dispatch-%d" % worker_index,
                        daemon=True,
                    ).start()
                threading.Thread(
                    target=_fomo_activity_ws_worker,
                    name="memecoin-fomo-activity-ws",
                    daemon=True,
                ).start()
                _p("[memecoin] Fomo activity WebSocket: primary armed signal lane started "
                   "with 2 dispatch workers")
            threading.Thread(target=_fast_activity_worker, daemon=True).start()
            _p("[memecoin] follow-fast REST lane: recovery thread started")

    # TRIAL ARM GRADER (spec 2026-10-01-memecoin-trial-arm-design.md 5.3): off the entry path, its
    # own thread and connection, one pass a minute. With no trial rows a pass is one indexed read.
    from sniper.services.memecoin_trial import run_grader_forever as _trial_grader
    threading.Thread(target=lambda: _trial_grader(get_connection, now_iso=real_utc_now_iso),
                     name="memecoin-trial-grader", daemon=True).start()
    _p("[memecoin] trial grader: own thread started (period 60s)")

    # SLOT LANE (2026-09-26, operator: fill at the source slot S or S+1). Default OFF; see
    # docs/superpowers/specs/2026-09-26-solana-slot-lane-design.md. Its own threads and
    # connections; a start failure is logged and leaves every other lane untouched.
    from sniper.services.memecoin_slot_lane import lane_enabled as _slot_lane_enabled
    # SOURCE-SELL MIRROR (2026-09-27, operator order). Created before the slot lane so its socket
    # feeds it sells and a tap of own buys; its own thread starts once the funded exit lock exists.
    from sniper.services.memecoin_source_sell_mirror import SourceSellMirror
    from sniper.settings.memecoin import (
        MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION as _SSM_MIN,
        MEMECOIN_SOURCE_SELL_MIRROR_ENABLED as _SSM_ON,
        MEMECOIN_SOURCE_SELL_MIRROR_SHADOW as _SSM_SHADOW,
        solana_read_rpc as _ssm_read_rpc,
    )
    _ssm_url = _ssm_read_rpc()
    _source_mirror = SourceSellMirror(
        enabled=True, shadow=_SSM_SHADOW, min_fraction=_SSM_MIN,
    ) if (_SSM_ON and contexts.get("solana")) else None
    from sniper.settings.memecoin import (
        MEMECOIN_SOURCE_SELL_ACCOUNT_WATCH_ENABLED as _SSM_ACCOUNTS)
    _account_price_stream = None
    _source_partials = None
    _slot_lane_status = {'mode': 'staged', 'startup_gate': _shyft_startup.status}
    def _slot_lane_snapshot():
        return {**(_slot_lane_status or {}),
                'source_partials': (_source_partials.status() if _source_partials is not None else None),
                'solana_volume_gate': solana_volume_status_for_contexts(contexts)}

    def _start_solana_streams():
        nonlocal _slot_lane_status
        if _source_mirror is not None and _SSM_ACCOUNTS and _ssm_url:
            # Routed sellers: the source owner's token account at processed, on the keyless read RPC.
            from sniper.services.memecoin_solana_swap_marks import ws_url_from_read_rpc
            from sniper.services.memecoin_source_sell_mirror import OwnerAccountWatch
            from sniper.services.memecoin_solana_provider import infra_provider
            from sniper.services.memecoin_shyft_signal import live_config
            _source_account_shyft = live_config() if infra_provider() == "shyft" else None
            _source_mirror.account_watch = OwnerAccountWatch(
                ws_url=None if _source_account_shyft else ws_url_from_read_rpc(_ssm_url), rpc_url=_ssm_url,
                sink=_source_mirror.offer_account, on_ack=_source_mirror.offer_ack,
                shyft_config=_source_account_shyft, on_gap=_source_mirror.offer_account_gap,
                on_baseline=_source_mirror.offer_account_baseline)
            _source_mirror.account_watch.start()
        _slot_lane_status = None
        if _slot_lane_enabled() and real_hook is not None and contexts.get("solana"):
            try:
                from sniper.services.memecoin_slot_lane import start_from_context as _slot_start
                _slot_lane_status = _slot_start(contexts["solana"], log=_p, mirror=_source_mirror,
                                                onchain_wake=_onchain_wake,
                                                receipt_resume_ready=lambda offer: setattr(real_hook, 'receipt_resume', offer))
                _p("[memecoin] slot lane: started")
                # Fill timing (2026-10-02): the lane's skip reasons, drained every second into the
                # timing file (never the ledger).
            except Exception:
                _p("[err] slot lane failed to start:\n%s" % traceback.format_exc())
        elif _slot_lane_enabled():
            _p("[memecoin] slot lane: enabled but no Solana funded context -- not started")

        _source_receipt_journals = (_slot_lane_status or {}).get('_receipt_journals', ())
        _execution_observer_status = {}
        try:
            _execution_journal = start_execution_measurement(contexts, journals=_source_receipt_journals,
                                                            status=_execution_observer_status)
        except Exception:
            _execution_journal = None
            _execution_observer_status.update(state='unknown', reason='observer_start_failed')
        if _slot_lane_status is not None or _execution_journal is not None:
            if _slot_lane_status is None:
                _slot_lane_status = {'mode': 'off'}
            _slot_lane_status['execution_observer'] = _execution_observer_status
            from sniper.services.memecoin_slot_skips import run_drain as _skip_drain
            threading.Thread(target=_skip_drain, args=(threading.Event(),),
                kwargs={'journals': (*_source_receipt_journals,
                                    *((_execution_journal,) if _execution_journal is not None else ()))},
                name="memecoin-slot-skips", daemon=True).start()

        if (_slot_lane_status is not None and _slot_lane_status.get("mode") == "live"
                and _BUFFER_CAP > 0 and _rent_keypair is not None):
            from sniper.services.memecoin_sol_buffer_watch import BufferWatch
            from sniper.settings import memecoin as _sbw_cfg
            from sniper.util.state_paths import state_path as _sbw_path
            _sol_buffer_box["watch"] = BufferWatch(
                _sbw_path("data", "sol_buffer_page.json"), below_usd=_sbw_cfg.MEMECOIN_SOL_BUFFER_STALL_BELOW_USD,
                stall_s=_sbw_cfg.MEMECOIN_SOL_BUFFER_STALL_PAGE_S,
                clip_fn=lambda: (_slot_lane_status or {}).get("clip_usd"))

            def _sol_buffer_worker():
                from sniper.services.memecoin_gas_topup import buffer_topup_once
                from sniper.services.memecoin_slot_lane import sol_usd_from_jupiter
                from sniper.settings.memecoin import MEMECOIN_SOLANA_RPC_URL
                from sniper.util.clock import real_utc_now_iso
                sc = contexts["solana"]
                def native_balance():
                    got = sc["sol_fn"]()
                    return got.balance if getattr(got, "ok", False) else None
                time.sleep(min(30.0, _BUFFER_PERIOD))
                while True:
                    if not _custody_ready():
                        time.sleep(_BUFFER_PERIOD)
                        continue
                    worker_conn = None
                    try:
                        worker_conn = get_connection()
                        tally = buffer_topup_once(
                            worker_conn, sol_balance=native_balance(), usdc_balance=sc["usdc_fn"](),
                            sol_usd=sol_usd_from_jupiter(), keypair=_rent_keypair,
                            pubkey=str(sc["pubkey"]), rpc_url=MEMECOIN_SOLANA_RPC_URL,
                            now_iso=real_utc_now_iso(), cap_usd=_BUFFER_CAP, topup_usd=_BUFFER_TOPUP,
                            daily_cap_usd=_BUFFER_DAILY, min_usdc_remaining_usd=_BUFFER_KEEP,
                            sol_balance_fn=native_balance, usdc_balance_fn=sc["usdc_fn"],
                            send_fn=lambda signed, url: sc['rpc_send_fn'](signed, url, classify_preflight=True),
                            confirm_fn=sc.get('rpc_confirm_fn'))
                        if tally.get("state") not in ("buffer_cap", "insufficient_free_usdc", "wallet_busy"):
                            _p("[memecoin] SOL execution buffer: %s" % tally)
                        _sol_buffer_box["watch"].observe(tally)     # quiet states included (2026-10-21)
                    except Exception:
                        _p("[err] SOL execution buffer:\n" + traceback.format_exc())
                    finally:
                        if worker_conn is not None:
                            worker_conn.close()
                    time.sleep(_BUFFER_PERIOD)
            threading.Thread(target=_sol_buffer_worker, name="memecoin-sol-buffer",
                             daemon=True).start()
            _p("[memecoin] SOL execution buffer: maintenance thread started")

        if _slot_lane_status is None and _account_price_stream is not None:
            _slot_lane_status = {'mode': 'off'}
        if _slot_lane_status is not None:
            if _account_price_stream is not None:
                _slot_lane_status['account_stream'] = _account_price_stream.status
            _slot_lane_status['startup_gate'] = _shyft_startup.status
    _shyft_startup.defer(_start_solana_streams, name='shyft-funded-stream-startup')

    def _blind_capture_worker():
        # Blind-chain probe capture (2026-07-16): 4663 is DexScreener-blind (545k/545k
        # missing_pair), so its forward tape comes from bounded GeckoTerminal batches here — its
        # own thread + connection because the poll loop's entry latency is a
        # measured edge (follow fast-lane). WAL + small commits; never touches copy/exit state.
        from sniper.persistence.db_core import get_connection
        from sniper.services.memecoin_capture_v2 import capture_blind_pass
        from sniper.settings.memecoin import MEMECOIN_CAPTURE_BLIND_PERIOD_S
        from sniper.util.clock import real_utc_now_iso
        bconn = get_connection()
        while True:
            try:
                bres = capture_blind_pass(bconn, real_utc_now_iso())
                if bres.requested:
                    _p("[memecoin] blind capture: %d/%d priced (eligible %d, over-budget %d)"
                       % (bres.captured, bres.requested, bres.eligible, bres.budget_skipped))
            except Exception:
                finish_db_cycle(bconn, failed=True)
                _p("[err] blind capture pass failed:\n" + traceback.format_exc())
            else:
                finish_db_cycle(bconn)
            time.sleep(max(5.0, float(MEMECOIN_CAPTURE_BLIND_PERIOD_S)))

    from sniper.settings.memecoin import (
        MEMECOIN_CAPTURE_BLIND_ENABLED, MEMECOIN_CAPTURE_V2_ENABLED as _CAP_V2,
        MEMECOIN_DEX_BLIND_CHAINS as _BLIND)
    from sniper.services.memecoin_fastlane import (
        FastlaneState, MidObservationState, RecentSwapMarks, ensure_fastlane_thread,
        fastlane_wait_s, has_priority_tp_confirmation, run_fastlane_exit_sweep,
        run_fastlane_worker_cycle, run_slow_exit_sweep, WatchedLock, watch_exit_lock,
    )
    from sniper.services.memecoin_solana_swap_marks import SolanaSwapMarkCache
    from sniper.services import memecoin_live_marks as _live_marks
    from sniper.settings.memecoin import (
        MEMECOIN_FASTLANE_DEADLINE_SLEEP_MAX_BOOK as _FASTLANE_DEADLINE_MAX_BOOK,
        MEMECOIN_FASTLANE_ENABLED as _FASTLANE,
        MEMECOIN_FASTLANE_TICK_S as _FASTLANE_TICK_S,
        MEMECOIN_SOLANA_SWAP_MARK_ENABLED as _SOL_SWAP_MARKS,
    )
    _fastlane_state = FastlaneState()
    # Rail wake (hot TP lane 2026-08-22; every rail 2026-09-27): a Solana mark that says a price
    # rail was probably crossed wakes the fastlane worker for a FOCUS pass on that position.
    # Timing only; the exact quote and the sweep still decide.
    from sniper.services.memecoin_hot_tp import HotTpTrigger
    from sniper.settings.memecoin import (
        MEMECOIN_HOT_TP_ENABLED as _HOT_TP,
        MEMECOIN_HOT_TP_MIN_INTERVAL_S as _HOT_TP_MIN_S,
        MEMECOIN_RAIL_WAKE_BIG_MOVE_PCT as _WAKE_BIG_MOVE_PCT,
        MEMECOIN_RAIL_WAKE_MAX_PER_MINUTE as _WAKE_MAX_PER_MINUTE,
        MEMECOIN_RAIL_WAKE_SHADOW as _WAKE_SHADOW,
    )
    _hot_tp = HotTpTrigger(min_interval_s=_HOT_TP_MIN_S, big_move_pct=_WAKE_BIG_MOVE_PCT,
                           max_per_minute=_WAKE_MAX_PER_MINUTE, shadow=_WAKE_SHADOW) \
        if _HOT_TP else None
    _swap_mark_cache = SolanaSwapMarkCache(
        on_mark=_hot_tp.on_mark if _hot_tp is not None else None)
    from sniper.services.memecoin_real_runner import configure_native_exit_hint
    configure_native_exit_hint(contexts.get("solana"), _swap_mark_cache if _SOL_SWAP_MARKS else None,
                               now_fn=real_utc_now_iso)
    if _FASTLANE and _SOL_SWAP_MARKS and contexts.get('solana'):
        from sniper.services.memecoin_solana_provider import infra_provider
        if infra_provider() == 'shyft':
            import atexit
            from sniper.services.memecoin_shyft_signal import live_config
            from sniper.services.memecoin_solana_swap_marks import AccountPriceStream
            _account_token, _account_urls = live_config()
            _account_price_stream = AccountPriceStream(_account_urls[0], _account_token)
            _shyft_startup.defer(_account_price_stream.start, name='shyft-account-price-startup')
            atexit.register(_account_price_stream.close)
            if _slot_lane_status is None:
                _slot_lane_status = {'mode': 'off'}
            _slot_lane_status['account_stream'] = _account_price_stream.status
    _paper_swap_marks = RecentSwapMarks()
    _mid_observation_state = MidObservationState()
    _funded_exit_lock = WatchedLock()
    # A HOLD PAST MEMECOIN_EXIT_LOCK_DUMP_S LOGS ITS HOLDER'S STACK, once per episode, one stamped
    # line per frame (2026-09-27: a ~39 s hold on 66015 left no stack to say what it waited on).
    _exit_lock_dump_s = float(os.getenv("MEMECOIN_EXIT_LOCK_DUMP_S", "10") or 0)
    if _exit_lock_dump_s > 0:
        threading.Thread(target=watch_exit_lock, name="exit-lock-watch", daemon=True,
                         args=(_funded_exit_lock,),
                         kwargs=dict(hold_s=_exit_lock_dump_s, log=_p)).start()
    if _source_mirror is not None:
        # Its own thread and connection: a source sell is handled the moment it arrives and waits
        # only for the exit lock, never for a fastlane pass to finish.
        from sniper.persistence.db_core import get_connection as _ssm_connect
        threading.Thread(target=_source_mirror.serve_forever, name="source-sell-mirror",
                         daemon=True, kwargs=dict(conn_factory=_ssm_connect, contexts=contexts,
                                                  exit_lock=_funded_exit_lock, log=_p)).start()
        _p("[memecoin] source-sell mirror: thread started mode=%s"
           % ("shadow" if _source_mirror.shadow else "live"))
    _fastlane_stale_s = max(5.0, float(_FASTLANE_TICK_S) * 3.0)

    def _solana_swap_mark_worker():
        import asyncio

        from sniper.persistence import db_ticks
        from sniper.services.memecoin_fastlane import open_swap_mark_positions
        from sniper.services.memecoin_solana_amounts import get_transaction_fn
        from sniper.services.memecoin_solana_swap_marks import (
            bind_open_positions,
            build_price_book,
            curve_seed_fn,
            vault_seed_fn,
            epoch_summary,
            run_price_stream_epoch,
            ws_url_from_read_rpc,
            account_mint,
        )
        from sniper.settings.memecoin import solana_read_rpc

        try:
            observer = connect_observer_main()
            # WRITABLE, and only for the tape file: this lane binds the position's pool itself
            # rather than waiting a median 276 s for the 300 s tape pass. The MAIN ledger handle
            # above stays read-only.
            sol_conn = db_ticks.connect_sol_tape()
        except Exception:
            _p(
                "[warn] Solana swap-mark lane: tape database unavailable; "
                "DexScreener fallback active"
            )
            return
        from sniper.services.memecoin_solana_provider import provider_urls
        read_url = provider_urls('read')[0] if _account_price_stream is not None else solana_read_rpc()
        ws_url = ws_url_from_read_rpc(read_url)
        if ws_url is None:
            _p(
                "[warn] Solana swap-mark lane: read WebSocket unavailable; "
                "DexScreener fallback active"
            )
            return
        # COMMITMENT (2026-08-23): this worker resolves a funded position's pool from OUR OWN
        # entry signature, and `getTransaction` defaults to `finalized` — about 32 slots (~13 s)
        # behind the fill. That default was a hard floor under every bind: the 91 funded Solana
        # binds on record have a MINIMUM lag of 13.8 s (p25 16.2 / median 17.6), and
        # BIND_MAX_ATTEMPTS=5 is spent from a 5 s spin, so a position can give up before its own
        # entry becomes readable. 37 of 46 unbound positions never bound at all despite having an
        # entry tx_sig, and they carry the SHORTEST holds (median 170.9 s vs 326.4 s for bound
        # ones) — the exits that most need speed ran on a ~10.3 s DexScreener mid instead of the
        # ~2.5 s swap lane. `confirmed` is queryable about 1 s after the fill and is correct here:
        # it reads a transaction that ALREADY LANDED to name a pool to OBSERVE. It signs nothing,
        # sizes nothing, and decides no rail. Scoped to this call site on purpose — the WO-2 sale
        # authority in memecoin_hot_supervisor builds its own reader and keeps the RPC default.
        get_tx_fn = get_transaction_fn(read_url, commitment="confirmed")
        bind_attempts = {}

        def _price_book(live):
            book = build_price_book(live, sol_conn)
            from sniper.settings import memecoin as _native_cfg
            if _native_cfg.MEMECOIN_SOLANA_NATIVE_EXIT or _native_cfg.MEMECOIN_SOLANA_NATIVE_RUNG:
                from sniper.services.memecoin_native_exit_rung import virtual_quote
                for binding in book[1].values():
                    if binding.pool != binding.quote_vault:    # AMM; curve identity is not a PumpSwap pool
                        virtual_quote(binding.pool)
            return book

        while True:
            try:
                positions = open_swap_mark_positions(observer)
                if get_tx_fn is not None:
                    bind_open_positions(
                        sol_conn, observer, positions, get_tx_fn,
                        attempts=bind_attempts,
                    )
                vaults, bindings = _price_book(positions)
                if not vaults:
                    if _account_price_stream is not None:
                        _account_price_stream._watched = ()
                    _swap_mark_cache.invalidate()
                    time.sleep(5.0)
                    continue
                _p(f"[memecoin] {epoch_summary(vaults, bindings)}")

                def _refresh():
                    # Runs on the epoch's event-loop thread, which IS this worker thread, so the
                    # SQLite handles below stay legal (check_same_thread=True).
                    live = open_swap_mark_positions(observer)
                    if get_tx_fn is not None:
                        bind_open_positions(
                            sol_conn, observer, live, get_tx_fn, attempts=bind_attempts)
                    return _price_book(live)

                stream_kwargs = {}
                if _account_price_stream is not None:
                    from functools import partial
                    stream_kwargs['stream_fn'] = partial(_account_price_stream.stream, bindings=bindings)
                expected_mint_fn = ((lambda account: account_mint(account, vaults, bindings))
                                    if _account_price_stream is not None else None)
                asyncio.run(
                    run_price_stream_epoch(
                        vaults,
                        bindings,
                        _swap_mark_cache,
                        ws_url=ws_url,
                        duration_s=30.0,
                        refresh_fn=_refresh,
                        # Pump curves push only on change: one seed read per acked curve per epoch
                        # (<= MEMECOIN_MAX_CONCURRENT curves x 2 epochs/min) on the read RPC.
                        seed_fn=curve_seed_fn(read_url, expected_mint_fn=expected_mint_fn),
                        # AMM vaults too (operator 2026-09-27, "fix the reconnect"): without a seed
                        # the first trade after every rollover was only a baseline. One read per
                        # acked vault per epoch on the same read RPC. MEMECOIN_SOL_VAULT_SEED=0 off.
                        vault_seed_fn=(vault_seed_fn(read_url, expected_mint_fn=expected_mint_fn)
                                       if os.getenv("MEMECOIN_SOL_VAULT_SEED", "1").strip().lower()
                                       not in {"0", "false", "no", "off"} else None),
                        **stream_kwargs,
                    )
                )
            except Exception:
                _swap_mark_cache.invalidate()
                _p(
                    "[warn] Solana swap-mark lane: socket fault; "
                    "DexScreener fallback active"
                )
                time.sleep(1.0)

    def _fastlane_worker():
        # Open-position quote fast lane (2026-07-22, Phase 2 Unit A/C): 4663 open-position marks
        # arrive at p50 56.5s because GeckoTerminal is their only source, and GT's budget is
        # shared with every other chain. This thread prices ONLY open real positions (bounded by
        # MEMECOIN_MAX_CONCURRENT) straight off the chain via QuoterV2 eth_call — no GT calls, so
        # no 429 pressure anywhere else.
        #
        # The setting selects shadow or funded decisions. The worker has its own connection because
        # the poll loop must not absorb RPC time.
        def _setup():
            fconn = None
            try:
                from sniper.persistence.db_core import get_connection as _get_connection
                from sniper.services.memecoin_exits import (
                    filter_implausible_ticks, write_marks)
                from sniper.services.memecoin_fastlane import (
                    build_demand_fn, rearm_rail_wake, refresh_open_positions)
                from sniper.services.memecoin_real_runner import real_exit_sweep as _exit_sweep
                from sniper.util.clock import real_utc_now_iso as _now_iso
                fconn = _get_connection()
                return {
                    "conn": fconn,
                    "demand_fn": build_demand_fn(fconn),
                    "filter_fn": filter_implausible_ticks,
                    "write_fn": write_marks,
                    "refresh_fn": refresh_open_positions,
                    "rearm_fn": rearm_rail_wake,
                    "exit_fn": _exit_sweep,
                    "now_fn": _now_iso,
                    "priority_tp_fn": has_priority_tp_confirmation,
                    "mid_observation_state": _mid_observation_state,
                }
            except Exception:
                if fconn is not None:
                    fconn.close()
                raise

        def _close(resource):
            fconn = resource["conn"]
            finish_db_cycle(fconn, failed=True)
            fconn.close()

        def _pass(resource, focus_ids=None):
            fconn = resource["conn"]
            owner_contexts = {k: v for k, v in contexts.items() if k != "1"}
            previews = {}
            if focus_ids is None:
                # The open-book size this pass saw, for the deadline pacing below. Cleared first so
                # a pass that dies before the refresh cannot leave the previous count behind.
                resource["open_book"] = None
                stats = {}
                pm = resource["refresh_fn"](
                    fconn,
                    owner_contexts,
                    preview_by_position=previews,
                    demand_fn=resource["demand_fn"],
                    swap_mark_cache=_swap_mark_cache if _SOL_SWAP_MARKS else None,
                    mid_observation_state=resource["mid_observation_state"],
                    now_iso_fn=resource["now_fn"],
                    stats_out=stats,
                )
                resource["open_book"] = stats.get("deadline_positions")
                resource["quote_usd"] = stats.get("quote_usd") or {}
                sweep_scope = {}
            else:
                # A rail wake's FOCUS pass: the sweep takes its own exact quote for the woken
                # positions only, priced for display from the swap-mark cache with the last full
                # pass's quote USD. No DexScreener batch; an empty map must still reach the sweep,
                # because a missing price_fn makes the sweep fetch one.
                pm = (_swap_mark_cache.fresh_price_map(
                    resource.get("quote_usd") or {}, now_ts=time.monotonic(), max_age_s=5.0)
                    if _SOL_SWAP_MARKS else {})
                sweep_scope = {"position_ids": focus_ids}
            # Only live open-position marks may enter the funded decision map. Filter before
            # any persistence, but leave observational writes and post-close reads after exits.
            tick_ts = resource["now_fn"]()
            if pm:
                pm = resource["filter_fn"](fconn, pm, tick_ts)
                if pm:
                    _paper_swap_marks.publish(pm, now_ts=time.monotonic())
            priced = bool(pm) or focus_ids is not None
            if contexts and MEMECOIN_FASTLANE_DECIDES:
                run_fastlane_exit_sweep(
                    state=_fastlane_state,
                    exit_lock=_funded_exit_lock,
                    sweep_fn=lambda: resource["exit_fn"](
                        fconn, owner_contexts, network_ids=set(owner_contexts),
                        price_fn=(lambda _mints, _pm=pm: _pm) if priced else None,
                        preview_by_position=previews,
                        demand_fn=resource["demand_fn"],
                        lane="fastlane_poll",
                        reserve_witness_cache=_swap_mark_cache if _SOL_SWAP_MARKS else None,
                        **sweep_scope,
                    ),
                )
            if _hot_tp is not None:
                try:                                   # the wake never costs the pass
                    resource["rearm_fn"](fconn, _hot_tp)
                except Exception:
                    _p("[warn] rail wake rearm failed (tick unaffected):\n"
                       + traceback.format_exc())
            # Post-close ratios are observation-only. A delayed/locked tape cannot hold up this
            # pass's funded exit, and a re-entered coin cannot inherit its former bag's mark.
            recorded = {
                key: {**entry, "observed_at": entry.get("observed_at") or tick_ts}
                for key, entry in pm.items()
            }
            try:
                from sniper.services.memecoin_evm_post_close_marks import evm_post_close_marks
                post_close = (evm_post_close_marks(fconn, now_iso=resource["now_fn"]())
                              if focus_ids is None else None)
                if post_close:
                    post_close = resource["filter_fn"](fconn, post_close, resource["now_fn"]())
                    for key, entry in post_close.items():
                        recorded.setdefault(key, entry)
            except Exception:
                _p("[warn] evm post-close marks failed (fast lane unaffected)")
            if recorded:
                resource["write_fn"](fconn, recorded, resource["now_fn"]())
            if not finish_db_cycle(fconn):
                raise RuntimeError("fastlane DB cycle failed")

        def _print_wake_records(records):
            # One line per wake, printed here because the stream thread must not do I/O. The
            # mark's slot joins to the deciding quote's context_slot in memecoin_route_series.
            for rec in records:
                _p("[memecoin] rail wake%s mint=%s reason=%s ratio=%.4f slot=%s src=%s ids=%s "
                   "mark_to_wake_ms=%.0f wake_to_now_ms=%.0f"
                   % (" (shadow)" if rec["shadow"] else "", rec["mint"][:8], rec["reason"],
                      rec["ratio"], rec["slot"], rec["source"],
                      ",".join(str(i) for i in rec["ids"]),
                      (rec["woke_mono"] - rec["mark_ts"]) * 1000.0 if rec["mark_ts"] else -1.0,
                      (time.monotonic() - rec["woke_mono"]) * 1000.0))

        resource = None
        while True:
            _pass_started = time.monotonic()
            resource, _ok = run_fastlane_worker_cycle(
                resource,
                setup_fn=_setup,
                pass_fn=_pass,
                close_fn=_close,
                state=_fastlane_state,
                error_fn=lambda _exc: _p(
                    "[err] fastlane pass failed; slow exit fallback active:\n"
                    + traceback.format_exc()),
            )
            if _ok and resource["priority_tp_fn"](resource["conn"]):
                continue
            # A failed cycle returns no resource, so the book size is unknown and the old floor
            # stands: an unknown book never buys the faster period.
            tick = fastlane_wait_s(
                _FASTLANE_TICK_S,
                started_at=_pass_started,
                now=time.monotonic(),
                open_book=(resource or {}).get("open_book") if _ok else None,
                max_book=_FASTLANE_DEADLINE_MAX_BOOK,
            )
            if _hot_tp is None:
                time.sleep(tick)
                continue
            # Rail wakes run FOCUS passes inside the tick; the next full pass keeps its schedule.
            _full_due = time.monotonic() + tick
            while True:
                _print_wake_records(_hot_tp.take_records())
                _left = _full_due - time.monotonic()
                if _left <= 0 or not _hot_tp.wait(_left):
                    break
                _focus = _hot_tp.take_woken()
                if not _focus:
                    continue
                _focus_started = time.monotonic()
                resource, _ok = run_fastlane_worker_cycle(
                    resource,
                    setup_fn=_setup,
                    pass_fn=lambda _r, _ids=_focus: _pass(_r, focus_ids=_ids),
                    close_fn=_close,
                    state=_fastlane_state,
                    error_fn=lambda _exc: _p(
                        "[err] fastlane focus pass failed:\n" + traceback.format_exc()),
                )
                _p("[memecoin] rail wake focus pass ids=%s ok=%s pass_ms=%.0f"
                   % (",".join(str(i) for i in sorted(_focus)), _ok,
                      (time.monotonic() - _focus_started) * 1000.0))
                if not _ok:
                    break

    if not _FUNDED_ONLY and _CAP_V2 and MEMECOIN_CAPTURE_BLIND_ENABLED and _BLIND:
        threading.Thread(target=_blind_capture_worker, daemon=True).start()
        _p("[memecoin] blind-chain probe capture (%s): background thread started"
           % ",".join(sorted(_BLIND)))

    if _FASTLANE and _SOL_SWAP_MARKS:
        _shyft_startup.defer(lambda: threading.Thread(
            target=_solana_swap_mark_worker, name="memecoin-solana-swap-mark", daemon=True).start(),
            name='shyft-swap-marks-startup')
        _p("[memecoin] Solana swap-mark lane: read-only thread started")
        # LIVE DISPLAY MARKS (S27-05): its own thread copies the swap and curve marks to a tmpfs
        # snapshot the web ticker reads. Display only: no exit, tape, rail wake or order reads
        # it. MEMECOIN_LIVE_MARKS_PUBLISH=0 turns it off and the web serves the tape.
        if _live_marks.enabled():
            from sniper.util.clock import real_utc_now_iso as _live_now_iso
            # ON-CHAIN LIQUIDITY: the pool owner program is read once per held pool on its own
            # thread (2 s wall-clock deadline) and decides whether the quote vault is the depth.
            # MEMECOIN_LIVE_MARKS_LIQUIDITY=0: no owner reads, the web shows the stored provider
            # liquidity, labelled.
            _pool_owners = None
            if _live_marks.liquidity_enabled():
                from sniper.services.memecoin_solana_swap_marks import pool_owner_fn
                from sniper.settings.memecoin import solana_read_rpc as _owner_read_rpc
                _pool_owners = _live_marks.PoolOwnerBook(pool_owner_fn(_owner_read_rpc()))
                threading.Thread(target=_pool_owners.run, args=(threading.Event(),),
                                 name="memecoin-live-mark-pool-owners", daemon=True).start()
            _live_pub = _live_marks.LiveMarkPublisher(
                _swap_mark_cache, quote_usd_fn=_live_marks.onchain_quote_usd,
                now_iso_fn=_live_now_iso, carry=_live_marks.carry_enabled(),
                owner_book=_pool_owners)
            threading.Thread(target=_live_pub.run, args=(threading.Event(),), kwargs={"log": _p},
                             name="memecoin-live-marks", daemon=True).start()
            # Quote assets other than SOL and USDC, priced through Jupiter every 10 s (operator,
            # 2026-09-27: "Jupiter"). Its own thread: a Jupiter call never stalls the publisher.
            _odd_quote_refresher = _live_marks.ExoticQuoteRefresher(_swap_mark_cache,
                                                            now_iso_fn=_live_now_iso)
            threading.Thread(target=_odd_quote_refresher.run, args=(threading.Event(),),
                             name="memecoin-live-mark-quotes", daemon=True).start()
            _p("[memecoin] live display marks: publisher thread started path=%s carry=%s liquidity=%s"
               % (_live_pub.path, _live_pub.carry, _pool_owners is not None))

    _fastlane_thread = None
    if _FASTLANE:
        _fastlane_thread = ensure_fastlane_thread(
            _fastlane_thread, target=_fastlane_worker, state=_fastlane_state)
        _p("[memecoin] open-position fast lane: thread started mode=%s"
           % ("decides" if MEMECOIN_FASTLANE_DECIDES else "shadow"))

    def _fastlane_status():
        price_status = {"thread_alive": None, "ready": None, "last_error": "unavailable"}
        price_peek = (contexts.get("56") or {}).get("quote_price_peek_fn")
        memo_status = getattr(price_peek, "status", None)
        if callable(memo_status):
            try:
                price_status = memo_status()
            except Exception as exc:
                price_status["last_error"] = type(exc).__name__
        if not _FASTLANE:
            return {"status": "disabled", "thread_alive": False,
                    "restart_count": 0, "last_success_age_s": None,
                    "last_failure_age_s": None, "failure": None,
                    "fallback_active": False, "bsc_price_preparation": price_status}
        snap = _fastlane_state.snapshot(stale_after_s=_fastlane_stale_s)
        snap["bsc_price_preparation"] = price_status
        snap["hot_tp"] = _hot_tp.snapshot() if _hot_tp is not None else {"status": "disabled"}
        snap["source_sell_mirror"] = (_source_mirror.snapshot() if _source_mirror is not None
                                      else {"status": "disabled"})
        # When each held coin's market price was last CHECKED, beside when it last changed
        # (docs/audits/2026-09-26-price-freshness.md). Same clock as the stamps it compares.
        from sniper.util.clock import real_utc_now_iso
        snap["mid_observations"] = _mid_observation_state.snapshot(now_iso=real_utc_now_iso())
        return snap

    def _tickstream_evm_worker():
        # Tick-stream ingest v0 (2026-07-23): raw swap tape, observation ONLY, own DB file so the
        # firehose never touches sniper_tracker.db's WAL. 4663 = whole-chain tape; 8453 = watched
        # pools (open real Base positions). Fail-closed: an RPC error skips the tick and resumes
        # from the persisted cursor — gaps, never inventions.
        from datetime import datetime, timedelta, timezone
        from sniper.execution.evm_wallet import _rpc
        from sniper.persistence import db_ticks
        from sniper.services.memecoin_tick_stream import (
            TickRetryBackoff, chain_tick_period, evm_tick_pass, watched_base_pools,
        )
        from sniper.settings import memecoin as _cfg
        from sniper.util.clock import real_utc_now_iso
        tconn = db_ticks.connect_ticks()
        mconn = connect_observer_main()

        def call_for(chain):
            rpc = _cfg.evm_tickstream_rpc_for_chain(chain)
            if not rpc:
                return None

            def call(method, params):
                resp = _rpc(rpc, method, params)
                if not isinstance(resp, dict) or "result" not in resp:
                    raise RuntimeError(f"rpc {method} error: {resp!r:.200}")
                return resp["result"]
            return call

        calls = {"4663": call_for("4663"), "8453": call_for("8453")}
        tick_s = max(1.0, float(_cfg.MEMECOIN_TICKSTREAM_TICK_S))
        retry = TickRetryBackoff(base_s=tick_s, cap_s=60.0)
        last_attempt = {}
        last_prune = 0.0
        # SKIP A CHAIN NOBODY IS ARMED ON (2026-08-25, operator). 4663 is a WHOLE-CHAIN tape, so
        # it polls the Robinhood public RPC continuously whether or not we can trade there. With
        # every armed wallet solana-only and no funded 4663 position since 2026-08-17, that tape
        # produced 1,729 of the runner's 1,795 HTTP 429s — burning a keyless public endpoint to
        # collect a tape for a chain we cannot enter. The skip is re-evaluated every pass, so
        # arming a wallet on 4663 resumes it within one tick with no restart.
        # 8453 is unaffected in practice: it tapes WATCHED POOLS from open Base positions, so it
        # is already position-driven and idle when there are none.
        skip_disarmed = str(getattr(_cfg, "MEMECOIN_TICKSTREAM_SKIP_DISARMED_CHAINS", "1")).strip(
            ).lower() in {"1", "true", "yes", "on"}
        last_skip_log = {}

        def chain_is_armed(chain):
            if not skip_disarmed:
                return True
            try:
                from sniper.persistence import db_memecoin as _m
                return bool(_m.real_approved_user_ids(mconn, network_id=chain))
            except Exception:
                return True                 # cannot tell -> keep taping, never go dark silently

        while True:
            now_iso = real_utc_now_iso()
            for chain in ("4663", "8453"):
                pass_started = time.monotonic()
                if not retry.ready(chain, pass_started):
                    continue
                if not chain_is_armed(chain):
                    # Log at most hourly per chain: silence would look identical to a dead thread.
                    if pass_started - last_skip_log.get(chain, -1e9) > 3600:
                        last_skip_log[chain] = pass_started
                        _p("[memecoin] tickstream %s: SKIPPED, no wallet armed on this chain "
                           "(re-checked every pass; arming one resumes it)" % chain)
                    continue
                if calls[chain] is None:
                    if pass_started - last_skip_log.get(chain, -1e9) > 3600:
                        last_skip_log[chain] = pass_started
                        _p("[memecoin] tickstream %s: SKIPPED, no dedicated observation RPC"
                           % chain)
                    continue
                period = chain_tick_period(
                    chain, tick_s, _cfg.MEMECOIN_TICKSTREAM_4663_TICK_S)
                if pass_started - last_attempt.get(chain, -period) < period:
                    continue
                last_attempt[chain] = pass_started
                watched = None if chain == "4663" else watched_base_pools(mconn)
                try:
                    result = evm_tick_pass(
                        tconn, chain, calls[chain], now_iso, watched_pools=watched)
                except Exception:
                    result = None
                    _p("[err] tickstream %s pass failed:\n%s"
                       % (chain, traceback.format_exc()))
                delay = retry.record(chain, result, time.monotonic())
                if delay > tick_s:
                    _p("[warn] tickstream %s: retry backoff %.0fs" % (chain, delay))
            if time.time() - last_prune > 86400:
                last_prune = time.time()
                try:
                    keep = {r[0] for r in mconn.execute(
                        "SELECT DISTINCT lower(f.quote_route_target) FROM memecoin_fills f "
                        "WHERE f.quote_route_target IS NOT NULL")}
                    horizon = (datetime.now(timezone.utc)
                               - timedelta(days=float(_cfg.MEMECOIN_TICKSTREAM_RETENTION_D)))
                    deleted = db_ticks.prune_unlinked(
                        tconn, keep_pools=keep, older_than_iso=horizon.isoformat())
                    if deleted:
                        _p("[memecoin] tickstream retention: pruned %d unlinked rows" % deleted)
                except Exception:
                    _p("[err] tickstream prune failed:\n" + traceback.format_exc())
            time.sleep(tick_s)

    def _tickstream_sol_worker():
        from sniper.persistence import db_ticks
        from sniper.services.memecoin_tick_stream import open_solana_mints, run_solana_arrival_lane
        from sniper.services.memecoin_solana_swap_marks import ws_url_from_read_rpc
        from sniper.settings.memecoin import MEMECOIN_SOLANA_RPC_URL, solana_read_rpc
        from sniper.util.clock import real_utc_now_iso
        sconn = connect_observer_main()
        # PREFER THE READ ENDPOINT (2026-08-26). This hardcoded MEMECOIN_SOLANA_RPC_URL, the
        # CUSTODY key. When that key's plan was spent the lane could not fail over at all and sat
        # in a reconnect loop from 2026-08-25 11:31 onward — 21 hours of no Solana ticks, 1,002
        # logged failures, while three healthy endpoints went unused. Custody is also the wrong
        # key for an observation lane on principle: it is the one that signs.
        # `solana_read_rpc()` is the selector the sibling swap-mark lane already uses. It resolves
        # to the public endpoint today, which the 2026-08-07 measurement clears for DELIVERY (5
        # pump.fun events in 20s, where publicnode returned a healthy subscription id and zero
        # events). Custody stays only as the last resort when no read endpoint is configured.
        ws_url = ws_url_from_read_rpc(solana_read_rpc()) \
            or (MEMECOIN_SOLANA_RPC_URL or "").replace("https://", "wss://")
        if not ws_url.startswith("wss://"):
            _p("[memecoin] tickstream solana lane: no wss-capable RPC url (lane off)")
            return
        _p("[memecoin] tickstream solana lane: %s" % ws_url.split("//")[-1].split("/")[0])

        # DELIVERY, NOT SUBSCRIPTION. A healthy subscription id proves nothing; count the rows the
        # lane actually writes and say so periodically, so a silently-dead endpoint is visible.
        _sol_lane = {"sessions": 0, "ticks": 0, "errors": 0, "logged": 0.0}

        def _on_session(ticks, watched, error):
            import time as _t
            _sol_lane["sessions"] += 1
            _sol_lane["ticks"] += int(ticks or 0)
            if error is not None:
                _sol_lane["errors"] += 1
            now = _t.monotonic()
            if now - _sol_lane["logged"] >= 900.0:
                _sol_lane["logged"] = now
                _p("[memecoin] tickstream solana delivery: %d ticks over %d sessions "
                   "(%d errored), watching %d mint(s)"
                   % (_sol_lane["ticks"], _sol_lane["sessions"], _sol_lane["errors"], watched))

        run_solana_arrival_lane(
            ws_url=ws_url, ticks_conn_factory=db_ticks.connect_ticks,
            mints_fn=lambda: open_solana_mints(sconn), now_fn=real_utc_now_iso,
            on_session=_on_session)

    def _start_bsc_volume_poll():
        """56 vol_death feed (spec 2026-10-01-memecoin-bsc-swap-feed-design.md): own thread, own
        read-only ledger connection, the archive endpoint only. Off unless 56 is listed."""
        from sniper.persistence import db_memecoin as _bm
        from sniper.services.memecoin_bsc_volume_poll import start_bsc_volume_poller
        from sniper.services.memecoin_fastlane import open_real_positions
        bconn = connect_observer_main()
        try:
            start_bsc_volume_poller(
                positions_fn=lambda: open_real_positions(bconn),
                entry_tx_fn=lambda pos: _bm.entry_tx_sig(bconn, pos.get("id")),
                thread_factory=threading.Thread, log=_p)
        except Exception:
            logger.exception("BSC volume poll start failed; 56 vol_death reads blind")

    def _start_chain1_volume_poll():
        """Chain-1 vol_death feed (spec 2026-10-08-memecoin-voldeath-chain1-arc-witness-design.md): its
        own thread and read-only ledger connection, the keyless eth.drpc.org on its own pacer.
        Off unless 1 is listed in MEMECOIN_VOLDEATH_POLL_CHAINS."""
        from sniper.persistence import db_memecoin as _bm
        from sniper.services.memecoin_bsc_volume_poll import start_chain1_volume_poller
        from sniper.services.memecoin_fastlane import open_real_positions
        econn = connect_observer_main()
        try:
            start_chain1_volume_poller(
                positions_fn=lambda: open_real_positions(econn),
                entry_tx_fn=lambda pos: _bm.entry_tx_sig(econn, pos.get("id")),
                thread_factory=threading.Thread, log=_p)
        except Exception:
            logger.exception("chain-1 volume poll start failed; chain-1 vol_death reads blind")

    def _start_arc_volume_poll():
        """Arc (5042) vol_death feed: the same poller on keyless arc.drpc.org, its own thread and
        read-only ledger connection. Off unless 5042 is listed in MEMECOIN_VOLDEATH_POLL_CHAINS."""
        from sniper.persistence import db_memecoin as _bm
        from sniper.services.memecoin_bsc_volume_poll import start_arc_volume_poller
        from sniper.services.memecoin_fastlane import open_real_positions
        aconn = connect_observer_main()
        try:
            start_arc_volume_poller(
                positions_fn=lambda: open_real_positions(aconn),
                entry_tx_fn=lambda pos: _bm.entry_tx_sig(aconn, pos.get("id")),
                thread_factory=threading.Thread, log=_p)
        except Exception:
            logger.exception("Arc volume poll start failed; Arc vol_death reads blind")

    def _robinhood_volume_worker():
        from sniper.services.memecoin_fastlane import open_real_positions
        from sniper.services.memecoin_volume_death import run_robinhood_volume_stream
        from sniper.settings.memecoin import MEMECOIN_ROBINHOOD_VOLUME_WS_URL
        from sniper.util.clock import real_utc_now_iso
        rconn = connect_observer_main()
        _p("[memecoin] Robinhood volume stream: started")
        run_robinhood_volume_stream(
            ws_url=MEMECOIN_ROBINHOOD_VOLUME_WS_URL,
            bindings_fn=lambda: open_real_positions(rconn),
            now_fn=real_utc_now_iso,
        )

    # Research RPC pools this process built, by lane, for the heartbeat. Written once by the lane's
    # own thread; the heartbeat only reads.
    _research_pool = {}

    def _research_pool_status():
        return {lane: pool.key_status() for lane, pool in list(_research_pool.items())
                if pool is not None} or None

    def _signal_pool_resolver_worker():
        """Resolve queued signal mints on the RESEARCH key. Own thread, own connections, never the
        entry path. Bounded by the rate cap so a signal burst cannot become an RPC burst."""
        import time as _t
        from sniper.persistence import db_ticks
        from sniper.services import memecoin_signal_pool_resolver as _spr
        from sniper.services.memecoin_pool_resolve import resolve_one_mint
        # Same construction the prewarm lane uses: one rate-limited read call over the rotating
        # research keys, with get_sigs/get_tx built on top of it.
        from sniper.services.memecoin_research_rpc import build_dependency_read_call
        from sniper.services.memecoin_solana_amounts import get_transaction_fn
        from sniper.services.memecoin_solana_tape import get_signatures_fn
        from sniper.settings.memecoin import (
            MEMECOIN_SIGNAL_POOL_RESOLVER_MAX_PER_MIN as _SPR_RATE)
        call, _research_pool["signal_pool_resolver"] = build_dependency_read_call("signal_pool_resolver")
        if call is None:
            _p("[memecoin] signal pool resolver: MEMECOIN_RESEARCH_HELIUS_RPC_URL(S) unset or "
               "shared with a funded key, OFF")
            return
        get_sigs, get_tx = get_signatures_fn(call=call), get_transaction_fn(call=call)
        _p("[memecoin] signal pool resolver: started on the research key")
        while True:
            try:
                job = _spr.take_next(_t.monotonic(), max_per_min=_SPR_RATE)
                if job is None:
                    _t.sleep(2.0)
                    continue
                mint, first_seen = job
                tc = db_ticks.connect_sol_tape()
                mconn = connect_observer_main()
                try:
                    got = resolve_one_mint(
                        tc, mconn, mint, first_seen=first_seen, get_tx=get_tx, get_sigs=get_sigs,
                        call=call, max_fetch=4, tape_budget=4)
                    _spr.record_outcome(got)
                finally:
                    tc.close()
                    mconn.close()
            except Exception:
                _p("[err] signal pool resolver:\n" + traceback.format_exc())
                _t.sleep(5.0)

    def _evm_key_prewarm_worker():
        """Warm V4 PoolKeys for queued signal mints. Own thread, own failure domain, never the
        entry path. A stored key answers in ~10ms; a live resolve costs ~840ms and fails under
        load, and no-quote is the top cause of a reserved 4663 attempt not filling."""
        import time as _t
        from sniper.services import memecoin_evm_key_prewarm as _kp
        from sniper.settings.memecoin import (
            MEMECOIN_EVM_KEY_PREWARM_MAX_PER_MIN as _KP_RATE)
        _p("[memecoin] evm key prewarm: started")
        while True:
            try:
                job = _kp.take_next(_t.monotonic(), max_per_min=_KP_RATE)
                if job is None:
                    _t.sleep(2.0)
                    continue
                chain, mint = job
                stored = None
                try:
                    stored = _kp.resolve(chain, mint)
                except Exception:
                    stored = None              # a provider failure is not "this coin has no pool"
                _kp.record_outcome(stored)
            except Exception:
                _p("[err] evm key prewarm:\n" + traceback.format_exc())
                _t.sleep(5.0)

    from sniper.settings.memecoin import MEMECOIN_SIGNAL_POOL_RESOLVER_ENABLED as _SPR_ON

    def _exit_tape_snapshot():
        try:
            from sniper.services import memecoin_exit_tape as _et
            return _et.snapshot()
        except Exception:
            return None

    def _route_series_snapshot():
        try:
            from sniper.services import memecoin_route_series as _rs
            return _rs.snapshot()
        except Exception:
            return None

    _coverage_cache = {"at": 0.0, "value": None}

    def _address_coverage():
        # Which armed Solana wallets have a verified on-chain address (step 1 of the address
        # registry: readiness only). Its own read-only connection, at most once a minute.
        now = time.monotonic()
        if now - _coverage_cache["at"] < 60.0:
            return _coverage_cache["value"]
        _coverage_cache["at"] = now
        try:
            from contextlib import closing as _closing
            from sniper.persistence.db_core import get_readonly_connection as _ro
            from sniper.services.memecoin_address_registry import solana_coverage
            from sniper.util.state_paths import state_path as _sp
            with _closing(_ro(str(_sp("sniper_tracker.db")))) as _c:
                _coverage_cache["value"] = solana_coverage(_c)
        except Exception:
            _coverage_cache["value"] = None
        return _coverage_cache["value"]

    _j7_diag_cache = {"at": -1e9, "value": None}

    def _j7_diag_status():
        # J7 refusal diagnostics: copy the cron job's prebuilt snapshot file, at most every 30 s.
        # No DB; a missing or stale file is published as such.
        now = time.monotonic()
        if now - _j7_diag_cache["at"] >= 30.0:
            _j7_diag_cache["at"] = now
            try:
                from sniper.services.memecoin_j7_refusal_diag import read_snapshot, snapshot_path
                _j7_diag_cache["value"] = read_snapshot(snapshot_path())
            except Exception:
                _j7_diag_cache["value"] = None
        return _j7_diag_cache["value"]

    def _early_quote_stats():
        # The J7 early entry quote's counters (Alpha GO option 4). None only if unimportable.
        try:
            from sniper.services.memecoin_early_quote import stats
            return stats()
        except Exception:
            return None

    def _dexscreener_stats():
        # DexScreener calls and suppressed calls per consumer (DexScreener out, phase A). None only
        # if unimportable.
        try:
            from sniper.execution.solana_quote import dexscreener_consumer_counts
            return dexscreener_consumer_counts()
        except Exception:
            return None

    def _raydium_late_shadow_stats():
        # The late Raydium shadow writer's counters. None only if unimportable.
        try:
            from sniper.execution.solana_real_seams import LATE_RAYDIUM_SHADOW
            return dict(LATE_RAYDIUM_SHADOW.stats)
        except Exception:
            return None

    def _coin_age_stats():
        # The coin-age floor's mint evidence (Alpha GO, 2026-09-28). None only if unimportable.
        try:
            from sniper.services.memecoin_coin_age import stats
            return stats()
        except Exception:
            return None

    def _desk_prequote_stats():
        try:
            from sniper.services.memecoin_desk_prequote import stats
            return stats()
        except Exception:
            return None

    def _submit_audit_stats():
        # Async send-receipt writer (slot lane). Empty until the first lane send starts a writer.
        try:
            from sniper.execution.submission_wiring import async_audit_stats
            return async_audit_stats()
        except Exception:
            return None

    def _jupiter_load_snapshot():
        # Funded Jupiter load. Measured because Jupiter is keyless and IP-limited, and no second
        # consumer on this host is enabled until funded's own draw is a number, not an estimate.
        try:
            from sniper.execution.solana_real_seams import jupiter_load_snapshot
            return jupiter_load_snapshot()
        except Exception:
            return None

    def _chain1_fee_headroom_status():
        try:
            from sniper.execution import chain1_protect as _c1
            return _c1.status()
        except Exception:
            return None

    def _base_fast_lane_status():
        try:
            from sniper.services import memecoin_base_fast_lane as _base_lane
            return _base_lane.status()
        except Exception:
            return None

    def _rh_fast_lane_status():
        try:
            from sniper.services import memecoin_rh_fast_lane as _rh_lane
            return _rh_lane.status()
        except Exception:
            return None

    def _standing_allowance_snapshot():
        try:
            from sniper.settings import memecoin as _sa_cfg
            if not _sa_cfg.memecoin_evm_standing_allowance_chains():
                return None
            from sniper.execution import evm_standing_allowance as _sa
            return _sa.snapshot()
        except Exception:
            return None

    def _spr_snapshot():
        from sniper.services import memecoin_signal_pool_resolver as _spr
        return _spr.snapshot()

    if _SPR_ON:
        threading.Thread(target=_signal_pool_resolver_worker, daemon=True).start()
    from sniper.settings.memecoin import MEMECOIN_EVM_KEY_PREWARM_ENABLED as _KP_ON
    if _KP_ON:
        threading.Thread(target=_evm_key_prewarm_worker, daemon=True).start()

    from sniper.settings.memecoin import MEMECOIN_TICKSTREAM_ENABLED as _TICKS
    if _TICKS:
        # EVM ticks feed the funded hot-exit engine and exit-state materializer.
        threading.Thread(target=_tickstream_evm_worker, daemon=True).start()
        if not _FUNDED_ONLY:
            threading.Thread(target=_tickstream_sol_worker, daemon=True).start()
        _p("[memecoin] tick-stream ingest v0: EVM%s lanes started"
           % ("" if _FUNDED_ONLY else " + solana"))

    from sniper.settings.memecoin import (
        MEMECOIN_ROBINHOOD_VOLUME_WS_URL as _RH_VOLUME_WS,
        MEMECOIN_VOLDEATH_ARMED as _VOLDEATH_ARMED,
    )
    if _VOLDEATH_ARMED and _RH_VOLUME_WS:
        threading.Thread(target=_robinhood_volume_worker, daemon=True).start()
    if _VOLDEATH_ARMED:
        _start_bsc_volume_poll()          # no thread unless MEMECOIN_VOLDEATH_POLL_CHAINS lists 56
        _start_chain1_volume_poll()       # no thread unless MEMECOIN_VOLDEATH_POLL_CHAINS lists 1
        _start_arc_volume_poll()          # no thread unless MEMECOIN_VOLDEATH_POLL_CHAINS lists 5042

    def _hot_exit_worker():
        """Default-OFF push lane. Own DB handles; all execution returns through real_exit_sweep."""
        import asyncio
        from sniper.persistence import db_exit_state, db_ticks
        from sniper.persistence.db_core import get_connection as _hot_connection
        from sniper.services.memecoin_exit_brain import CUT, DERISK, TAKE_PROFIT
        from sniper.services.memecoin_hot_supervisor import (
            PrewarmWorkerPool, build_hot_book, run_hot_exit_lane)
        from sniper.settings import memecoin as _cfg
        from sniper.util.clock import real_utc_now, real_utc_now_iso

        from sniper.execution.evm_wallet import _rpc
        from sniper.services.memecoin_evm_hot import HOT_CHAINS
        from sniper.services.memecoin_hot_supervisor import build_evm_hot_book

        hconn = _hot_connection()
        try:
            sol_conn = db_ticks.connect_ticks_readonly(db_ticks.sol_tape_db_path())
        except Exception:
            _p("[err] hot exit lane: Solana tape unavailable; lane stopped")
            return
        try:
            evm_conn = db_ticks.connect_ticks_readonly(db_ticks.ticks_db_path())
        except Exception:
            evm_conn = None
            _p("[warn] hot exit lane: EVM tape unavailable; EVM push lane off (pull covers)")

        def evm_call_for(chain):
            rpc = _cfg.evm_rpc_for_chain(chain)

            def call(method, params):
                resp = _rpc(rpc, method, params)
                if not isinstance(resp, dict) or "result" not in resp:
                    raise RuntimeError(f"rpc {method} error: {resp!r:.200}")
                return resp["result"]
            return call

        idle_logged = False
        # Process-lifetime capacity: a quote that times out logically may still occupy its Python
        # thread. Sharing the pool across 30-second lane rebuilds keeps the bound real across epochs.
        prewarm_pool = PrewarmWorkerPool(max_workers=_cfg.MEMECOIN_HOT_PREWARM_WORKERS)
        while True:
            positions = m.open_positions_by_strategy(
                hconn, _cfg.MEMECOIN_REAL_POLICY, mode="real")
            engines, vaults, entries = build_hot_book(positions, sol_conn, now_fn=real_utc_now)
            # WO-9: merge held EVM engine-envelope positions into the SAME shared supervisor. Held
            # pools only; grounded EVM logs authorize directly; the pull lane still bounds the gap.
            evm_lanes = []
            if evm_conn is not None:
                e_engines, e_entries, held = build_evm_hot_book(
                    positions, evm_conn, now_fn=real_utc_now)
                for key in e_engines.keys():
                    engines.track(key, e_engines.engine(key))
                entries.update(e_entries)
                evm_lanes = [{"chain_id": chain, "held_pools": pools,
                              "rpc_call": evm_call_for(chain)}
                             for chain, pools in held.items()
                             if chain in HOT_CHAINS and pools]
            finish_db_cycle(hconn)
            if not entries:
                if not idle_logged:
                    _p("[memecoin] hot exit lane: no engine-envelope Solana/EVM positions; polling")
                    idle_logged = True
                time.sleep(5.0)
                continue
            idle_logged = False

            def preview_for(key, pos):
                ctx = context_for_position(contexts, pos)
                fn = ctx.get("preview_exit_fn") if ctx else None
                return fn(pos) if fn else None

            def pull_fallback():
                # The hot lane's own pull fallback: it runs when the WS has not woken us,
                # so it is neither a wake nor the periodic pass and must not be filed as
                # either — that would put a fallback's latency in the wake lane's p50.
                return real_exit_sweep(
                    hconn, maintenance_contexts, network_ids=set(maintenance_contexts), lane="hot_pull_fallback",
                    reserve_witness_cache=_swap_mark_cache if _SOL_SWAP_MARKS else None,
                    derisk_settler=_rail_settler)

            def on_decision(key, decision, _row, preview):
                entry = entries.get(key)
                if entry is None or entry["engine"].last_state is None:
                    return
                pending_structure = (
                    decision.action not in (CUT, DERISK, TAKE_PROFIT)
                    and "structure_hysteresis_pending" in tuple(decision.reason_codes or ()))
                if decision.action not in (CUT, DERISK, TAKE_PROFIT) and not pending_structure:
                    return
                now_iso = real_utc_now_iso()
                try:
                    xc = db_exit_state.connect_exit_state_existing(busy_ms=250)
                    try:
                        db_exit_state.write_market_state(
                            xc, entry["engine"].last_state, computed_at=now_iso)
                    finally:
                        xc.close()
                except Exception:
                    _p("[err] hot exit lane: state persist failed; action held fail-closed")
                    return
                pid = entry["position"]["id"]
                handoff = persist_hot_controller_state(
                    hconn, pid, entry["engine"].contract.get("controller_state"))
                if handoff is None:
                    _p("[warn] hot exit lane: controller CAS contention; funded wake held")
                    return
                previews = {pid: preview} if preview is not None else None
                real_exit_sweep(
                    hconn, maintenance_contexts, network_ids=set(maintenance_contexts), price_fn=lambda _mints: {},
                    preview_by_position=previews, lane="fastlane_wake",
                    reserve_witness_cache=_swap_mark_cache if _SOL_SWAP_MARKS else None,
                    derisk_settler=_rail_settler)

            # Rebuild subscriptions every 30s so newly-opened/closed positions enter/leave the
            # book without a runner restart. The pull fallback remains active during each epoch.
            try:
                asyncio.run(run_hot_exit_lane(
                    engines, vaults, entries, now_fn=real_utc_now, on_decision=on_decision,
                    pull_fn=pull_fallback, preview_fn=preview_for,
                    max_decision_gap_s=_cfg.MEMECOIN_HOT_EXIT_MAX_DECISION_GAP_S,
                    prewarm_period_s=_cfg.MEMECOIN_HOT_EXIT_PREWARM_PERIOD_S, duration_s=30.0,
                    decision_cfg=_cfg, evm_lanes=evm_lanes, now_iso_fn=real_utc_now_iso,
                    evm_poll_s=_cfg.MEMECOIN_HOT_EVM_POLL_S,
                    prewarm_workers=_cfg.MEMECOIN_HOT_PREWARM_WORKERS,
                    prewarm_deadline_s=_cfg.MEMECOIN_HOT_PREWARM_DEADLINE_S,
                    prewarm_pool=prewarm_pool))
            except Exception:
                finish_db_cycle(hconn, failed=True)
                raise
            else:
                finish_db_cycle(hconn)

    from sniper.settings.memecoin import MEMECOIN_HOT_EXIT_ENABLED as _HOT_EXIT
    if _HOT_EXIT and contexts:
        threading.Thread(target=_hot_exit_worker, name="memecoin-hot-exit", daemon=True).start()
        _p("[memecoin] hot exit lane: thread started (push + enforced pull fallback)")

    # One worker owns source notices, funded results, and durable delivery state.
    from sniper.services.memecoin_telegram import start_notification_dispatcher
    tg_on = os.getenv("MEMECOIN_TG_ALERTS", "0").strip().lower() in {"1", "true", "yes"}
    notification_dispatcher = start_notification_dispatcher()
    alert_q = notification_dispatcher if tg_on else None

    # Operator health digest has its own connection and thread. Collection is SQLite-only and
    # delivery enters the bounded Telegram dispatcher, so neither DB aggregation nor HTTP can
    # delay feed polling, reservation, execution, or exit sweeps.
    from sniper.settings.memecoin import (
        MEMECOIN_TG_DIGEST_ENABLED, MEMECOIN_TG_DIGEST_PERIOD_S)

    def _operator_digest_worker():
        from sniper.persistence.db_core import get_connection as _digest_connection
        from sniper.services.memecoin_digest import run_digest_worker
        run_digest_worker(
            connection_factory=_digest_connection,
            period_s=MEMECOIN_TG_DIGEST_PERIOD_S,
            log_fn=_p,
        )

    if start_operator_digest_worker(
            enabled=MEMECOIN_TG_DIGEST_ENABLED,
            period_s=MEMECOIN_TG_DIGEST_PERIOD_S,
            worker_fn=_operator_digest_worker,
            thread_factory=threading.Thread,
            log_fn=_p):
        _p("[memecoin] operator digest: background thread started period=%ss"
           % MEMECOIN_TG_DIGEST_PERIOD_S)

    # Ticks handle for the fill-anchored position->pool store. Opened ONCE: the hook runs every
    # poll and a fresh connection per iteration would churn the ticks DB for no reason. None means
    # the store is unreachable and the hook no-ops — the */2 cron reconciler still covers it.
    try:
        from sniper.persistence import db_ticks as _pp_ticks
        _pool_tc = _pp_ticks.connect_ticks()
    except Exception:
        _pool_tc = None
        _p("[warn] position->pool store unavailable; in-loop resolution disabled (cron still runs)")

    def _run_manual_exits(exec_conn, where):
        from sniper.services.memecoin_manual_exit_intents import drain as _mei_drain
        from sniper.services.memecoin_alerts import alert_operator_action as _mei_notify
        _mei_done = _mei_drain(
            exec_conn, make_venue_exit_fn(contexts), make_venue_partial_exit_fn(contexts),
            now_fn=real_utc_now_iso, position_fn=m.get_position,
            notify_fn=_mei_notify, demotion_fn=check_wallet_demotion,
            settle_fn=lambda c, pos: settle_derisk_for_manual_close(c, contexts, pos, real_utc_now_iso()))
        for _iid, _istate in _mei_done:
            _p("[memecoin] manual exit intent %s -> %s (%s)" % (_iid, _istate, where))
        return _mei_done

    _manual_exits = None
    if contexts:
        from sniper.services.memecoin_manual_exit_intents import ManualExitWorker, start_source_partial_worker
        _source_partials = start_source_partial_worker(
            connect=get_connection, observe=get_readonly_connection,
            source_partial_fn=make_venue_source_partial_exit_fn(contexts),
            recovery_fn=lambda c, at: recover_source_partials(c, contexts, at),
            now_fn=real_utc_now_iso, position_fn=m.get_position)
        _p("[memecoin] source partial worker started")
        _manual_exits = ManualExitWorker(
            connect=get_connection, observe=get_readonly_connection,
            drain_fn=lambda exec_conn: _run_manual_exits(exec_conn, "worker"))
        try:
            _manual_exits.start()
            _p("[memecoin] manual exit worker started")
        except Exception:
            _p("[err] manual exit worker start failed:\n" + traceback.format_exc())

    # MANUAL + DESK BUYS ON THEIR OWN THREAD (build step 2, spec desk-execution-plumbing §2.4).
    # Before this the queue waited for the main loop's pass after exit work: claim lag p50 3.54 s,
    # p90 11.93 s over 445 intents. The worker is woken by enqueue's datagram, uses its own
    # connection per pass, claims the operator's intents before desk ones, and shares pass_lock with
    # the main loop's fallback below, so one buy_fn never runs on two threads at once. The on-chain
    # lane already calls real_entry_hook from its own thread the same way.
    # DESK PRE-QUOTE (Alpha GO 2026-10-23, option A; sniper/services/memecoin_desk_prequote.py): keeps a
    # fresh entry quote for each mint JUDGE is deciding, at most MEMECOIN_DESK_MAX_CONCURRENT, on its
    # own thread with its own read-only desk.db connection. Nothing here reserves, signs or authorizes.
    from sniper.services import memecoin_desk_prequote as _prequote
    _sol_ctx = (contexts or {}).get("solana")
    _prequote_quote_fn = _sol_ctx.get("entry_quote_fn") if isinstance(_sol_ctx, dict) else None
    from sniper.settings import memecoin as _pq_cfg
    if (_prequote.enabled() and _prequote_quote_fn is not None
            and float(getattr(_pq_cfg, "MEMECOIN_DESK_CLIP_USD", 0) or 0) > 0
            and int(getattr(_pq_cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 0) or 0) > 0):
        from sniper.services import memecoin_early_quote as _pq_eq
        from sniper.persistence import db_memecoin_desk as _pq_store
        from sniper.util.state_paths import state_path as _pq_path
        from sniper.util.clock import real_utc_now_iso as _pq_now
        _pq_max = int(_pq_cfg.MEMECOIN_DESK_MAX_CONCURRENT)
        _pq_book = _prequote.PrequoteBook(quote_fn=_prequote_quote_fn, clip_usd=_pq_cfg.MEMECOIN_DESK_CLIP_USD,
                                          max_hints=_pq_max, max_age_s=_pq_eq.configured_max_age_s(),
                                          # the corrected-UTC clock every trace anchors on (Codex P2): the held
                                          # quote's observed time is its completion, never the execution time
                                          observed_fn=_pq_now)
        _prequote.install(_pq_book)

        def _pq_hints():
            import sqlite3 as _pq_sql
            path = _pq_path("data", "memecoin_desk.db")
            c = _pq_sql.connect("file:%s?mode=ro" % path, uri=True, timeout=1)
            try:
                return _pq_store.live_prequote_hints(c, now=time.time(), limit=_pq_max)
            finally:
                c.close()

        threading.Thread(target=_prequote.run_forever, args=(_pq_book, _pq_hints, threading.Event()),
                         name="desk-prequote", daemon=True).start()
        _p("[memecoin] desk pre-quote thread started (max %d hints)" % _pq_max)

    _manual_intents = None
    _mi_stall_paged = [False]
    if contexts:
        from sniper.services.memecoin_manual_intents import drain as _mi_drain
        from sniper.services.memecoin_manual_intents import (
            pending_count as _mi_pending, wake_address as _mi_wake_address)
        from sniper.services.memecoin_onchain_wake import LaneWake
        _intent_buy_fn = make_real_buy_fn(contexts)

        def _run_manual_intents(exec_conn):
            where = "worker" if threading.current_thread().name == "manual-intents" else "fallback"
            _mi_done = _mi_drain(exec_conn, _intent_buy_fn, now_fn=real_utc_now_iso)
            for _iid, _istate in _mi_done:
                _p("[memecoin] manual intent %s -> %s (%s)" % (_iid, _istate, where))
            return _mi_done

        _manual_intents = ManualExitWorker(
            connect=get_connection, observe=get_readonly_connection,
            drain_fn=_run_manual_intents,
            pending_fn=_mi_pending, name="manual-intents", wake=LaneWake(_mi_wake_address()))
        try:
            _manual_intents.start()
            _p("[memecoin] manual intent worker started")
        except Exception:
            _p("[err] manual intent worker start failed:\n" + traceback.format_exc())

    def _drain_manual_exits(where):
        """The main loop covers a failed worker; healthy manual sells never wait on this loop."""
        if _manual_exits is None:
            return
        try:
            _manual_exits.fallback(conn)
        except Exception:
            _p("[err] manual exit intent drain failed:\n" + traceback.format_exc())

    def _publish_idle_heartbeat():
        # Cached status only. No DB, feed poll, retry, resolver, alert or provider call.
        refresh_idle_heartbeat(started_at=runner_started_at,
            slot_lane_status=_slot_lane_snapshot(),
            tpu_status=tpu_status_for_contexts(contexts),
            rpc_pool_status=rpc_pool_status_for_contexts(contexts),
            fastlane_status=_fastlane_status())

    while True:
        cycle = time.time()
        _iters += 1
        _ph = {}                      # per-phase seconds, this cycle (measurement only)
        _drain_manual_exits("head")
        if _FASTLANE:
            _fastlane_thread = ensure_fastlane_thread(
                _fastlane_thread, target=_fastlane_worker, state=_fastlane_state)
        # COPY path (feed-dependent). Isolated so a fomo outage here does NOT skip the exit
        # sweep below — otherwise open positions would sit unmanaged (no SL/TP) during any
        # feed 5xx/timeout, biasing realized PnL.
        try:
            if not _J7_SOURCE and cycle >= refresh_retry_after and refresh_due(
                    now=cycle, last_refresh=last_refresh, interval_s=REFRESH_EVERY_S,
                    ttl_s=auth.access_expires_in(), margin_s=REFRESH_MARGIN_S):
                try:
                    auth.refresh_token()
                except Exception:
                    # A FAILED REFRESH MUST BACK OFF TOO. This assignment lived only in the
                    # `not extended` branch below, which a raise can never reach — so an HTTP 429
                    # bypassed the backoff entirely and the next cycle retried at poll cadence.
                    # Measured 2026-09-02 over the runner log: 3,723 rate-limit rejections since
                    # 2026-08-17, still arriving today, with 44% of consecutive retries faster
                    # than the 60s they were supposed to obey and 17% inside ten seconds.
                    # Retrying into a rate limit is what sustains the rate limit.
                    # `raise` is deliberate: the outer handler writes the heartbeat and fires the
                    # auth page, and swallowing here would hide a real auth death.
                    refresh_retry_after = cycle + REFRESH_RETRY_BACKOFF_S
                    raise
                if auth.extended:
                    last_refresh = cycle          # expiry really moved — re-arm the interval
                else:
                    # Privy re-issued the SAME token (it only mints a new one near expiry).
                    # Do NOT bank this as a refresh: leave `last_refresh` alone so we try again,
                    # but back off, because every attempt rotates the single-use refresh token
                    # and spinning at poll cadence risks losing the chain on a dropped response.
                    refresh_retry_after = cycle + REFRESH_RETRY_BACKOFF_S
            # ENTRY-KNOB OVERLAY — THE ONLY WRITER (2026-09-08, Alpha review).
            # The operator's cockpit changes entry thresholds through a DB overlay. It is applied
            # HERE, on the main cycle, and nowhere else: `real_hook` runs in several daemon threads
            # and applying it from the entry path had all of them writing the settings module.
            # The applier also defers while any funded buy decision is in flight, so a change lands
            # between decisions and never inside one. A deferred cycle simply retries on the next.
            if real_hook is not None:
                try:
                    from sniper.settings import memecoin as _knob_cfg
                    from sniper.services.memecoin_entry_knobs import apply_overrides as _apply_knobs
                    _kres = _apply_knobs(conn, _knob_cfg)
                    if _kres["changed"]:
                        _p("[memecoin] entry knobs applied: %s"
                           % " ".join("%s=%s" % kv for kv in sorted(_kres["changed"].items())))
                except Exception as _kexc:
                    _p("[memecoin] entry knob overlay failed (%s) — staying on the current values"
                       % _kexc)

            # THE FEED FETCH HAS ITS OWN CADENCE, THE LOOP DOES NOT CHANGE (2026-09-06).
            # The global feed is a discovery telescope: it finds wallets we do not follow yet, and
            # 1-40 a day is all it yields. Polling it at loop cadence spends 14x the fomo request
            # budget of the armed lane, and every armed poll queues behind it on the shared guard.
            # Slowing the LOOP instead would slow the ~15 s stop-loss bracket sweep below, which is
            # the coupling that let a feed backoff hold a stop-loss on 2026-09-02.
            # J7 SOURCE DRAINS EVERY CYCLE (2026-09-09). The 45 s period below rations fomo
            # requests; a socket relay has no request to ration, and holding its queue for 45 s
            # would put the copy 9 cycles behind a 300 ms signal.
            if (_J7_SOURCE or _GLOBAL_FEED_PERIOD_S <= 0
                    or cycle - _last_global_feed >= _GLOBAL_FEED_PERIOD_S):
                _last_global_feed = cycle
                res = run_once(conn, auth, SIZE_USD, stats_fn=dexscreener_stats,
                               alert_queue=alert_q, real_hook=real_hook,
                               receipt_observers={chain: (contexts.get(chain) or {}).get("bsc_execution_observer")
                                                  for chain in ("56", "4663")})
            else:
                res = {"events": 0, "opened": 0, "exits": 0, "skipped_feed_not_due": True}
            # The J7 arrival stamps, written after the cycle's funded work and never before it.
            flush_j7_receipts(_j7_feed_or_none_cached(), _J7_RECEIPT_FLUSH)
            # REVIVAL WATCH: poll on the ordinary loop cadence. NOT the fastlane, and it holds no
            # lock the entry path wants — it re-reads DexScreener and, on clearance, re-asks the
            # normal hook. Zero work when no watch is live.
            if real_hook is not None and _REVIVAL_ENABLED:
                try:
                    from sniper.services import memecoin_revival_watch as _rw
                    if _rw.WATCHES:
                        import time as _rwt
                        from sniper.util.clock import real_utc_now_iso as _rui6
                        from sniper.services.memecoin_real_runner import revival_watch_volume_cleared

                        def _rw_superseded(mint):
                            # A live position or reservation owns the mint; the watch must not
                            # duplicate that entry or be mistaken for its cause.
                            return bool(conn.execute(
                                "SELECT 1 FROM memecoin_positions WHERE execution_mode='real' "
                                "AND is_open=1 AND mint=? "
                                "UNION SELECT 1 FROM memecoin_pending_entries "
                                "WHERE status='reserved' AND mint=? LIMIT 1",
                                (mint, mint)).fetchone())

                        def _rw_record(watch, outcome, detail):
                            m.record_revival_watch(
                                conn, origin_event_id=(watch or {}).get("origin_event_id"),
                                mint=(watch or {}).get("mint"),
                                network_id=(watch or {}).get("network_id") or "solana",
                                refused_at=(watch or {}).get("refused_at") or _rui6(),
                                refused_mcap=(watch or {}).get("refused_mcap"),
                                refused_need=(watch or {}).get("refused_need"),
                                outcome=outcome, detail=detail,
                                polls=(watch or {}).get("polls"), finished_at=_rui6())

                        def _rw_clears(watch, stats):
                            # ONE read: revival_watch_volume_cleared() calls gate_evidence() and
                            # volume_alive_decision() itself, once. evidence_out only extracts
                            # values already computed -- fetch first, write last, no second call.
                            evidence = {}
                            cleared = revival_watch_volume_cleared(
                                stats, watch["event"], mcap=watch.get("refused_mcap"),
                                network_id=watch.get("network_id"),
                                coin_band=watch.get("coin_band"), evidence_out=evidence,
                                decision_at=datetime.fromisoformat(_rui6()))
                            if evidence:
                                # A recording failure costs a row, never the decision: `poll()`'s
                                # own try/except wraps the whole per-watch pass, so an unwrapped
                                # write error here would discard an already-decided clearance,
                                # drop the watch, skip the re-inject, and leave it un-re-armable.
                                try:
                                    m.record_revival_watch_poll(
                                        conn, origin_event_id=watch.get("origin_event_id"),
                                        mint=watch.get("mint"),
                                        network_id=watch.get("network_id") or "solana",
                                        poll_index=watch.get("polls"), polled_at=_rui6(),
                                        usd=evidence.get("usd"), reason=evidence.get("reason"),
                                        coverage=evidence.get("coverage"),
                                        covered_s=evidence.get("covered_s"),
                                        covered_gap_s=evidence.get("covered_gap_s"),
                                        measured_at=evidence.get("measured_at"),
                                        allowed=evidence.get("allowed"),
                                        decision_reason=evidence.get("decision_reason"),
                                        source_age_s=evidence.get("source_age_s"))
                                except Exception:
                                    _p("[err] revival watch poll record failed for %s:\n%s"
                                       % (watch.get("mint"), traceback.format_exc()))
                            return cleared

                        _rwt_tally = _rw.poll(
                            _rw.WATCHES, now=_rwt.monotonic(), floor=0.0,
                            stats_fn=dexscreener_stats,
                            reinject_fn=lambda e: real_hook(conn, e),
                            record_fn=_rw_record, superseded_fn=_rw_superseded,
                            clears_fn=_rw_clears)
                        if _rwt_tally.get("cleared") or _rwt_tally.get("superseded"):
                            _p("[memecoin] revival watch: %s" % _rwt_tally)
                except Exception:
                    _p("[err] revival watch poll failed:\n" + traceback.format_exc())
            # Fresh-pool re-quote lane: re-attempt buys that missed on an unindexed pool, as the
            # pool becomes routable. Cheap (indexed queries; a quote only when a row is due), runs
            # every poll so a coin fills the instant a venue can price it. Never breaks the loop.
            if real_hook is not None:
                try:
                    from sniper.services.memecoin_real_runner import run_entry_retry_lane
                    from sniper.util.clock import real_utc_now_iso as _rui_retry
                    from sniper.services.memecoin_dip_retry import ensure_watch as _dip_watch
                    # The dip retry's quote-only watch (idempotent; idles while its knobs are off).
                    # Its own read-only handle on the SAME ledger file as this connection.
                    # The EVM watches quote through the funded contexts; a hit wakes the dip
                    # hit lane.
                    _dip_watch(conn.execute("PRAGMA database_list").fetchone()[2],
                               contexts=contexts, wake=_dip_wake.set)
                    _rl = (_entry_retries.fallback(conn, _rui_retry()) if _entry_retries is not None
                           else run_entry_retry_lane(conn, real_hook, _rui_retry()))
                    if _rl.get("filled") or _rl.get("gave_up"):
                        _p("[memecoin] entry-retry lane: %s" % _rl)
                except Exception:
                    _p("[err] entry-retry lane failed:\n" + traceback.format_exc())
            n += 1
            # `.get`, not `[]`: a cycle that skips the feed fetch returns a partial result, and a
            # KeyError here costs the heartbeat write and books the cycle as a DB failure. Every
            # other read of `res` below is already `.get`; these two were the outliers.
            opened += res.get("opened", 0)
            exits += res.get("exits", 0)
            for _st, _n in (res.get("real") or {}).items():
                real_tally[_st] = real_tally.get(_st, 0) + _n
            # item 9: only a 401/403 is auth death (heartbeat auth_ok=False -> operator paged, but
            # a restart won't fix it). A 5xx/timeout is transient -> keep auth_ok=True so the loop
            # (and its exit sweep) stays alive. bracket_ts = last SUCCESSFUL sweep.
            kind = classify_fomo_error(status=res.get("feed_status"))
            _evm_pool_watch()
            write_heartbeat(
                res, auth_ok=(kind != "auth_dead"), bracket_ts=last_bracket or None,
                sol_buffer=(_sol_buffer_box["watch"].status() if _sol_buffer_box["watch"] is not None else None),
                tpu_status=tpu_status_for_contexts(contexts),
                rpc_pool_status=rpc_pool_status_for_contexts(contexts),
                evm_rpc_pool_status=_evm_pool_status(),
                fastlane_status=_fastlane_status(), started_at=runner_started_at,
                rails_observability=rails_obs,
                pool_resolver=(_spr_snapshot() if _SPR_ON else None),
                research_rpc_pool=_research_pool_status(),
                research_capture=(_research_capture.status() if _research_capture is not None else None),
                paper_fanout=(_paper_fanout_mod.active().status()
                              if _paper_fanout_mod.active() is not None else None),
                evm_gas_topup=(_evm_gas_topup_worker.status()
                               if _evm_gas_topup_worker is not None else None),
                other_holdings=_other_holdings_status(),
                paper_bracket=(paper_bracket_status() if _PAPER_BRACKET_THREAD else None),
                jupiter_load=_jupiter_load_snapshot(),
                route_series=_route_series_snapshot(), lifi_status=_lifi_status(),
                exit_tape=_exit_tape_snapshot(),
                fomo_activity_ws_status=_fomo_activity_ws_snapshot(),
                j7_feed_status=(_j7_feed.status() if _j7_feed is not None else None),
                onchain_volume_feed=_onchain_volume_feed(),
                wallet_entry_caps=wallet_cap_cache.status(),
                ethereum_worker=(_ethereum_worker.status() if _ethereum_worker is not None else None),
                submit_audit=_submit_audit_stats(), address_coverage=_address_coverage(), j7_refusal_diag=_j7_diag_status(),
                slot_lane_status=_slot_lane_snapshot(), early_quote=_early_quote_stats(),
                coin_age=_coin_age_stats(), dexscreener=_dexscreener_stats(),
                desk_prequote=_desk_prequote_stats(),
                raydium_late_shadow=_raydium_late_shadow_stats(),
                dip_lane=_dip_lane_status(), pinned_reoffer=_pinned_reoffer_status(),
                entry_retry_workers=(_entry_retries.status() if _entry_retries is not None else None),
                manual_exits=(_manual_exits.status() if _manual_exits is not None else None),
                exit_sweep_timing=exit_sweep_timing(), lp_rail=lp_rail_status(_swap_mark_cache),
                manual_intents=(_manual_intents.status() if _manual_intents is not None else None),
                rail_derisk_settler=(_rail_settler.status() if _rail_settler is not None else None),
                standing_allowance=_standing_allowance_snapshot(),
                gate_balance_warm=(_gate_balance_warmer.status()
                                   if _gate_balance_warmer is not None else None),
                # Never raises (it is evaluated outside write_heartbeat's own guard).
                quote_price_warmers=(_quote_price_warmer_status(contexts) if contexts else None),
                chain1_fee_headroom=_chain1_fee_headroom_status(),
                base_fast_lane=_base_fast_lane_status(),
                            rh_fast_lane=_rh_fast_lane_status())
            # Persist the J7 tracked lists from THIS thread, which owns a connection. The socket
            # thread must not touch the ledger. Best-effort: losing a list costs the prune its
            # freshness check, never the runner its tick.
            _persist_j7_tracked(conn, _j7_feed)
            lifi_heartbeat_ready.set()
            # Sanitized submission health snapshot for the cockpit (observability only; best-effort).
            write_submission_health(_STATE_ROOT)
        except Exception as e:
            finish_db_cycle(conn, failed=True)
            _p("[err] run_once failed:\n" + traceback.format_exc())
            # an exception (network/refresh) is transient by default -> keep the loop alive.
            kind = classify_fomo_error(exc=e)
            write_heartbeat({"events": 0, "opened": 0}, auth_ok=(kind != "auth_dead"),
                            bracket_ts=last_bracket or None,
                            tpu_status=tpu_status_for_contexts(contexts),
                            rpc_pool_status=rpc_pool_status_for_contexts(contexts),
                            evm_rpc_pool_status=_evm_pool_status(),
                            fastlane_status=_fastlane_status(),
                            other_holdings=_other_holdings_status(),
                            started_at=runner_started_at, lifi_status=_lifi_status(),
                            fomo_activity_ws_status=_fomo_activity_ws_snapshot(),
                            j7_feed_status=(_j7_feed.status() if _j7_feed is not None else None),
                            onchain_volume_feed=_onchain_volume_feed(),
                            wallet_entry_caps=wallet_cap_cache.status(), j7_refusal_diag=_j7_diag_status(),
                            slot_lane_status=_slot_lane_snapshot(),
                            manual_exits=(_manual_exits.status() if _manual_exits is not None else None),
                exit_sweep_timing=exit_sweep_timing(), lp_rail=lp_rail_status(_swap_mark_cache),
                manual_intents=(_manual_intents.status() if _manual_intents is not None else None),
                            rail_derisk_settler=(_rail_settler.status() if _rail_settler is not None else None),
                            chain1_fee_headroom=_chain1_fee_headroom_status(),
                            base_fast_lane=_base_fast_lane_status(),
                            rh_fast_lane=_rh_fast_lane_status())
            lifi_heartbeat_ready.set()
        else:
            finish_db_cycle(conn)
        # Auth-death page fires from EITHER branch. A spent Privy token RAISES (fomo_get -> run_once ->
        # here), landing in the except above, so the page MUST be hoisted out of the try or it never
        # fires on the exact path auth death actually takes (the residual the review caught).
        _ph['copy'] = time.time() - cycle
        try:                          # fetch vs fan-out inside the copy phase
            _ph['fomo_fetch'] = float(res.get('fetch_s') or 0.0)
        except Exception:
            pass
        auth_alerted = handle_auth_page(kind, auth_alerted)
        # A DARK OR EXPIRING J7 ACCOUNT PAGES (2026-09-24: 54 armed wallets dark ~5 h, 1,184
        # 'Invalid token' errors, no page). Hoisted beside the auth page so it runs from either
        # branch above. A status read the heartbeat already makes, arithmetic and an enqueue:
        # no network, nothing on the fill path.
        if _j7_feed is not None:
            try:
                from sniper.services.memecoin_alerts import note_j7_accounts, note_j7_token_expiry
                _j7_status = _j7_feed.status()
                note_j7_accounts(_j7_status, now_mono=time.monotonic())
                _j7_auto_refresh = {index for index, account in enumerate(
                    _j7_status.get("accounts") or [_j7_status], 1)
                    if (account.get("auth_refresh") or {}).get("enabled")}
                note_j7_token_expiry(_j7_feed.token_expiries(), now_epoch=time.time(),
                                     auto_refresh_accounts=_j7_auto_refresh)
            except Exception:
                _p("[warn] j7 health pages failed:\n" + traceback.format_exc())
        _ph['auth'] = time.time() - cycle - _ph['copy']

        # EXIT management + telemetry run REGARDLESS of feed health.
        # NAME THE MAINTENANCE WORKER (2026-09-06). `maint` below is a RESIDUAL --
        # `cycle - copy - auth` -- so it names nothing. On 2026-09-06 a `maint=56.3s` tail pushed the
        # cycle past the 90s sweep limit and the watchdog restarted the runner, and the log could not
        # say which of the twelve cadence-gated blocks in this try had spent the time. Each block now
        # stamps its own elapsed seconds, so the next tail identifies its own worker.
        #
        # MEASUREMENT ONLY: `_mp` records a duration and touches no control flow. A block that its
        # cadence gate skipped stamps ~0, which is itself the answer. The residual `maint` is kept
        # as the total so the existing series stays comparable across this change.
        _t_mp = [time.time()]

        def _mp(_name):
            _now = time.time()
            _ph["maint." + _name] = _now - _t_mp[0]
            _t_mp[0] = _now

        try:
            if cycle - last_bracket > 15:   # ~15s SL/TP sweep (batched, rate-safe)
                # Blind pricing for DexScreener-blind chains (4663 = 100% missing_pair) on a
                # slower cadence than the 15s dex sweep; we
                # want a usable price series, not a real-time tape. Without it those positions get
                # no marks at all and book `unquotable` at a fictional $0.
                evm_px = None
                if not _FUNDED_ONLY and evm_price_due(cycle=cycle, last_run=last_evm_px,
                                 period_s=EVM_PRICE_PERIOD_S):
                    evm_px = evm_price_for
                # Sub-stamps (2026-09-27, measurement only): this block averaged 2.5-3 s a run on
                # the funded loop with no real position open, and `maint.bracket` cannot say which
                # part. Seconds per iteration, like every other phase; absent when a part raised.
                _tbr = time.time()
                _pb_kwargs = dict(
                    evm_price_for=evm_px,
                    evm_mcap_for=(evm_mcap_for if evm_px else None),
                    supplemental_price_map=_paper_swap_marks.snapshot(
                        now_ts=time.monotonic(), max_age_s=20.0),
                )
                if _FUNDED_ONLY:
                    m.normalize_open_position_hold_stamps(conn, now_iso=real_utc_now_iso())
                    _pb_started = False
                elif _PAPER_BRACKET_THREAD:
                    # PAPER BRACKET OFF THE FUNDED LOOP (2026-09-27): it measured 98% of this block.
                    # The hold-stamp normalize rewrites REAL rows too, so it stays on this thread.
                    m.normalize_open_position_hold_stamps(conn, now_iso=real_utc_now_iso())
                    _pb_started = start_paper_bracket(now_fn=real_utc_now_iso, **_pb_kwargs)
                else:
                    bracket_pass(conn, **_pb_kwargs)
                    _pb_started = True
                if evm_px and _pb_started:       # a skipped pass priced nothing: retry next bracket
                    last_evm_px = cycle
                _ph["maint.bracket.paper"], _tbr = time.time() - _tbr, time.time()
                # When the 1s fast lane DECIDES, this slow pass must not also decide: two lanes
                # evaluating the same position race on stop_hits and can double-submit an exit.
                # The fast lane is authoritative; this one keeps its telemetry/alert duties only.
                if contexts:
                    try:                    # (runs even if the kill switch is tripped; exits never gated)
                        run_slow_exit_sweep(
                            state=_fastlane_state,
                            exit_lock=_funded_exit_lock,
                            sweep_fn=lambda: real_exit_sweep(
                                conn, maintenance_contexts, network_ids=set(maintenance_contexts), lane="periodic_sweep",
                                reserve_witness_cache=_swap_mark_cache if _SOL_SWAP_MARKS else None,
                                derisk_settler=_rail_settler),
                            fastlane_enabled=_FASTLANE,
                            fastlane_decides=MEMECOIN_FASTLANE_DECIDES,
                            stale_after_s=_fastlane_stale_s,
                            # Contention means the FAST LANE is mid-exit holding the lock. Say so
                            # out loud: it used to be invisible, and the watchdog read the
                            # resulting frozen bracket_ts as a dead sweep and killed the runner
                            # mid-sell, 58 times since 2026-08-22.
                            on_contended=lambda: _p(
                                "[memecoin] slow exit sweep skipped: fast lane holds the exit "
                                "lock (it is mid-exit); bracket clock still advances"),
                        )
                        _ph["maint.bracket.real_sweep"], _tbr = time.time() - _tbr, time.time()
                        # operational-alert transitions (fire once on crossing) — these run on the
                        # slow pass ALWAYS, including when the fast lane owns the decision. They are
                        # once-on-crossing pages, not rails, and hoisting them would page every tick.
                        from sniper.util.clock import real_utc_now_iso as _rui
                        _day = _rui()[:10]
                        _sol_ctx = context_for(contexts, "solana")
                        _kill_tripped = check_kill_switch_alert(
                            _sol_ctx.get("kill_path") if _sol_ctx else None, _kill_tripped)
                        _daily_tripped = check_daily_loss_alert(conn, _day, _daily_tripped)
                        if _sol_ctx is not None:   # SOL gas reserve — Solana-only open count (EVM
                            _gas_low = check_gas_alert(  # positions exit with ETH, not SOL). EVM gas paging: follow-up.
                                _sol_ctx, m.real_open_snapshot(conn, network="solana")["count"], _gas_low)
                        _ph["maint.bracket.alerts"], _tbr = time.time() - _tbr, time.time()
                    except Exception:
                        _p("[err] real exit sweep failed:\n" + traceback.format_exc())
                _tbr = time.time()
                last_bracket = finish_bracket_cycle(
                    candidate_engine, cycle, now_fn=real_utc_now_iso,
                )
                _ph["maint.bracket.candidate"] = time.time() - _tbr
                if not launch_sweep_published:
                    # The heartbeat above predates this sweep; publish its stamp now rather than
                    # with the next cycle's heartbeat, a poll later. The deploy's entry gate waits
                    # on it (runner_launch_health). Heartbeat only; retried after the next sweep.
                    launch_sweep_published = publish_sweep(last_bracket,
                                                           started_at=runner_started_at)
                    if launch_sweep_published:
                        launch_published_at = time.time()
            # The runner checks manual intents after exit work on each main loop pass.
            _mp("bracket")
            if contexts:
                try:
                    # Only while the intent worker is unhealthy; a healthy worker returns [] here.
                    if _manual_intents is not None:
                        _manual_intents.fallback(conn)
                        # A STALLED pass holds pass_lock, so this fallback cannot cover it either:
                        # page once per stall (edge-triggered), never every cycle.
                        _mi_stalled = _manual_intents.stalled()
                        if _mi_stalled and not _mi_stall_paged[0]:
                            from sniper.services.memecoin_alerts import send_alert as _mi_alert
                            _mi_alert("MANUAL-INTENTS-STALLED",
                                      "intent worker pass busy %.0f s; manual and desk buys are "
                                      "waiting and the main-loop fallback cannot take the lock"
                                      % (_manual_intents.status().get("busy_age_s") or 0))
                        _mi_stall_paged[0] = _mi_stalled
                except Exception:
                    # A queue fault must not stop the runner.
                    _p("[err] manual intent drain failed:\n" + traceback.format_exc())
                # The operator's SELLS, right after the buys (and again at the next cycle head).
                _drain_manual_exits("bracket")
            _mp("contexts")
            # The pump / capture-v2 research pass runs on its own thread (_research_capture): inline
            # here, one DNS stall inside its DexScreener fetch stopped this loop for ~100 s on
            # 2026-09-25 and the watchdog killed the runner mid-retry.
            if cycle - last_sync > 300:     # ~5min fomo follow-graph sync (fail-closed)
                if not _J7_SOURCE:          # J7 source: fomo is never called
                    report_follow_sync(follow_sync_without_snapshot(conn, auth))
                last_sync = cycle
            _mp("follow_sync")
            if not _FUNDED_ONLY and cycle - last_cleanup > 3600:  # hourly paper cleanup
                from sniper.services.memecoin_exits import paper_unquotable_cleanup  # = no bracket can fire)
                from sniper.util.clock import real_utc_now_iso as _rui3
                cleaned = paper_unquotable_cleanup(conn, _rui3())
                if cleaned:
                    _p("[memecoin] unquotable cleanup: closed %d dark paper position(s)" % len(cleaned))
                last_cleanup = cycle
            _mp("cleanup")
            if contexts and time.monotonic() - last_reconcile > MEMECOIN_RECONCILE_PERIOD_S:
                from sniper.services.memecoin_alerts import alert_drift
                from sniper.util.clock import real_utc_now_iso as _rui2
                findings = real_reconcile(conn, maintenance_contexts, alert_fn=alert_drift,
                                          **_reconcile_main_scope)
                drifts = [f for f in findings if f.get("status") != "unreadable"]
                unreadable = [f for f in findings if f.get("status") == "unreadable"]
                if drifts:
                    _p("[memecoin] ⚠ RECONCILE drift on %d real position(s)" % len(drifts))
                if unreadable:
                    # not drift — the chain read failed. Logged, never alerted, re-checked next pass.
                    _p("[memecoin] RECONCILE could not read balance for %d position(s): %s"
                       % (len(unreadable), ", ".join(str(f["position_id"]) for f in unreadable)))
                # orphaned-reservation recovery: resolve reservations stuck 'reserved' against chain
                # (per-venue confirm router) so a crashed/unknown entry can't permanently block a mint
                # + eat an exposure slot.
                def _height_for(network_id):
                    # the exact-proof release needs the chain height; only Solana carries the
                    # write-ahead last_valid_height binding today
                    ctx = contexts.get(m.normalized_coin_network(network_id, None))                         if isinstance(contexts, dict) else None
                    fn = (ctx or {}).get("block_height_fn")
                    return fn() if fn else None
                recovered = recover_stale_reservations(
                    conn, confirm_for=_confirm_for, now_iso=_rui2(), alert_fn=alert_drift,
                    height_for=_height_for, confirm_proof_for=_confirm_proof_for,
                    confirmed_entry_for=_confirmed_entry_for,
                    confirmed_nonce_for=_confirmed_nonce_for,
                    token_balance_for=_token_balance_for,
                    pending_nonce_for=_pending_nonce_for,
                    proof_snapshot_for=_proof_snapshot_for, network_ids=set(maintenance_contexts))
                if recovered:
                    _p("[memecoin] ⚠ RESERVATION recovery: %s" % recovered)
                # RAILS-DEAF CHECK. Runs OUTSIDE the sweep on purpose: the sweep's own
                # rails_blind_streak cannot see a sweep that never ran, which is the 08-24 case
                # (87 min holding with an active stop and zero marks, no alert). Alert only —
                # a blind exit needs a price too.
                try:
                    from sniper.services import memecoin_rails_observability as _ro
                    from sniper.settings.memecoin import (
                        MEMECOIN_RAILS_BLIND_NOTICE_S as _RD_NOTICE,
                        MEMECOIN_RAILS_BLIND_ALERT_S as _RD_ALERT,
                        MEMECOIN_RAILS_BLIND_REALERT_S as _RD_REALERT)
                    from sniper.services.memecoin_telegram import send_telegram as _rd_tg
                    from sniper.services.memecoin_real_runner import _has_active_loss_rail
                    from sniper.services.memecoin_exits import _held_s
                    from sniper.settings import memecoin as _rd_cfg
                    import time as _rdt
                    # Same seams the sweep itself uses at memecoin_real_runner.py:2270 —
                    # effective_exit_override per position, and the settings module as cfg.
                    _open_real = m.list_open_positions(conn, mode="real", include_paused=False)
                    _rd_now = _rui2()
                    _finds, _worst, _blind = _ro.evaluate(
                        conn, now_iso=_rd_now, open_positions=_open_real,
                        has_loss_rail_fn=lambda p: _has_active_loss_rail(
                            p, m.effective_exit_override(conn, p["id"]), _rd_cfg),
                        opened_age_fn=lambda p: _held_s(
                            p.get("opened_recorded_at") or p.get("opened_at"), _rd_now),
                        notice_s=_RD_NOTICE, alert_s=_RD_ALERT)
                    rails_obs = _ro.snapshot(_worst, _blind, len(_open_real))
                    if _finds:
                        _p("[memecoin] rails observability: worst blind age %.0fs over %d "
                           "position(s), %d past the alert bound" % (_worst, len(_finds), _blind))
                    if _blind:
                        _ro.page_blind(_finds, now_mono=_rdt.monotonic(), alert_fn=_rd_tg,
                                       realert_s=_RD_REALERT)
                except Exception:
                    _p("[err] rails observability failed:\n" + traceback.format_exc())
                # ROUTE SERIES FLUSH: write the route quotes the exit path already computed and
                # buffered. Off the hot exit loop by construction — recording is in-memory, the DB
                # write happens here.
                try:
                    from sniper.services import memecoin_route_series as _rs
                    _rs_n = _rs.flush(conn)
                    if _rs_n:
                        _p("[memecoin] route series: flushed %d observation(s)" % _rs_n)
                except Exception:
                    _p("[err] route series flush failed:\n" + traceback.format_exc())
                # JUPITER LOAD FLUSH: per-minute quote/429 buckets, so the funded rate-limit
                # record survives a restart. Same shape as the route-series flush above — the
                # quote path only increments a dict; the DB write happens here.
                try:
                    from sniper.services import memecoin_jupiter_load_sink as _jl
                    from sniper.util.clock import real_utc_now_iso as _jl_now
                    _jl_n = _jl.flush(conn, now=_jl_now())
                    if _jl_n:
                        _p("[memecoin] jupiter load: flushed %d bucket(s)" % _jl_n)
                except Exception:
                    _p("[err] jupiter load flush failed:\n" + traceback.format_exc())
                # VOLUME-DEATH SHADOW: what a silence-based exit WOULD have done. Arms nothing,
                # exits nothing, changes no gate. Reuses the open-position set and loss-rail
                # predicate the staleness check just built.
                if _VOLDEATH_ENABLED and not _FUNDED_ONLY:
                    try:
                        from sniper.services import memecoin_volume_death as _vd
                        from sniper.settings.memecoin import (
                            MEMECOIN_VOLDEATH_THRESHOLDS_S as _VD_TH,
                            MEMECOIN_VOLDEATH_HEALTH_WINDOW_S as _VD_HW)
                        _vd_rows, _vd_tally = _vd.evaluate(
                            conn, now_iso=_rd_now, open_positions=_open_real,
                            has_loss_rail_fn=lambda p: _has_active_loss_rail(
                                p, m.effective_exit_override(conn, p["id"]), _rd_cfg),
                            thresholds=_VD_TH, health_window_s=_VD_HW,
                            hold_age_fn=lambda p: _ro.newest_mark_age_s(
                                conn, mint=p.get("mint"), now_iso=_rd_now,
                                chain_id=p.get("network_id")))

                        _vd_chain_by_pos = {p.get("id"): p.get("network_id")
                                            for p in (_open_real or [])}

                        def _vd_record(row, now_iso):
                            # The position row carries no price field, so take the newest STORED
                            # mark. No new quoting — this is already-persisted data.
                            # CHAIN-SCOPED (2026-08-27 review). The same mint string exists on
                            # more than one chain, so a mint-only lookup can store another chain's
                            # price as this position's evidence — the vol_death cross-chain class
                            # again. chain_id also LEADS idx_mc_marks_uniq, so this is an index
                            # seek rather than a scan of the mint's whole tape.
                            #
                            # The chain comes from the POSITION, not the row: evaluate() returns
                            # position_id/mint/silence_s/thresholds/blind_frozen/stream_healthy and
                            # no chain, so row.get("chain_id") is always None and would have made
                            # this query match nothing — trading a wrong-chain price for no price,
                            # which loses the evidence just as completely and more quietly.
                            _vd_chain = _vd_chain_by_pos.get(row.get("position_id"))
                            _mp = conn.execute(
                                "SELECT mid FROM memecoin_position_marks WHERE mint=? "
                                "AND chain_id=? ORDER BY sweep_ts DESC LIMIT 1",
                                (row.get("mint"), str(_vd_chain or ""))).fetchone() if _vd_chain else None
                            m.record_volume_death_shadow(
                                conn, position_id=row["position_id"], mint=row.get("mint"),
                                fired_at=now_iso, silence_s=row.get("silence_s"),
                                thresholds=row.get("thresholds"),
                                mark_price=(_mp[0] if _mp else None),
                                stream_healthy=row.get("stream_healthy"),
                                blind_frozen=row.get("blind_frozen"),
                                observable=row.get("observable"))

                        _vd_n = _vd.record_new_escalations(
                            conn, _vd_rows, now_iso=_rd_now, record_fn=_vd_record)
                        if _vd_n or _vd_tally.get("blind_frozen"):
                            _p("[memecoin] volume-death shadow: %s (+%d recorded)"
                               % (_vd_tally, _vd_n))
                    except Exception:
                        _p("[err] volume-death shadow failed:\n" + traceback.format_exc())
                last_reconcile = time.monotonic()
            # RENT SWEEP: return the SOL locked in empty token accounts left by closed positions.
            # Runs here, in the runner, because every real execution on this wallet serializes
            # behind one cross-process lock and a cron signer would sit outside it. Takes that lock
            # NON-blocking, so a pass yields to a live entry instead of delaying it.
            _mp("reconcile")
            if (_RENT_SWEEP_ENABLED and contexts and _custody_ready()
                    and cycle - last_rent_sweep > _RENT_SWEEP_PERIOD_S):
                last_rent_sweep = cycle
                try:
                    from sniper.services.memecoin_rent_sweep import rent_sweep_once
                    from sniper.settings.memecoin import (
                        MEMECOIN_RENT_SWEEP_MIN_ACCOUNTS as _RS_MIN,
                        MEMECOIN_RENT_SWEEP_MAX_ACCOUNTS as _RS_MAX,
                        MEMECOIN_SOLANA_RPC_URL as _RS_CUSTODY, solana_funded_rpc_urls as _RS_POOL)
                    _sol_ctx = (contexts or {}).get("solana") or {}
                    _owner = _sol_ctx.get("pubkey")
                    if _owner and _rent_keypair is not None:
                        _tally = rent_sweep_once(
                            conn, rpc_candidates=[_RS_CUSTODY] + list(_RS_POOL()),
                            keypair=_rent_keypair, owner_text=str(_owner),
                            min_accounts=_RS_MIN, max_accounts=_RS_MAX,
                            call_fn=_sol_ctx.get('rpc_call_fn'),
                            confirm_fn=(lambda sig, url: _sol_ctx['rpc_confirm_fn'](
                                sig, url, max_attempts=8, poll_s=.5)) if _sol_ctx.get('rpc_confirm_fn') else None)
                        _quiet = ("below_min", "wallet_busy", None)
                        if _tally.get("closed") or _tally.get("state") not in _quiet:
                            _p("[memecoin] rent sweep: %s" % _tally)
                except Exception:
                    _p("[err] rent sweep failed:\n" + traceback.format_exc())
            # GAS TOP-UP: buy a small, capped amount of SOL when the wallet is near the entry gate.
            # Checked far more often than the rent sweep — gas starvation stops trading silently,
            # which is how 2026-08-25 lost an afternoon. Two balance reads when gas is fine.
            _mp("rent_sweep")
            if (_GAS_TOPUP_ENABLED and contexts and _custody_ready()
                    and cycle - last_gas_topup > _GAS_TOPUP_PERIOD_S):
                last_gas_topup = cycle
                try:
                    from sniper.services.memecoin_gas_topup import gas_topup_once
                    from sniper.settings.memecoin import (
                        MEMECOIN_GAS_TOPUP_LOW_SOL as _GT_LOW,
                        MEMECOIN_GAS_TOPUP_USD as _GT_USD,
                        MEMECOIN_GAS_TOPUP_DAILY_CAP_USD as _GT_CAP,
                        MEMECOIN_SOLANA_RPC_URL as _GT_RPC,
                        solana_funded_rpc_urls as _RS_POOL2)
                    from sniper.util.clock import real_utc_now_iso as _rui4
                    _sctx = (contexts or {}).get("solana") or {}
                    _sol_fn, _usdc_fn = _sctx.get("sol_fn"), _sctx.get("usdc_fn")
                    _pk = _sctx.get("pubkey")

                    def _gt_reclaim():
                        """Sweep rent before buying gas. Runs INSIDE the top-up's own wallet lock,
                        so it passes already_locked — otherwise its non-blocking acquire would fail
                        against that lock and the reclaim would silently never happen.
                        MIN_ACCOUNTS is bypassed: the floor exists so a routine pass does not spend
                        a signature on one deposit, and here the alternative is spending bankroll."""
                        from sniper.services.memecoin_rent_sweep import rent_sweep_once
                        from sniper.settings.memecoin import (
                            MEMECOIN_RENT_SWEEP_MAX_ACCOUNTS as _RS_MAX2)
                        _t = rent_sweep_once(
                            conn, rpc_candidates=[_GT_RPC] + list(_RS_POOL2()),
                            keypair=_rent_keypair, owner_text=str(_pk),
                            min_accounts=1, max_accounts=_RS_MAX2, already_locked=True,
                            call_fn=_sctx.get('rpc_call_fn'),
                            confirm_fn=(lambda sig, url: _sctx['rpc_confirm_fn'](
                                sig, url, max_attempts=8, poll_s=.5)) if _sctx.get('rpc_confirm_fn') else None)
                        if _t.get("closed"):
                            _p("[memecoin] gas top-up: reclaimed %d account(s) = %.6f SOL "
                               "before considering a buy"
                               % (_t["closed"], _t.get("reclaimed_sol") or 0.0))
                        return int(_t.get("closed") or 0)
                    # UNWRAP THE READ. `sol_fn()` returns a NativeSolBalanceRead dataclass, not a
                    # number; gas_topup_once takes float-or-None, where None means UNREADABLE and
                    # refuses. Passing the object straight through raised inside the module's try
                    # on every pass, so the top-up would have logged 'error' every 300s and never
                    # fired — including on the day it was needed. Caught in review 2026-08-26.
                    def _sol_float():
                        _r = _sol_fn()
                        return _r.balance if getattr(_r, "ok", False) else None
                    if _pk and _rent_keypair is not None and _sol_fn and _usdc_fn:
                        _gt = gas_topup_once(
                            conn, sol_balance=_sol_float(), usdc_balance=_usdc_fn(),
                            keypair=_rent_keypair, pubkey=str(_pk), rpc_url=_GT_RPC,
                            now_iso=_rui4(), low_water_sol=_GT_LOW, topup_usd=_GT_USD,
                            daily_cap_usd=_GT_CAP, sol_balance_fn=_sol_float,
                            reclaim_fn=_gt_reclaim,
                            send_fn=_sctx.get('rpc_send_fn'),
                            confirm_fn=_sctx.get('rpc_confirm_fn'))
                        if _gt.get("state") not in ("above_low_water", "wallet_busy", None):
                            _p("[memecoin] gas top-up: %s" % _gt)
                except Exception:
                    _p("[err] gas top-up failed:\n" + traceback.format_exc())
            _mp("gas_topup")
            # OTHER HOLDINGS: list the tokens no position owns, and run one operator sell intent.
            # The sell takes the wallet lock non-blocking; a busy wallet leaves the intent queued.
            if _HOLDINGS_ENABLED and contexts and (contexts or {}).get("solana") and _custody_ready():
                try:
                    from sniper.services import memecoin_other_holdings as _oh
                    from sniper.settings.memecoin import (
                        MEMECOIN_HOLDINGS_MIN_SELL_USD as _OH_MIN,
                        MEMECOIN_SOLANA_RPC_URL as _OH_RPC, solana_funded_rpc_urls as _OH_POOL)
                    _ohc = contexts["solana"]
                    _ohpk = _ohc.get("pubkey")
                    # QUOTE-RESIDUE SWEEP (design 8.1): enqueue the holding sell below for residue
                    # an ExactIn quote leg left; ledger reads only. Its OWN try, so a failure can
                    # never skip the holding pass below; the flag is read first, so flag-off costs
                    # one attribute read (real_utc_now_iso() reads a file: not free per tick).
                    try:
                        from sniper.settings import memecoin as _qr_cfg
                        if getattr(_qr_cfg, "MEMECOIN_QUOTE_RESIDUE_SWEEP", False):
                            from sniper.services.memecoin_quote_residue import (
                                run_sweep_once as _qr_sweep)
                            _qr_sweep(conn, now_iso=real_utc_now_iso(), log=_p)
                    except Exception:
                        _p("[err] quote residue sweep failed:\n" + traceback.format_exc())
                    # END QUOTE-RESIDUE SWEEP
                    if _oh.thread_enabled():
                        # OFF THE FUNDED LOOP (2026-09-27): a worker thread with its own connection
                        # runs the sell, the resolve and the snapshot; this loop only computes the
                        # due flags. MEMECOIN_SOLANA_HOLDINGS_THREAD=0 runs them inline below.
                        _oh_due = cycle - last_holdings > _HOLDINGS_PERIOD_S
                        if _ohpk and _oh.start_pass(
                                _ohc, keypair=_rent_keypair, pubkey=str(_ohpk), rpc_url=_OH_RPC,
                                rpc_candidates=[_OH_RPC] + list(_OH_POOL()), snapshot_due=_oh_due,
                                sell_due=m.holding_sell_queued(conn, "solana"),
                                resolve_due=bool(m.unresolved_holding_sells(conn, "solana")),
                                min_sell_usd=_OH_MIN, now_fn=real_utc_now_iso,
                                log_fn=_p) and _oh_due:
                            last_holdings = cycle
                    else:
                        if _ohpk and cycle - last_holdings > _HOLDINGS_PERIOD_S:
                            last_holdings = cycle
                            _oht = _oh.snapshot_once(
                                conn, rpc_candidates=[_OH_RPC] + list(_OH_POOL()), owner=str(_ohpk),
                                now_iso=real_utc_now_iso(), call_fn=_ohc.get('rpc_call_fn'))
                            if _oht.get("state") != "ok":
                                _p("[memecoin] other holdings scan: %s" % _oht)
                        if (_ohpk and _rent_keypair is not None and _ohc.get("exit_quote_fn")
                                and m.holding_sell_queued(conn, "solana")):
                            _ohs = _oh.sell_once(
                                conn, keypair=_rent_keypair, pubkey=str(_ohpk), rpc_url=_OH_RPC,
                                now_iso=real_utc_now_iso(), exit_quote_fn=_ohc["exit_quote_fn"],
                                min_sell_usd=_OH_MIN,
                                receipt_proceeds_fn=_ohc.get("receipt_proceeds_fn"),
                                send_fn=_ohc.get('rpc_send_fn'), confirm_fn=_ohc.get('rpc_confirm_fn'))
                            if _ohs.get("state") not in ("wallet_busy", "idle", "book_busy"):
                                _p("[memecoin] other holdings sell: %s" % _ohs)
                                last_holdings = 0.0          # rescan next pass so the row updates
                        # Settle signed sells whose chain result was unknown (read-only; no lock).
                        if (_ohpk and _ohc.get("confirm_once_fn") and _ohc.get("block_height_fn")
                                and m.unresolved_holding_sells(conn, "solana")):
                            _ohr = _oh.resolve_once(
                                conn, pubkey=str(_ohpk), rpc_url=_OH_RPC, now_iso=real_utc_now_iso(),
                                confirm_once_fn=_ohc["confirm_once_fn"],
                                height_fn=_ohc["block_height_fn"],
                                receipt_proceeds_fn=_ohc.get("receipt_proceeds_fn"))
                            if _ohr:
                                _p("[memecoin] other holdings resolved: %s" % _ohr)
                                last_holdings = 0.0
                except Exception:
                    _p("[err] other holdings failed:\n" + traceback.format_exc())
            # Each enabled EVM chain gets its own scan / sell worker and cadence. Explorer/RPC
            # reads, quotes and receipt waits stay on the worker; this loop only checks for work.
            if _HOLDINGS_ENABLED and contexts:
                try:
                    from sniper.services import memecoin_evm_holdings as _eh
                    from sniper.settings.memecoin import MEMECOIN_HOLDINGS_MIN_SELL_USD as _EH_MIN
                    for _eh_chain in _eh.supported_chains():
                        _eh_ctx = contexts.get(_eh_chain)
                        if not _eh_ctx:
                            continue
                        try:
                            _eh_due = cycle - last_evm_holdings.get(_eh_chain, 0.0) > _HOLDINGS_PERIOD_S
                            if _eh.start_pass(
                                    _eh_ctx, snapshot_due=_eh_due,
                                    sell_due=m.holding_sell_queued(conn, _eh_chain),
                                    resolve_due=bool(m.unresolved_holding_sells(conn, _eh_chain)),
                                    min_sell_usd=_EH_MIN, now_fn=real_utc_now_iso, log_fn=_p) and _eh_due:
                                last_evm_holdings[_eh_chain] = cycle
                        except Exception:
                            _p("[err] %s other holdings failed:\n%s" % (
                                _eh_chain, traceback.format_exc()))
                except Exception:
                    _p("[err] EVM other holdings failed:\n" + traceback.format_exc())
            # OPERATOR WITHDRAWALS: send a queued dashboard withdrawal to the ONE configured
            # address, and settle any whose chain result was unknown. One cheap read when idle.
            if contexts and (contexts or {}).get("solana") and _custody_ready():
                try:
                    from sniper.settings.memecoin import (
                        MEMECOIN_WITHDRAW_SOLANA_ADDRESS as _WD_TO,
                        MEMECOIN_SOLANA_RPC_URL as _WD_RPC)
                    from sniper.services.memecoin_withdraw import start_pass as _withdraw_pass
                    _withdraw_pass(
                        contexts["solana"], state=m.withdraw_pending(conn, "solana"),
                        keypair=_rent_keypair, rpc_url=_WD_RPC, to_address=_WD_TO,
                        now_fn=real_utc_now_iso, log_fn=_p)
                except Exception:
                    _p("[err] withdrawal failed:\n" + traceback.format_exc())
            _mp("other_holdings")
            if (_EVM_GAS_TOPUP_ENABLED and contexts and _evm_gas_topup_worker is None
                    and cycle - last_evm_gas_topup > _EVM_GAS_TOPUP_PERIOD_S):
                last_evm_gas_topup = cycle
                try:
                    _egt = run_evm_gas_topup_pass(
                        conn, contexts=contexts, now_iso=real_utc_now_iso(),
                        gas_alert_state=_evm_gas_alert_state)
                    if _egt.get("state") not in ("above_low_water", "wallet_busy", None):
                        _p("[memecoin] EVM gas top-up: %s" % _egt)
                except Exception:
                    _p("[err] EVM gas top-up failed:\n" + traceback.format_exc())
            # RESIDUAL TAIL: hand each new funded TP close to its own timer. EVERY CYCLE, and not
            # behind the 30s post-TP gate — the study's first horizon is 2s, so spawn latency is
            # the measurement's error bar. One indexed read of closes since a watermark; the
            # workers do the rest on their own clock and their own connections.
            #
            # MEASUREMENT ONLY. This reads closed positions after the fact and writes to its own
            # table. It cannot delay, alter, or fail an exit, and nothing in a decision path reads
            # what it writes (post-exit doctrine, 2026-07-27).
            _mp("evm_gas_topup")
            # SEED THE LADDER'S V4 POOLKEY CACHE FROM OUR OWN TAPE (2026-09-24). The volume writer
            # sees a pool's `Initialize` event before its first swap and keeps the verified key;
            # V4 discovery otherwise relearns it from a provider a median 346 s after the pool's
            # first tick (p90 417 s), which is exactly the window a freshly graduated coin trades
            # in. Read-only on the tape, writes only the ladder's cache, admits nothing.
            if cycle - last_v4_key_seed > _V4_KEY_SEED_PERIOD_S:
                last_v4_key_seed = cycle
                try:
                    from sniper.services.memecoin_v4_key_seed import seed_v4_pool_keys
                    for _chain in ("4663", "8453"):
                        _n = seed_v4_pool_keys(
                            conn, chain=_chain, since_iso=_seed_since_default,
                            progress=_v4_key_seed_since.setdefault(_chain, {}))
                        if _n:
                            _p("[memecoin] v4 key seed: %s pools from the tape on %s"
                               % (_n, _chain))
                except Exception:
                    _p("[err] v4 key seed failed:\n" + traceback.format_exc())
            _mp("v4_key_seed")
            if not _FUNDED_ONLY and _RESIDUAL_TAIL_ENABLED and contexts and (contexts or {}).get("solana"):
                try:
                    from sniper.services import memecoin_residual_tail_worker as _rt
                    from sniper.execution.solana_real_seams import (
                        jupiter_exit_quote as _rt_jexit, token_supply as _rt_supply)
                    from sniper.settings.memecoin import MEMECOIN_SOLANA_RPC_URL as _RT_RPC
                    from sniper.persistence.db_core import get_connection as _rt_connect
                    for _c in m.recent_real_tp_closes_with_principal(
                            conn, since_iso=_residual_since, limit=20):
                        if _c["position_id"] in _residual_seen:
                            continue
                        _residual_seen.add(_c["position_id"])
                        _rt.spawn_for_close(
                            _c, connect_fn=_rt_connect,
                            decimals_fn=lambda mint: _rt_supply(mint, _RT_RPC)[1],
                            exit_quote_fn=_rt_jexit, now_fn=real_utc_now_iso,
                            fee_buffer_pct=_RESIDUAL_FEE_BUFFER_PCT)
                        _residual_since = max(_residual_since, _c["closed_at"] or _residual_since)
                    if len(_residual_seen) > 500:
                        _residual_seen.clear()      # the watermark is the real dedup; this is slack
                except Exception:
                    _p("[err] residual tail spawn failed:\n" + traceback.format_exc())
            # POST-TP FOLLOW-UP: price the tail we stop watching after a take-profit. MEASUREMENT
            # ONLY — it schedules from CLOSED positions and writes to its own tables. Nothing here
            # is read by a decision, and it cannot touch the exit path it observes.
            _mp("residual_tail")
            if (not _FUNDED_ONLY and _POST_TP_ENABLED and contexts
                    and cycle - last_post_tp > _POST_TP_PERIOD_S):
                last_post_tp = cycle
                try:
                    from sniper.services.memecoin_post_tp_followup import post_tp_pass
                    from sniper.settings.memecoin import (
                        MEMECOIN_POST_TP_FOLLOWUP_PER_PASS as _PT_N)
                    from sniper.util.clock import real_utc_now_iso as _rui5
                    # The funded solana context exposes NO exit-quote or decimals seam, so these
                    # come from the shared read-only helpers directly. jupiter_exit_quote is a
                    # Jupiter HTTP call and touches no RPC; token_supply is cached forever per mint
                    # (a pump.fun mint has its authority revoked, so supply cannot change).
                    from sniper.execution.solana_real_seams import (
                        jupiter_exit_quote as _jexit, token_supply as _tsupply)
                    from sniper.settings.memecoin import MEMECOIN_SOLANA_RPC_URL as _PT_RPC
                    _sc = (contexts or {}).get("solana") or {}
                    if _sc:
                        _pt = post_tp_pass(
                            conn, since_iso=_post_tp_since, now_iso=_rui5(),
                            decimals_fn=lambda mint: _tsupply(mint, _PT_RPC)[1],
                            exit_quote_fn=_jexit,
                            limit=_PT_N)
                        if _pt.get("scheduled") or _pt.get("quoted"):
                            _p("[memecoin] post-TP follow-up: %s" % _pt)
                except Exception:
                    _p("[err] post-TP follow-up failed:\n" + traceback.format_exc())
            # SOURCE-SELL MIRROR COUNTERFACTUAL: what the bag a mirror sold was worth 600 s later,
            # one exact quote per mirror close, measurement only (same tables, its own horizon).
            if (not _FUNDED_ONLY and _source_mirror is not None and contexts.get("solana")
                    and cycle - last_mirror_cf > _POST_TP_PERIOD_S):
                last_mirror_cf = cycle
                try:
                    from sniper.services.memecoin_post_tp_followup import post_tp_pass
                    from sniper.execution.solana_real_seams import (
                        jupiter_exit_quote as _jexit, token_supply as _tsupply)
                    from sniper.settings.memecoin import MEMECOIN_SOLANA_RPC_URL as _CF_RPC
                    from sniper.util.clock import real_utc_now_iso as _rui6
                    _cf = post_tp_pass(
                        conn, since_iso=_post_tp_since, now_iso=_rui6(),
                        decimals_fn=lambda mint: _tsupply(mint, _CF_RPC)[1],
                        exit_quote_fn=_jexit, limit=1,
                        exit_reasons=("mirror_source_sell",), horizons=(600,))
                    if _cf.get("scheduled") or _cf.get("quoted"):
                        _p("[memecoin] source-sell mirror counterfactual: %s" % _cf)
                except Exception:
                    _p("[err] source-sell mirror counterfactual failed:\n"
                       + traceback.format_exc())
            _mp("post_tp")
            if not _FUNDED_ONLY and os.getenv("MEMECOIN_SNAPSHOT_ENABLED", "1").strip().lower() in {"1", "true", "yes"}:
                from sniper.util.clock import real_utc_now_iso
                now_real = real_utc_now_iso()
                today = now_real[:10]   # real-UTC calendar day (not the +7h host day boundary)
                if today != last_snapshot_day:
                    from sniper.services.memecoin_snapshot import daily_snapshot_pass
                    snap = daily_snapshot_pass(conn, auth, now_real)
                    last_snapshot_day = today
                    _p("[memecoin] daily snapshot: leaderboard=%d snapshots=%d"
                          % (snap["leaderboard"], snap["snapshots"]))
            _mp("snapshot")
        except Exception:
            finish_db_cycle(conn, failed=True)
            # a maintenance-pass failure must not take down the loop or the copy heartbeat
            # (run_once already wrote its own); just log and keep the exit sweep cadence.
            _p("[err] maintenance pass failed:\n" + traceback.format_exc())
        else:
            finish_db_cycle(conn)
        _ph['maint'] = time.time() - cycle - _ph.get('copy', 0.0) - _ph.get('auth', 0.0)
        # PHASE COST, ACCUMULATED ACROSS THE REPORTING WINDOW (2026-09-04). The loop targets a 1s
        # cycle and measured 5.03s median, dead flat from 66 to 80 open positions -- a fixed wait,
        # not per-position work. Narrowing from outside eliminated CPU (19.5% of one core), EVM RPC
        # (3.9 calls, 1.20s), DexScreener (66ms), SQLite locks (21 in the whole log) and the fomo
        # rate guard (engaged 26 times a day, peak gap 2.0s), and could not close it. Measure it.
        if cycle - last_log > 30:
            last_log = cycle
            curated = len(m.curated_user_ids(conn))
            openpos = len(m.list_open_positions(conn))
            _rt = (" real{%s}" % ",".join(f"{k}={v}" for k, v in sorted(real_tally.items()))
                   if real_tally else "")
            # DIVIDE BY ITERATIONS, NOT BY `n`. `n` only increments on a SUCCESSFUL feed poll,
            # so using it as the denominator silently inflated every phase by the failure ratio and
            # made the loop look four times slower than it runs.
            _span = max(1, _iters - _iters_at_report)
            _wall = cycle - _report_wall if _report_wall else 0.0
            _ps = (" iters=%d wall=%.1fs period=%.2fs phase{%s}" % (
                _span, _wall, (_wall / _span) if _span else 0.0,
                ",".join("%s=%.3f" % (_k, _phase_tot[_k] / _span) for _k in sorted(_phase_tot))))
            _phase_tot.clear(); _phase_since_n = n
            _iters_at_report = _iters; _report_wall = cycle
            _p("[memecoin] cycles=%d curated=%d opened=%d exits=%d open_now=%d%s%s"
                  % (n, curated, opened, exits, openpos, _rt, _ps))
        # Fill-anchored position->pool resolution, at the END of the iteration by design so its
        # RPC never sits between a signal arriving and an order going out. Placed BEFORE the
        # finish_db_cycle below on purpose: it opens a read snapshot, and that release must be the
        # last thing before the sleep or the runner pins the WAL for the whole poll interval.
        _t_tail = time.time()
        if _pool_tc is not None:
            _resolve_position_pools_tick(conn, _pool_tc)
        _ph['pool_resolve'] = time.time() - _t_tail
        # The status reads above start a fresh SQLite snapshot after the maintenance commit.
        # Release it before sleeping or the runner pins the WAL for the whole poll interval.
        _t_fin = time.time()
        finish_db_cycle(conn)
        _ph['db_finish'] = time.time() - _t_fin
        # WHOLE-CYCLE TRUTH. The three phases above summed to ~1.5s of a measured 5.0s cycle, so
        # most of it was AFTER the last marker: the fill-anchored pool resolution and the snapshot
        # release, both of which run every cycle and both of which do I/O. Account for the whole
        # loop, so the phases can never silently fail to add up again.
        _ph['cycle'] = time.time() - cycle
        if _iters == 1:
            _p(first_cycle_line(_ph, None if launch_published_at is None
                                else launch_published_at - cycle))
        for _k, _v in _ph.items():
            _phase_tot[_k] = _phase_tot.get(_k, 0.0) + float(_v)
        _rest = max(0.0, POLL_MS / 1000.0 - (time.time() - cycle))
        # The work cadence stays unchanged. Only cached provider status is republished
        # during idle time; live J7 events retain their immediate wake ahead of telemetry.
        wait_for_idle_heartbeat(_rest, provider=_infra_provider, funded_only=_FUNDED_ONLY,
            wait_fn=_j7_feed.wait if _j7_feed is not None else lambda duration: time.sleep(duration),
            heartbeat_fn=_publish_idle_heartbeat)


if __name__ == "__main__":
    main()
