"""One poll cycle: fomo feed -> normalize -> curated filter -> engine.

`run_once` is the unit-testable core; the FomoFeedAdapter.tick calls it each poll.
"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import sys
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from sniper.feeds.fomo_feed import (
    fomo_get, build_feed_path, normalize_feed_observation, normalize_thesis_item,
)
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_copy_trader import arm_base_size, on_copy_event
from sniper.execution.solana_quote import honest_fill
from sniper.util.state_paths import state_path

# Runner liveness beacon — the loop rewrites this each cycle; the /health endpoint
# reads its age + last poll status to answer "is the system actually copying?".
HEARTBEAT_PATH = os.environ.get(
    "MEMECOIN_HEARTBEAT_PATH", str(state_path("data", "memecoin_runner.heartbeat")))
# Latest full /feed snapshot (ALL traders, buys+sells) so the UI Activity tab can mirror
# fomo's live alerts feed without an extra fomo call per page load.
FEED_SNAPSHOT_PATH = os.environ.get(
    "MEMECOIN_FEED_SNAPSHOT_PATH", str(state_path("data", "memecoin_feed.json")))


def _capture_pump_outcome(conn, ev, now_iso, min_usd):
    """Open a post-buy price-path capture for a NEW large-buy sighting (curated or discovery),
    so we can later rank the trader by 'do their buys pump'. No-op unless enabled + a large
    buy with a reference price. Dedups on fomo_event_id inside create_signal_outcome."""
    from sniper.settings.memecoin import MEMECOIN_PUMP_ENABLED
    if not (MEMECOIN_PUMP_ENABLED and ev.side == "buy" and ev.their_price
            and (ev.their_size_usd or 0) >= min_usd):
        return
    try:
        # received_at is corrected real-UTC and represents when the signal became actionable.
        m.create_signal_outcome(conn, fomo_event_id=ev.fomo_event_id,
                                 source_user_id=ev.source_user_id, handle=ev.source_handle,
                                 mint=ev.mint, ticker=ev.ticker, buy_ts=ev.received_at,
                                 ref_price=ev.their_price, their_size_usd=ev.their_size_usd,
                                 now_iso=now_iso, network_id=getattr(ev, "network_id", None))
    except Exception:
        pass   # pump capture is best-effort telemetry; never break the copy path
    try:
        from sniper.services.memecoin_capture_v2 import create_outcome_v2
        from sniper.settings.memecoin import MEMECOIN_PUMP_HORIZONS_S
        create_outcome_v2(
            conn, upstream_event_id=ev.fomo_event_id, source_user_id=ev.source_user_id,
            handle=ev.source_handle, network_id=getattr(ev, "network_id", None), mint=ev.mint,
            ticker=ev.ticker, signal_observed_at=ev.received_at, ref_price=ev.their_price,
            horizons=MEMECOIN_PUMP_HORIZONS_S, created_at=now_iso,
        )
    except Exception:
        pass   # v2 capture is independent telemetry and cannot break the copy path


def _capture_theses(conn, raw_items, recv_iso, server_epoch, curated):
    """Store curated traders' thesis_created posts (display/context only — never a copy signal).
    Curated-only, best-effort: a parse/write error must never break the copy path.

    received_at is stamped in CORRECTED real-UTC (not the host clock, which runs ~+7h): the
    thesis path is independent of the pump path (which needs host-clock received_at), so keeping
    thesis received_at on real-UTC makes token_thesis_counts' window and _ageRealIso correct."""
    from sniper.settings.memecoin import MEMECOIN_THESIS_ENABLED
    if not MEMECOIN_THESIS_ENABLED:
        return 0
    from sniper.util.clock import real_utc_now_iso
    recv_corrected = real_utc_now_iso()
    n = 0
    for r in raw_items:
        try:
            th = normalize_thesis_item(r, recv_corrected, server_epoch)
            if th and th.source_user_id in curated:
                if conn is not None and _record_thesis(conn, th):
                    n += 1
        except Exception:
            pass
    return n


def _record_thesis(conn, th):
    return m.record_thesis(conn, th.__dict__)


def _prepare_alert(conn, ev):
    """Synchronous half of a front-run alert: dedup (record_alert) + thesis lookup + build the
    message. Returns a job {fomo_event_id, message, local_ms} for the sender, or None if this
    event was already alerted. Fast (DB write + one read) — safe on the copy path.

    Wall-clock stamps use CORRECTED real-UTC (detected_at + alert_sent_at same domain, so diffing
    them is honest — the host clock runs ~+7h). local_ms measures our receipt->send-start gap
    (record_alert commit + thesis lookup + message build)."""
    from sniper.util.clock import real_utc_now_iso
    from sniper.services.memecoin_telegram import build_alert_message
    from sniper.settings import memecoin as _cfg
    # STALE SUPPRESSION (2026-07-16): a signal older than MEMECOIN_TG_MAX_LAG_S is unactionable —
    # do not send it at all (labeling it 'stale' still spams the channel). Unknown/negative lag
    # sends (fail-open; the builder labels it) — suppress only on POSITIVE known lag.
    try:
        _lag = float(ev.lag_s) if getattr(ev, "lag_s", None) is not None else None
    except (TypeError, ValueError):
        _lag = None
    if _lag is not None and 0 < _cfg.MEMECOIN_TG_MAX_LAG_S < _lag:
        return None
    t0 = time.perf_counter()
    detected = getattr(ev, "received_at_normalized", None) or getattr(ev, "received_at", None)
    if not m.record_alert(conn, {
            "fomo_event_id": ev.fomo_event_id, "source_user_id": ev.source_user_id,
            "handle": ev.source_handle, "mint": ev.mint, "ticker": ev.ticker,
            "their_size_usd": ev.their_size_usd, "market_cap": ev.their_market_cap,
            "fomo_created_at": ev.created_at, "detected_at": detected,
            "lag_s": ev.lag_s, "signal_source": ev.signal_source,
            "network_id": getattr(ev, "network_id", None) or None}):
        return None   # already alerted this event
    thesis = None
    try:
        rows = m.list_theses(conn, uid=ev.source_user_id, mint=ev.mint, limit=1)
        thesis = rows[0] if rows else None
    except Exception:
        thesis = None
    # Exact-chain funded authority + measurable paper evidence. Best-effort: the latency
    # instrument's message must still build if enrichment fails.
    context = None
    try:
        from sniper.settings import memecoin as _cfg
        from sniper.services.memecoin_chains import funded_chain_keys, resolve_chain
        chain = resolve_chain(getattr(ev, "network_id", None))
        funded = chain in funded_chain_keys()
        armed_chains = m.wallet_armed_chains(conn, ev.source_user_id)
        # THE GATE'S OWN READ (2026-09-11). `wallet_armed_chains` parses `real_chains` only; the
        # roster gate also requires real_approved=1, paused=0 and excluded=0. A paused wallet
        # keeps its chain list, so the raw read printed ARMED for a wallet the gate refuses.
        armed_on_chain = bool(
            funded and ev.source_user_id in m.real_approved_user_ids(conn, network_id=chain))
        g = m.alpha_b_copy_stats(conn, ev.source_user_id, chain=chain) if funded else None
        gate_ok = bool(
            g and g["n_closes"] >= _cfg.MEMECOIN_REAL_PROMOTE_MIN_CLOSES
            and g["pnl"] >= _cfg.MEMECOIN_REAL_PROMOTE_MIN_PNL_USD)
        prior = m.prior_buyers_60m(conn, mint=ev.mint, network_id=getattr(ev, "network_id", None),
                                   source_user_id=ev.source_user_id, before_iso=ev.created_at)
        context = {"real_armed_chains": armed_chains,
                   "real_armed": armed_on_chain,
                   "authority_status": ("armed" if armed_on_chain else
                                        "watch_only" if funded else "unfunded_chain"),
                   "gate_applicable": funded,
                   "chain": chain,
                   "paper_pnl": g["pnl"] if g else None,
                   "paper_n": g["n_closes"] if g else None,
                   "gate_ok": gate_ok,
                   "prior_buyers_60m": prior}
        # A VERDICT THE ALERT ALREADY KNOWS (2026-09-11). This alert goes out BEFORE the funded
        # hook runs, so it normally says "decision pending" and the result pages later. But the
        # roster gate refuses an unarmed wallet `not_approved` on this same read, and
        # `_alert_entry_result` pages nothing for `not_approved`. So "pending" was the last word
        # and it was false: Faucet/Tekkerrss said pending, refused 25 ms after the send.
        if funded and not armed_on_chain and _cfg.MEMECOIN_REAL_ROSTER_GATE_ENABLED:
            context.update({"entry_status": "refused", "entry_code": "not_approved",
                            "entry_reason": "wallet not armed on %s" % chain})
    except Exception:
        pass
    message = build_alert_message(ev, thesis=thesis, sent_at=detected, context=context)
    local_ms = (time.perf_counter() - t0) * 1000.0
    job = {"fomo_event_id": ev.fomo_event_id, "message": message, "local_ms": local_ms}
    # MCAP FOR THE OPERATOR'S EYES, OFF THE COPY PATH (2026-09-11). J7 relays carry no market cap
    # on 12% of buys (53 of 65 on 4663, where DexScreener is blind by design), so the alert prints
    # "mcap ~?". The follow-activity lane resolved this with its DexScreener display helper, and that lane
    # is off under J7. This function runs BEFORE the funded hook, so the provider call cannot
    # live here; the SENDER thread fills it in (`finish_alert_job`) and rebuilds the message.
    if not (getattr(ev, "their_market_cap", None) or 0) and (context or {}).get("display_market_cap") is None:
        job["rebuild"] = {"ev": ev, "thesis": thesis, "detected": detected, "context": context}
    return job


def _display_mcap_lookup(mint, network_id, price):
    """Display-only market cap: DexScreener first; on a DexScreener-blind chain (4663), the
    relay's own price x cached on-chain totalSupply -- the same `build_evm_mcap_for` the marks
    use, so a coin the marks priced costs no extra read. None when nothing answers."""
    from sniper.services.memecoin_follow_activity import _live_display_mcap
    value = _live_display_mcap(mint, network_id)
    if value or not price or str(network_id or "") in ("", "solana"):
        return value
    from sniper.services.memecoin_evm_price import build_evm_mcap_for
    return build_evm_mcap_for()(network_id, mint, price)


def finish_alert_job(job):
    """Fill a missing display market cap and rebuild the message. Sender thread only; never
    raises, never changes a decision -- the value is display-only by construction."""
    rebuild = (job or {}).get("rebuild")
    if not rebuild:
        return job
    from sniper.services.memecoin_telegram import build_alert_message
    try:
        ev = rebuild["ev"]
        mcap = _display_mcap_lookup(getattr(ev, "mint", None), getattr(ev, "network_id", None),
                                    getattr(ev, "their_price", None))
        if mcap:
            context = dict(rebuild["context"] or {})
            context["display_market_cap"] = mcap
            job["message"] = build_alert_message(ev, thesis=rebuild["thesis"],
                                                 sent_at=rebuild["detected"], context=context)
    except Exception:
        logger.exception("alert mcap fill failed (alert still sent)")
    return job


def _send_prepared(conn, fomo_event_id, message, local_ms, alert_fn):
    """Blocking half: send the prepared message and record its send latency. In production this
    runs on a dedicated sender thread (its OWN conn) so the ~8s Telegram round-trip never stalls
    the copy path or the exit sweep. Returns the send-ok flag."""
    from sniper.util.clock import real_utc_now_iso
    from sniper.services.memecoin_telegram import telegram_audit_payload
    ok, ms = alert_fn(message)
    audit = telegram_audit_payload(message)
    m.update_alert_sent(conn, fomo_event_id, real_utc_now_iso(), ok, ms, local_ms=local_ms,
                        **audit)
    return ok


def want_front_run_alert(ev, curated, *, prior_fn):
    """Should this feed buy alert? Curated buys: always (over the size floor). NON-curated buys
    (2026-07-17, operator: fomo's UI shows signals our TG never does): only as the 2nd+ distinct
    buyer on a sub-CONSENSUS_MAX_MCAP coin within 60m — the consensus signal (49% hit20 in the
    1-2M study) — never the solo noise (13.8-20% hit20). prior_fn raises/None -> no alert
    (an alert is advisory; unlike entry gates it may fail closed)."""
    from sniper.settings import memecoin as _cfg
    if not (_cfg.MEMECOIN_TG_ALERTS and getattr(ev, "side", None) == "buy"):
        return False
    if (getattr(ev, "their_size_usd", 0) or 0) < _cfg.tg_min_buy_usd(
            getattr(ev, "their_market_cap", None)):
        return False
    # mcap ceiling applies to EVERY alert kind — curated included (2026-08-17, operator).
    if not _cfg.tg_mcap_ok(getattr(ev, "their_market_cap", None)):
        return False
    if ev.source_user_id in curated:
        return True
    if not _cfg.MEMECOIN_TG_CONSENSUS_ALERTS:
        return False
    mc = getattr(ev, "their_market_cap", None) or 0
    if not (0 < mc < _cfg.MEMECOIN_TG_CONSENSUS_MAX_MCAP):
        return False
    try:
        prior = prior_fn(ev)
    except Exception:
        return False
    return (prior or 0) >= 1


def _want_source_intent(conn, ev, curated, alert_queue):
    """Defer consensus reads to the worker when the shared dispatcher is present."""
    if hasattr(alert_queue, "enqueue_source"):
        return want_front_run_alert(ev, curated, prior_fn=lambda _ev: 1)
    return want_front_run_alert(ev, curated, prior_fn=lambda e:
        m.prior_buyers_60m(conn, mint=e.mint, network_id=getattr(e, "network_id", None),
                          source_user_id=e.source_user_id, before_iso=e.created_at))


def _maybe_alert(conn, ev, alert_fn):
    """Synchronous prepare+send (no queue) — used by tests and any single-threaded caller.
    Returns True iff a NEW alert was sent this call."""
    job = _prepare_alert(conn, ev)
    if not job:
        return False
    job = finish_alert_job(job)
    _send_prepared(conn, job["fomo_event_id"], job["message"], job["local_ms"], alert_fn)
    return True


def _dispatch_front_run_alert(conn, ev, alert_fn, alert_queue):
    """Dispatch one advisory alert without ever blocking or breaking the copy path.

    Returns ``(accepted, dropped)``. A full async queue counts as dropped; build/send failures
    remain best-effort and do not mutate funded execution.
    """
    try:
        if hasattr(alert_queue, "enqueue_source"):
            return (1, 0) if alert_queue.enqueue_source(ev) else (0, 1)
        if alert_queue is None:
            return (1, 0) if _maybe_alert(conn, ev, alert_fn) else (0, 0)
        job = _prepare_alert(conn, ev)
        if not job:
            return 0, 0
        try:
            alert_queue.put_nowait(job)
            return 1, 0
        except queue.Full:
            return 0, 1
    except Exception:
        return 0, 0


def write_feed_snapshot(events, path=FEED_SNAPSHOT_PATH):
    """Replace the source snapshot atomically. Preserve source and receipt identity."""
    import tempfile
    from sniper.util.clock import real_utc_now_iso
    temporary = None
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        rows = [{"handle": e.source_handle, "uid": e.source_user_id, "side": e.side,
                 "ticker": e.ticker, "mint": e.mint, "network_id": getattr(e, "network_id", None),
                 "size_usd": e.their_size_usd, "source_price": getattr(e, "their_price", None),
                 "market_cap": e.their_market_cap, "created_at": e.created_at,
                 "origin_event_id": getattr(e, "fomo_event_id", None),
                 "fomo_event_id": getattr(e, "fomo_event_id", None),
                 "received_at": getattr(e, "received_at", None),
                 "observed_at": getattr(e, "received_at_normalized", None) or getattr(e, "received_at", None),
                 "clock_domain": getattr(e, "clock_domain", None)} for e in events[:60]]
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as f:
            temporary = f.name
            json.dump({"events": rows, "snapshot_at": real_utc_now_iso(), "error": None}, f)
        os.replace(temporary, path)
    except Exception:
        logger.exception("memecoin source snapshot write failed")
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def read_feed_snapshot_status(path=FEED_SNAPSHOT_PATH):
    """Expose the snapshot time and a typed read error."""
    try:
        with open(path) as f:
            result = json.load(f)
        if not isinstance(result, dict) or not isinstance(result.get("events"), list):
            raise ValueError("InvalidSnapshot")
        return {"events": result["events"], "snapshot_at": result.get("snapshot_at"), "error": None}
    except Exception as exc:
        return {"events": [], "snapshot_at": None, "error": type(exc).__name__}


def read_feed_snapshot(path=FEED_SNAPSHOT_PATH):
    return read_feed_snapshot_status(path)["events"]


_LIFI_STATUS_FIELDS = (
    "corrected_at", "runner_heartbeat_at", "release_sha", "manifest_hash", "chain_id",
    "custody_wallet", "mode", "authenticated", "effective_limit", "remaining", "reset_s",
    "in_flight", "exit_reserve", "slots_used", "slot_limit", "entry_admitted",
    "disable_reasons", "unresolved_approvals", "unresolved_swaps", "unresolved_revokes",
    "unresolved_finality",
)
_RESEARCH_KEY_FIELDS = ("key", "state", "bench_left_s", "last_reason", "calls")
_RESEARCH_CAPTURE_FIELDS = ("last_pass_at", "last_pass_s", "passes", "failures", "last_error",
                            "transaction_span_n", "transaction_span_ms_p50",
                            "transaction_span_ms_p90", "transaction_span_ms_max")
_SUBMIT_AUDIT_FIELDS = ("queued", "dropped", "failed", "written")
_PAPER_BRACKET_FIELDS = ("alive", "pass_started_at", "last_pass_s", "failures")
_OTHER_HOLDINGS_FIELDS = ("alive", "pass_started_at", "last_pass_s", "last_snapshot_at", "failures",
                          "evm_ready_chains")
_EVM_GAS_TOPUP_FIELDS = ("last_pass_at", "last_pass_s", "last_state", "passes", "failures",
                         "last_error")
_PAPER_FANOUT_FIELDS = ("alive", "depth", "queued", "dropped_full", "censored_late", "processed",
                        "opened", "failed", "lag_ms_p50", "lag_ms_p90", "last_job_at")

_FOMO_ACTIVITY_WS_STATUS_FIELDS = (
    "status", "connected_at", "subscribed_at", "last_payload_at", "last_error_at",
    "reconnect_count",
)


def _slot_feed_readiness(feed, now_mono):
    """Observation diagnosis only. Unknown evidence never becomes a funded readiness gate."""
    blockers, unknown = [], []
    if feed.get('connected') is False:
        blockers.append('disconnected')
    elif feed.get('connected') is not True:
        unknown.append('connection_unknown')
    requested, acknowledged = feed.get('requested_owners'), feed.get('acknowledged_owners')
    if requested is None or acknowledged is None:
        unknown.append('owner_ack_unknown')
    elif set(requested) != set(acknowledged):
        blockers.append('owner_ack_pending')
    elif not requested:
        blockers.append('empty_roster')
    stamp = feed.get('head_slot_mono')
    if feed.get('head_slot') is None:
        unknown.append('head_unknown')
    elif stamp is None:
        unknown.append('head_clock_unknown')
    elif now_mono - stamp > 2.0:
        blockers.append('head_stale')
    return {'state': 'blocked' if blockers else 'unknown' if unknown else 'ready',
            'blockers': blockers, 'unknown': unknown}


def write_heartbeat(result, auth_ok, path=HEARTBEAT_PATH, ts=None, bracket_ts=None,
                    tpu_status=None, rpc_pool_status=None, fastlane_status=None,
                    started_at=None, rails_observability=None, pool_resolver=None,
                    jupiter_load=None, route_series=None, lifi_status=None,
                    fomo_activity_ws_status=None, evm_rpc_pool_status=None, j7_feed_status=None,
                    onchain_volume_feed=None, research_rpc_pool=None, research_capture=None,
                    submit_audit=None, address_coverage=None, slot_lane_status=None,
                    paper_fanout=None, evm_gas_topup=None, other_holdings=None,
                    paper_bracket=None, early_quote=None, coin_age=None, dexscreener=None,
                    raydium_late_shadow=None,
                    dip_lane=None, pinned_reoffer=None, exit_tape=None, entry_retry_workers=None, wallet_entry_caps=None, ethereum_worker=None, manual_exits=None, exit_sweep_timing=None, lp_rail=None, manual_intents=None,
                    rail_derisk_settler=None,
                    standing_allowance=None, chain1_fee_headroom=None, base_fast_lane=None,
                    gate_balance_warm=None, quote_price_warmers=None, sol_buffer=None, desk_prequote=None,
                    rh_fast_lane=None, j7_refusal_diag=None,
                    _base_payload=None):
    """Best-effort liveness beacon; never raises into the poll loop. `bracket_ts` = time of the
    last SUCCESSFUL exit sweep, so a dead SL/TP sweep is visible even when the poll loop is fresh."""
    result = result or {}
    payload = dict(_base_payload) if _base_payload is not None else {}
    payload.update({
        "ts": time.time() if ts is None else ts,
        "auth_ok": bool(auth_ok),
        "events": result.get("events", 0),
        "opened": result.get("opened", 0),
    })
    from sniper.settings import memecoin as cfg
    payload["solana_execution"] = {
        "native_exit_enabled": cfg.MEMECOIN_SOLANA_NATIVE_EXIT,
        "exotic_round_trip_enabled": cfg.MEMECOIN_SLOT_LANE_EXOTIC_ROUND_TRIP,
    }
    if bracket_ts is not None:
        payload["bracket_ts"] = bracket_ts
    if dip_lane is not None:
        # The dip hit lane's last-pass age (memecoin_dip_retry.lane_status): a lane that stopped
        # passing is visible here before its page fires.
        payload["dip_lane"] = dict(dip_lane)
    if ethereum_worker is not None:
        payload["ethereum_worker"] = dict(ethereum_worker)
    if manual_exits is not None:
        payload["manual_exits"] = dict(manual_exits)
    if exit_sweep_timing is not None:
        # Exit-sweep wall time per lane (rolling p50/p90/max, n); observation only.
        payload["exit_sweep_timing"] = dict(exit_sweep_timing)
    if lp_rail is not None:
        payload["lp_rail"] = dict(lp_rail)
    if manual_intents is not None:
        # The intent worker (manual + desk buys, build step 2): same status shape as manual_exits.
        payload["manual_intents"] = dict(manual_intents)
    if rail_derisk_settler is not None:
        # RailDeriskSettler: rails behind an unknown-confirm partial. Counters and a type name only.
        payload["rail_derisk_settler"] = {
            key: (value if (type(value) is int and value >= 0) or type(value) is bool
                  or (key == "last_error" and isinstance(value, str) and value.isidentifier()
                      and len(value) <= 80) else None)
            for key, value in dict(rail_derisk_settler).items()
            if key in ("offered", "throttled", "dropped", "confirmed", "failed", "unknown",
                       "errors", "last_error", "alive", "queued")}
    if standing_allowance is not None:
        # The standing quote-token allowance book: the count per spender and top-up outcomes.
        payload["standing_allowance"] = dict(standing_allowance)
    if sol_buffer is not None:
        # The native-SOL execution buffer (memecoin_sol_buffer_watch.BufferWatch): last refusal, free
        # USDC, native SOL value, last refill and how long it has been short. Read-only numbers.
        payload["sol_buffer"] = dict(sol_buffer)
    if gate_balance_warm is not None:
        # The EVM gate balance warmer's thread (memecoin_real_runner.GateBalanceWarmer).
        payload["gate_balance_warm"] = dict(gate_balance_warm)
    if quote_price_warmers is not None:
        # Per chain (memecoin_real_runner.quote_price_warmer_status): ready, last_error,
        # thread_alive, age_s. fastlane.bsc_price_preparation stays as it was for 56.
        # Outside the write's try: a value that is not a dict is published as an error, never raised
        # (Gamma HOLD on 748bffd23: one bad value would have failed the whole heartbeat every tick).
        try:
            payload["quote_price_warmers"] = {
                str(k): (dict(v) if isinstance(v, dict) else {"last_error": type(v).__name__})
                for k, v in dict(quote_price_warmers).items()}
        except Exception as exc:
            payload["quote_price_warmers"] = {"_error": {"last_error": type(exc).__name__}}
    # The v4_multihop bridge-leg memo (evm_v4_multihop.bridge_memo_status, 2026-10-17): entries,
    # served/stored, refresh rounds and what each GeckoTerminal refresh met. Only when this process
    # has the module loaded (it never imports it for the heartbeat); a raise is published, not raised.
    _multihop = sys.modules.get("sniper.execution.evm_v4_multihop")
    if _multihop is not None:
        try:
            payload["multihop_bridge_memo"] = dict(_multihop.bridge_memo_status())
        except Exception as exc:
            payload["multihop_bridge_memo"] = {"_error": type(exc).__name__}
    if rh_fast_lane is not None:
        # The 4663 fast lane's inline safety host rotation (memecoin_rh_fast_lane.status): the active host's
        # 1-based index (null when none is healthy), each host's provider label and state, rotation counters.
        # Whitelisted and typed: nothing else from the status leaves, and never a URL.
        hosts_in = rh_fast_lane.get("inline_hosts") if isinstance(rh_fast_lane, dict) else None
        if isinstance(hosts_in, dict):
            def _int(v):
                return v if type(v) is int and v >= 0 else None
            def _label(value):
                if not isinstance(value, str):
                    return None
                parts = value.split(" ")
                if len(parts) != 5 or parts[0] != "host" or parts[2] != "of":
                    return None
                if any(not (1 <= len(v) <= 2 and v.isascii() and v.isdigit() and 1 <= int(v) <= 99)
                       for v in (parts[1], parts[3])):
                    return None
                domain = parts[4]
                if not (domain.startswith("(") and domain.endswith(")")):
                    return None
                domain = domain[1:-1]
                return value if domain == "?" or (1 <= len(domain) <= 63 and all(c in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in domain)) else None
            states = ("healthy", "quota spent", "dead", "429 pause", "identity pending", "wrong chain (refused)")
            counters = ("rotated", "all_unavailable", "recovered", "refused", "identity_expired")
            payload["rh_fast_lane"] = {"inline_hosts": {
                "n": _int(hosts_in.get("n")), "active": _int(hosts_in.get("active")),
                "hosts": [{"label": _label(h.get("label")),
                           "state": h.get("state") if h.get("state") in states else None}
                          for h in (hosts_in.get("hosts")[:8] if isinstance(hosts_in.get("hosts"), list) else []) if isinstance(h, dict)],
                "rotations": {k: v for k, v in (hosts_in.get("rotations") or {}).items() if k in counters and type(v) is int and v >= 0}
                if isinstance(hosts_in.get("rotations"), dict) else {}}}
    if base_fast_lane is not None:
        # The Base 8453 fast lane (memecoin_base_fast_lane.status): mode, snapshot reads/errors and
        # its last success age, the lane host's paced calls/429s, datagrams received and dropped by
        # reason, signals judged and verdict counts. Absent while the lane does not run.
        payload["base_fast_lane"] = dict(base_fast_lane)
    if chain1_fee_headroom is not None:
        # The chain-1 fee-history reader (chain1_protect.status): reads, errors, the current failure
        # streak, the last error, and the snapshot's age (stale -> signing takes today's fees).
        payload["chain1_fee_headroom"] = dict(chain1_fee_headroom)
    if entry_retry_workers is not None:
        payload["entry_retry_workers"] = {str(k): dict(v)
                                         for k, v in entry_retry_workers.items()}
    if exit_tape is not None:
        # The exit tape's counters (memecoin_exit_tape.snapshot): a dead writer shows as
        # alive=false and a rising `dropped`, never as a quiet book.
        payload["exit_tape"] = dict(exit_tape)
    if pinned_reoffer is not None:
        # The pinned-close re-offer's counters (memecoin_exit_rebroadcast.snapshot): copies sent,
        # refused, exhausted, and how many are in flight now.
        payload["pinned_reoffer"] = dict(pinned_reoffer)
    if started_at is not None:
        payload["started_at"] = started_at
    if tpu_status is not None:
        payload["tpu"] = dict(tpu_status)
    if dexscreener is not None:
        # Calls and suppressed calls per DexScreener consumer (phase A): a consumer the removal
        # missed shows up here as a count.
        payload["dexscreener"] = dict(dexscreener)
    if wallet_entry_caps is not None:
        payload["wallet_entry_caps"] = dict(wallet_entry_caps)
    if rpc_pool_status is not None:
        payload["solana_rpc_pool"] = dict(rpc_pool_status)
    if evm_rpc_pool_status is not None:
        # 2026-09-03: 4663 provider health was invisible -- a dead custody endpoint read as an outage
        # of everything. Hosts only, never urls (a custody url may carry an API key).
        payload["evm_rpc_pool"] = {str(k): list(v) for k, v in dict(evm_rpc_pool_status).items()}
    if fastlane_status is not None:
        payload["fastlane"] = dict(fastlane_status)
    if slot_lane_status is not None:
        # Named explicitly (whitelist). Who the slot lane watches, so a newly armed owner that the
        # lane has not picked up is visible (2026-09-27: the watcher showed 39 and nothing showed
        # the lane's count).
        signal = slot_lane_status.get("signal") or {}
        payload["slot_lane"] = {
            "mode": slot_lane_status.get("mode"), "owners": slot_lane_status.get("owners"),
            **{key: slot_lane_status.get(key) for key in (
                'fast_lane_only', 'max_copy_premium_pct', 'receipt_resume_ready', 'j7_verified_backup',
                'copy_market_movement_only', 'copy_market_unknown_policy',
                'nozomi_ab_ready', 'nozomi_wallet_scope') if key in slot_lane_status},
            "armed": slot_lane_status.get("armed"), "handled": slot_lane_status.get("handled"),
            "subscriptions": signal.get("subscriptions"), "connected": signal.get("connected"),
            "events": signal.get("events"), "sells": signal.get("sells"),
            "decode_errors": signal.get("decode_errors"), "reconnects": signal.get("reconnects"),
            **{key: signal[key] for key in ("requested_owners", "acknowledged_owners", "slippage_guard")
               if key in signal}}
        # Unmeasured exotic hop labels that signed the blind CU budget (settings, 2026-10-29).
        payload["slot_lane"]["exotic_cu_blind"] = cfg.slot_lane_exotic_cu_blind_counts()
        if 'source_partials' in slot_lane_status:
            worker = slot_lane_status['source_partials']
            status = None
            if isinstance(worker, dict):
                status = {key: worker[key] if type(worker[key]) is bool else None
                          for key in ('alive', 'healthy', 'busy', 'stalled') if key in worker}
                for key in ('last_pass_age_s', 'busy_age_s'):
                    if key in worker:
                        value = worker[key]
                        status[key] = value if (type(value) in (int, float)
                            and 0 <= value <= sys.float_info.max) else None
                # Presence of an error is useful; its text must never reach the heartbeat.
                if 'error' in worker:
                    status['error'] = 'worker_error' if type(worker['error']) is str and worker['error'] else None
                if 'wake' in worker:
                    wake = worker['wake']
                    status['wake'] = None
                    if isinstance(wake, dict):
                        status['wake'] = {key: wake[key] if (
                            type(wake[key]) is bool if key == 'bound' else
                            type(wake[key]) is int and 0 <= wake[key] < 2**63) else None
                            for key in ('bound', 'datagrams', 'bind_failures') if key in wake}
                        if 'last_error' in wake:
                            status['wake']['last_error'] = ('wake_error' if
                                type(wake['last_error']) is str and wake['last_error'] else None)
            payload['slot_lane']['source_partials'] = status
        entry = slot_lane_status.get('entry_opportunity')
        if isinstance(entry, dict):
            from sniper.services.memecoin_entry_opportunity import _uid
            policy = {key: entry.get(key) if type(entry.get(key)) is bool else None for key in (
                'enabled', 'first_buy_only', 'head_callback_ready', 'landing_deadline_guaranteed')}
            for key in ('default_max_slot_delta', 'head_max_age_ms'):
                value = entry.get(key)
                policy[key] = value if type(value) is int and 0 <= value < 2**63 else None
            ids = entry.get('qualified_exception_ids')
            policy['qualified_exception_ids'] = [value for value in ids
                if type(value) is str and len(value) <= 64 and _uid(value)] \
                if isinstance(ids, (list, tuple)) else None
            policy['deadline_basis'] = (entry.get('deadline_basis') if entry.get('deadline_basis')
                in ('observed_processed_head', 'onchain_clock_slot') else None)
            payload['slot_lane']['entry_opportunity'] = policy
        startup = slot_lane_status.get('startup_gate')
        if isinstance(startup, dict):
            errors = startup.get('errors')
            payload['slot_lane']['startup_gate'] = {
                'state': startup.get('state') if startup.get('state') in ('staged', 'ready') else None,
                'errors': [value for value in (errors if isinstance(errors, (tuple, list)) else ())
                           if type(value) is str and len(value) <= 80
                           and value.replace(':', '_').isidentifier()]}
        # Diagnostic tokens/counters only; never journals, endpoints or provider payloads.
        for name, fields in (
                ('execution_observer', ('state', 'reason', 'generation')),
                ('solana_volume_gate', ('state', 'pools', 'skipped', 'last_error',
                    'enabled', 'min_m5_mcap_ratio', 'max_age_s', 'window_s')),
                ('exotic_warm', ('noted', 'hits', 'misses', 'seeded', 'refreshed',
                    'refresh_failed', 'rate_limited', 'expired', 'evicted', 'busy',
                    'http_calls', 'last_error', 'funded_429_skips', 'renewed', 'cap'))):
            diagnostic = slot_lane_status.get(name)
            if isinstance(diagnostic, dict):
                sanitized = {}
                for key in fields:
                    value = diagnostic.get(key)
                    if key in ('state', 'reason', 'last_error'):
                        valid = type(value) is str and len(value) <= 80 and value.isidentifier()
                    elif key == 'enabled':
                        valid = type(value) is bool
                    elif key in ('min_m5_mcap_ratio', 'max_age_s', 'window_s'):
                        valid = type(value) in (int, float) and 0 < value <= sys.float_info.max
                    else:
                        valid = type(value) is int and value >= 0
                    sanitized[key] = value if valid else None
                payload['slot_lane'][name] = sanitized
        warm = slot_lane_status.get('exotic_warm')
        if isinstance(warm, dict) and isinstance(payload['slot_lane'].get('exotic_warm'), dict):
            # WHY REFRESHES FAIL (2026-10-17, Codex speed audit item 3): typed tokens and counts only.
            def _token(value, size=80):
                return (value if type(value) is str and 0 < len(value) <= size
                        and value.replace(':', '_').replace('-', '_').isidentifier() else None)
            out = payload['slot_lane']['exotic_warm']
            failure = warm.get('last_failure')
            if 'last_failure' in warm:
                out['last_failure'] = None if not isinstance(failure, dict) else {
                    'stage': _token(failure.get('stage'), 32), 'category': _token(failure.get('category')),
                    'http_status': failure.get('http_status') if type(failure.get('http_status')) is int else None,
                    'quote_mint': (failure.get('quote_mint') if type(failure.get('quote_mint')) is str
                                   and failure.get('quote_mint').isalnum()
                                   and len(failure.get('quote_mint')) <= 48 else None),
                    'clip_atoms': (failure.get('clip_atoms') if type(failure.get('clip_atoms')) is int
                                   and failure.get('clip_atoms') >= 0 else None)}
            stages = warm.get('failure_stages')
            if 'failure_stages' in warm:
                out['failure_stages'] = {
                    key: count for key, count in (stages.items() if isinstance(stages, dict) else ())
                    if _token(key) is not None and type(count) is int and count >= 0}
        observer = slot_lane_status.get('execution_observer')
        if isinstance(observer, dict) and observer.get('provider') == 'shyft':
            # Typed status only; no URLs, tokens, filter addresses, receipts or journals.
            diagnostic = payload['slot_lane'].get('execution_observer', {})
            for key in ('provider', 'hostname', 'commitment', 'connected',
                    'acknowledged_generation', 'requested_count', 'acknowledged_count',
                    'requested_filter_fingerprint', 'acknowledged_filter_fingerprint',
                    'head_slot', 'reconnects', 'observations', 'duplicates', 'coverage_gaps',
                    'last_gap', 'journal_evicted'):
                if key not in observer:
                    continue
                value = observer[key]
                if key == 'hostname':
                    valid = value in ('grpc.ams.shyft.to', 'grpc.fra.shyft.to')
                elif key.endswith('_fingerprint'):
                    valid = type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)
                elif key == 'connected':
                    valid = type(value) is bool
                elif key in ('provider', 'commitment', 'last_gap'):
                    valid = type(value) is str and len(value) <= 80 and value.isidentifier()
                else:
                    valid = type(value) is int and value >= 0
                diagnostic[key] = value if valid else None
            for name, stamp_name in (('head_age_s', 'head_monotonic'),
                                     ('last_frame_age_s', 'last_frame_monotonic')):
                stamp = observer.get(stamp_name)
                valid = type(stamp) in (int, float) and math.isfinite(stamp) and stamp >= 0
                if stamp_name == 'head_monotonic':
                    # Preserve the receiver's clock; publishing cannot refresh stale evidence.
                    diagnostic[stamp_name] = stamp if valid else None
                diagnostic[name] = (round(max(0., time.monotonic() - stamp), 1)
                    if valid else None)
            payload['slot_lane']['execution_observer'] = diagnostic
        routed = signal.get("routed")
        accounts = slot_lane_status.get('account_stream')
        if isinstance(accounts, dict):
            accounts = accounts.copy()
            payload['slot_lane']['account_stream'] = {key: accounts.get(key) for key in (
                'endpoint', 'commitment', 'connected', 'subscription_acknowledged',
                'connection_generation', 'watch_generation', 'requested_account_count',
                'coverage', 'accounts', 'updates', 'reconnects', 'stale_updates',
                'stale_generations', 'queue_overflows', 'consumer_stale', 'seed_failures',
                'seed_errors', 'last_error', 'signed_updates', 'lp_unsigned_rows')}
            payload['slot_lane']['account_stream']['acknowledged_account_count'] = len(
                accounts.get('acknowledged_accounts') or ())
            stamp = accounts.get('last_frame_monotonic')
            payload['slot_lane']['account_stream']['last_frame_age_s'] = (
                round(max(0.0, time.monotonic() - stamp), 1) if stamp is not None else None)
        if signal.get('arrival_metrics') is not None:
            payload['slot_lane']['feed_arrivals'] = signal['arrival_metrics'].snapshot()
        if signal.get('raw_arrival_metrics') is not None:     # every owner-signed frame, buy or not
            payload['slot_lane']['feed_arrivals_raw'] = signal['raw_arrival_metrics'].snapshot()
        if slot_lane_status.get('confirmation_workers') is not None:
            payload['slot_lane']['confirmation_workers'] = dict(slot_lane_status['confirmation_workers'])
        timing = slot_lane_status.get('queue_timing')
        if timing is not None:
            payload['slot_lane']['queue_timing'] = {
                **{key: timing.get(key) for key in ('handled', 'queue_expired', 'already_old',
                    'wallet_busy', 'unknown', 'sample', 'window')},
                'queue_ms': {key: (timing.get('queue_ms') or {}).get(key) for key in ('p50', 'p90', 'max')}}
        metadata = signal.get('metadata_worker')
        if metadata is not None:
            payload['slot_lane']['metadata_worker'] = {key: metadata.get(key) for key in (
                'capacity', 'offered', 'overflow', 'decoded', 'errors', 'roster_refused', 'last_error')}
            # Refused processed updates by validator reason (Gamma 2026-10-14); a bounded key set.
            payload['slot_lane']['metadata_worker']['metadata_refused'] = dict(
                metadata.get('metadata_refused') or {})
        preparation = signal.get('preparation')
        preparation_queue = slot_lane_status.get('preparation')
        if preparation is not None or preparation_queue is not None:
            preparation = preparation or {}
            preparation_queue = preparation_queue or {}
            prepare_feed = (preparation.get('feed') or {}).copy()
            prepare_pending = preparation.get('pending') or {}
            from sniper.services.memecoin_shyft_prepare import EUROPE_HOSTS, MISS_REASONS
            payload['slot_lane']['preparation'] = {
                'queue': {key: preparation_queue.get(key) for key in (
                    'capacity', 'retention_s', 'per_tick', 'queued', 'overflow', 'expired',
                    'paced_drop', 'unsupported', 'delivered', 'errors')},
                'pending': {key: prepare_pending.get(key) for key in (
                    'capacity', 'retention_s', 'offered', 'retained', 'duplicate', 'expired',
                    'overflow', 'matched', 'mismatch', 'late', 'failed')},
                'feed': {key: prepare_feed.get(key) for key in (
                    'connected', 'connection_generation', 'reconnects', 'transactions', 'decoded',
                    'misses', 'before_ack', 'callback_errors', 'last_error',
                    'requested_owners', 'acknowledged_owners')},
                'callback_errors': preparation.get('callback_errors')}
            # IDL-decoded DFlow legs into a venue that takes no references yet (MEMECOIN_SLOT_LANE_RABBIT_SIGNAL).
            payload['slot_lane']['preparation']['queue']['no_prebuild'] = {
                key[len('no_prebuild_'):]: value for key, value in preparation_queue.items()
                if key.startswith('no_prebuild_') and type(value) is int}
            rabbit = payload['slot_lane']['preparation']['feed']
            reasons = prepare_feed.get('miss_reasons')
            rabbit['miss_reasons'] = None
            if isinstance(reasons, dict):
                rabbit['miss_reasons'] = {}
                for key in MISS_REASONS:
                    count = reasons.get(key, 0)
                    rabbit['miss_reasons'][key] = count if type(count) is int and count >= 0 else None
                errors = reasons.get('observer_errors')
                rabbit['observer_errors'] = errors if type(errors) is int else None

            shapes = prepare_feed.get('alt_frames')        # how lookup-table frames arrive
            rabbit['alt_frames'] = dict(shapes) if isinstance(shapes, dict) else None
            hostname = prepare_feed.get('endpoint_hostname')
            rabbit['endpoint_hostname'] = hostname if hostname in EUROPE_HOSTS else None
            stamp = prepare_feed.get('last_frame_mono')
            rabbit['last_frame_age_s'] = (
                round(max(0.0, time.monotonic() - stamp), 1) if stamp is not None else None)
        rabbit_buy = signal.get('rabbit_buy')
        if isinstance(rabbit_buy, dict):         # MEMECOIN_SLOT_LANE_RABBIT_BUY: fires and refusals
            payload['slot_lane']['rabbit_buy'] = {key: rabbit_buy.get(key) for key in (
                'candidates', 'held', 'duplicate', 'unsigned', 'unsupported', 'busy', 'read_errors',
                'processed_first', 'roster_refused', 'fired', 'fire_errors', 'ping_errors',
                'read_ms_p50', 'read_ms_p90', 'last_error')}
            payload['slot_lane']['rabbit_buy']['refused'] = dict(rabbit_buy.get('refused') or {})
            # The read guard's counters (memecoin_rabbit_signal, Gamma B2): copied, so the loop's
            # later updates never touch a row being written.
            for key in ('read_basis', 'read_slot_delta', 'read_matrix', 'by_route', 'frame_log', 'by_feed'):
                payload['slot_lane']['rabbit_buy'][key] = json.loads(json.dumps(rabbit_buy.get(key) or {}))
            # The AMM handlers (memecoin_rabbit_dflow_amm, memecoin_rabbit_okx_amm): each one's status(),
            # taken under its own lock; the drift samples arrive as {n, p50, p90}.
            handler_status = {}
            for route, read in dict(rabbit_buy.get('handler_status') or {}).items():
                try:
                    handler_status[route] = json.loads(json.dumps(read()))
                except Exception:
                    handler_status[route] = {'error': 'status_unreadable'}
            payload['slot_lane']['rabbit_buy']['handler_status'] = handler_status
            if rabbit_buy.get('amm_unwired'):
                payload['slot_lane']['rabbit_buy']['amm_unwired'] = dict(rabbit_buy.get('amm_unwired'))
            payload['slot_lane']['rabbit_buy']['read_miss'] = rabbit_buy.get('read_miss')
            payload['slot_lane']['rabbit_buy']['handlers'] = list(rabbit_buy.get('handlers') or ())
        rabbit_tape = signal.get('rabbit_tape')
        if isinstance(rabbit_tape, dict):         # MEMECOIN_SLOT_LANE_RABBIT_TAPE: the pool tape stream
            since = rabbit_tape.get('connected_mono')
            span = (time.monotonic() - since) if since is not None else None
            by_filter = {}
            for label, row in dict(rabbit_tape.get('by_filter') or {}).items():
                frames, size = (list(row) + [0, 0])[:2]
                by_filter[label] = {'frames': frames, 'bytes': size,
                                    'frames_per_s': round(frames / span, 1) if span else None,
                                    'kb_per_s': round(size / 1024 / span, 1) if span else None}
            store = rabbit_tape.get('store')
            try:
                store = json.loads(json.dumps(store())) if callable(store) else None
            except Exception:
                store = {'error': 'status_unreadable'}
            payload['slot_lane']['rabbit_tape'] = {
                **{key: rabbit_tape.get(key) for key in (
                    'connected', 'reconnects', 'frames', 'bytes', 'watch', 'resends', 'addresses_added', 'last_error',
                    'thread_cpu_s', 'host', 'dial_target', 'tls_authority', 'connect_ip')},
                'connected_s': round(span, 1) if span is not None else None,
                'resends_per_min': (round(rabbit_tape.get('resends', 0) * 60.0 / span, 2) if span else None),
                'addresses_added_per_min': (round(rabbit_tape.get('addresses_added', 0) * 60.0 / span, 1)
                                            if span else None),
                'by_filter': by_filter, 'store': store}
        def _preconf_export(preconf):
            feed = dict(preconf.get('feed') or {})
            out = {
                'feed': {key: feed.get(key) for key in (
                    'connected', 'acknowledged', 'reconnects', 'frames', 'malformed', 'binary_before_ack',
                    'owners', 'last_error', 'connection_generation', 'callback_errors')},
                'credits': {key: (preconf.get('credits') or {}).get(key) for key in (
                    'day', 'credits', 'messages', 'covered_slots', 'cap', 'capped', 'paused_until', 'pages')},
                'owner_frames': preconf.get('owner_frames'), 'candidates': preconf.get('candidates'),
                'offered': preconf.get('offered'),
                'refused': dict(preconf.get('refused') or {}),
                'frame_log': json.loads(json.dumps(preconf.get('frame_log') or {}))}
            stamp = feed.get('last_frame_mono')
            out['feed']['last_frame_age_s'] = (
                round(max(0.0, time.monotonic() - stamp), 1) if stamp is not None else None)
            return out
        preconf = signal.get('preconf')
        if isinstance(preconf, dict):            # MEMECOIN_SLOT_LANE_PRECONF: the socket, frames, credits
            payload['slot_lane']['preconf'] = _preconf_export(preconf)   # the first source, as before
        by_source = signal.get('preconf_sources')
        if isinstance(by_source, dict):          # one socket per includeSource (2026-10-17)
            payload['slot_lane']['preconf_sources'] = {
                str(source): _preconf_export(entry) for source, entry in by_source.items()
                if isinstance(entry, dict)}
        landing = signal.get('rabbit_landing')
        if isinstance(landing, dict):             # each fire's source: landed, failed, not landed
            payload['slot_lane']['rabbit_landing'] = {key: landing.get(key) for key in (
                'watched', 'landed', 'source_failed', 'source_not_landed', 'feed_missed', 'replaced',
                'unknown', 'check_errors', 'node_behind', 'verdict_errors', 'observe_errors')}
        crowded = slot_lane_status.get('crowded_fee')
        if isinstance(crowded, dict):             # the crowded priority-fee test: arms, spend, cap
            crowded = dict(crowded)
            payload['slot_lane']['crowded_fee'] = {key: crowded.get(key) for key in (
                'rule', 'target_lamports', 'day', 'spent_lamports', 'accounting_state', 'daily_cap_lamports', 'capped',
                'treated', 'holdout', 'sent_treated', 'bound_at')}
            payload['slot_lane']['crowded_fee']['not_eligible'] = dict(crowded.get('not_eligible') or {})
        breaker = slot_lane_status.get('rabbit_breaker')
        if isinstance(breaker, dict):             # the hot knob, the breaker, fills and landing slots
            breaker = dict(breaker)
            payload['slot_lane']['rabbit_breaker'] = {key: breaker.get(key) for key in (
                'knob', 'paused', 'paused_remaining_s', 'trips', 'window', 'sent', 'fills',
                'exits_closed', 'realized_usd', 'last_trip_at', 'last_resume')}
            payload['slot_lane']['rabbit_breaker']['verdicts'] = dict(breaker.get('verdicts') or {})
            payload['slot_lane']['rabbit_breaker']['land_minus_s'] = breaker.get('land_minus_s')
            preconf_breaker = breaker.get('preconf')
            if isinstance(preconf_breaker, dict):  # the preconf fire's own knob and breaker
                preconf_breaker = dict(preconf_breaker)
                payload['slot_lane']['rabbit_breaker']['preconf'] = {key: preconf_breaker.get(key) for key in (
                    'knob', 'paused', 'paused_remaining_s', 'trips', 'window', 'sent', 'fills',
                    'last_trip_at', 'last_resume')}
                payload['slot_lane']['rabbit_breaker']['preconf']['verdicts'] = dict(
                    preconf_breaker.get('verdicts') or {})
                payload['slot_lane']['rabbit_breaker']['preconf']['land_minus_s'] = preconf_breaker.get(
                    'land_minus_s')
        if signal.get('signal_feeds') is not None:
            payload['slot_lane']['signal_feeds'] = {}
            payload['slot_lane']['feed_readiness'] = {}
            for name, feed in signal['signal_feeds'].copy().items():
                payload['slot_lane']['signal_feeds'][name] = {key: feed.get(key) for key in (
                    'connected', 'owners', 'subscriptions', 'events', 'sells',
                    'transactions', 'decoded', 'decode_errors',
                    'wins', 'duplicates', 'last_signature', 'reconnects', 'last_error', 'head_slot',
                    'native_wins', 'native_duplicates', 'routed_wins', 'routed_duplicates',
                    'requested_owners', 'acknowledged_owners', 'migration_filter_requested',
                    'migration_filter_acknowledged', 'migration_filter_generation',
                    'migration_frames', 'migration_pools')}
                payload['slot_lane']['signal_feeds'][name].update({key: feed[key] for key in (
                    'damm_filter_requested', 'damm_filter_acknowledged', 'damm_filter_generation',
                    'damm_frames', 'damm_creations', 'connection_generation', 'watch_generation',
                    'requested_watch_generation', 'acknowledged_watch_generation',
                    'requested_filter_fingerprint', 'acknowledged_filter_fingerprint') if key in feed})
                payload['slot_lane']['feed_readiness'][name] = _slot_feed_readiness(feed.copy(), time.monotonic())
                head_stamp = feed.get('head_slot_mono')
                payload['slot_lane']['signal_feeds'][name]['head_age_s'] = (
                    round(max(0., time.monotonic() - head_stamp), 1) if head_stamp is not None else None)
                stamp = feed.get('last_frame_mono')
                payload['slot_lane']['signal_feeds'][name]['last_frame_age_s'] = (
                    round(max(0.0, time.monotonic() - stamp), 1) if stamp is not None else None)
        if routed is not None:
            # Routed-buy pairing on the same socket (2026-09-27). Whitelisted; last_error is a
            # refusal message or an exception class, never a URL.
            payload["slot_lane"]["routed"] = {key: routed.get(key) for key in (
                "mode", "subs_ok", "subs_refused", "ready", "emitted", "ambiguous",
                "ambiguous_after", "expired", "baseline_failed", "last_error")}
        prewarm = slot_lane_status.get("prewarm")
        cash_legs = slot_lane_status.get("cash_legs")
        if callable(cash_legs):                   # {venue: {usdc_atomic: {age_s, fetched_at, primary}}}
            try:
                payload["slot_lane"]["cash_legs"] = json.loads(json.dumps(cash_legs()))
            except Exception:
                payload["slot_lane"]["cash_legs"] = {"error": "status_unreadable"}
        if slot_lane_status.get('route_cache_churn') is not None:
            payload['slot_lane']['route_cache_churn'] = dict(slot_lane_status['route_cache_churn'])
        if prewarm is not None:
            # The migration listener and the pool cache it feeds (2026-09-27). Whitelisted; the
            # listener's last_error is a class name, never a URL.
            heard = prewarm.get("last_frame_mono")
            cache = slot_lane_status.get("pool_cache") or {}
            payload["slot_lane"]["prewarm"] = {
                **{key: prewarm.get(key) for key in (
                    "connected", "frames", "pools", "reconnects", "last_error")},
                **{key: prewarm[key] for key in (
                    "source", "ready", "queued", "dropped", "delivered", "errors") if key in prewarm},
                "last_frame_age_s": (round(time.monotonic() - heard, 1)
                                     if heard is not None else None),
                **{key: cache.get(key) for key in (
                    "known", "streamed", "stream_stored", "stream_dropped",
                    "read_ms_p50", "read_ms_p90", "warm_unresolved_streak", "warm_retry_n")}}
            # The second socket and the pooled stream (2026-10-07): each socket keeps its own counters;
            # `pooled.deaf` is the alarm Gamma's watch keys on.
            research = prewarm.get("research")
            if research is not None:
                seen = research.get("last_frame_mono")
                payload["slot_lane"]["prewarm"]["research"] = {
                    **{key: research.get(key) for key in (
                        "connected", "frames", "pools", "reconnects", "last_error", "disabled")},
                    "last_frame_age_s": round(time.monotonic() - seen, 1) if seen is not None else None}
            pooled = prewarm.get("pooled")
            if pooled is not None:
                fanin, watch = pooled.get("fanin") or {}, pooled.get("watch") or {}
                last = fanin.get("last_unique_mono")
                payload["slot_lane"]["prewarm"]["pooled"] = {
                    "sockets": list(pooled.get("sockets") or ()),
                    **{key: fanin.get(key) for key in ("unique_frames", "duplicates", "pools")},
                    "first_by": dict(fanin.get("first_by") or {}),
                    "last_unique_age_s": round(time.monotonic() - last, 1) if last is not None else None,
                    **{key: watch.get(key) for key in ("deaf", "deaf_since", "episodes", "chain_reads",
                                                       "chain_errors")}}
        cpmm = slot_lane_status.get("cpmm_shadow")
        if cpmm is not None:
            # The Raydium CPMM shadow read (2026-09-27): events logged/dropped, fresh reserve reads
            # that failed, and the CPMM pool cache's learning. Whitelisted counters only.
            pools = slot_lane_status.get("cpmm_pools") or {}
            payload["slot_lane"]["cpmm_shadow"] = {
                **{key: cpmm.get(key) for key in ("logged", "dropped", "read_failed", "sim_ok",
                                                  "sim_err", "sim_failed", "sim_skipped")},
                **{"pools_" + key: pools.get(key) for key in ("learned", "learn_failed", "queued")}}
        launchlab = slot_lane_status.get("launchlab_shadow")
        if launchlab is not None:
            # The Raydium LaunchLab shadow read (2026-09-27): events logged/dropped, simulations that
            # would have landed / failed on chain / could not run, and the pool cache's learning.
            pools = slot_lane_status.get("launchlab_pools") or {}
            payload["slot_lane"]["launchlab_shadow"] = {
                **{key: launchlab.get(key) for key in ("logged", "dropped", "sim_ok", "sim_err",
                                                       "sim_failed", "sim_skipped")},
                **{"pools_" + key: pools.get(key) for key in ("learned", "learn_failed", "queued")}}
        damm = slot_lane_status.get("damm_v2_shadow")
        graduation = slot_lane_status.get("graduation")
        if graduation is not None:
            # Graduation direct (2026-10-07): the pool watch and the lane's re-entries. Counters only;
            # per-attempt timings are the slot_graduation trace substage.
            lane_grad = slot_lane_status.get("graduation_lane") or {}
            payload["slot_lane"]["graduation"] = {
                **{key: graduation.get(key) for key in (
                    "offered", "busy", "requeued", "found", "expired", "refused", "reads",
                    "read_errors", "notified", "last")},
                **{"lane_" + key: lane_grad.get(key) for key in (
                    "offered_presend", "offered_revert", "reentered", "aged_out", "sent")}}
        damm_prewarm = slot_lane_status.get("damm_prewarm")
        if damm_prewarm is not None:
            heard = damm_prewarm.get("last_frame_mono")
            payload["slot_lane"]["damm_prewarm"] = {
                **{key: damm_prewarm.get(key) for key in (
                    "connected", "frames", "pools", "reconnects", "last_error")},
                **{key: damm_prewarm[key] for key in ("source", "ready", "queued", "dropped",
                    "delivered", "errors") if key in damm_prewarm},
                "last_frame_age_s": (round(time.monotonic() - heard, 1)
                                     if heard is not None else None),
                **{key: (slot_lane_status.get("damm_v2_pools") or {}).get(key) for key in (
                    "creation_queued", "creation_learned", "creation_read_failed",
                    "creation_dropped", "cache_evicted", "route_rows", "route_scan_ok",
                    "route_scan_failed", "route_queued", "route_loaded", "route_read_failed",
                    "route_dropped", "route_refused", "route_suppressed")}}
            failure = (slot_lane_status.get('damm_v2_pools') or {}).get('route_last_failure')
            payload['slot_lane']['damm_prewarm']['route_last_failure'] = (
                {key: failure.get(key) for key in ('kind', 'reason')} if failure is not None else None)
        if damm is not None:
            # The Meteora DAMM v2 shadow read (B2, 2026-10-01): simulations that would have landed /
            # failed on chain / could not run, and the pool cache's learning, including first
            # sightings dropped unconfirmed. Whitelisted counters only.
            pools = slot_lane_status.get("damm_v2_pools") or {}
            payload["slot_lane"]["damm_v2_shadow"] = {
                **{key: damm.get(key) for key in ("logged", "dropped", "sim_ok", "sim_err",
                                                  "sim_failed", "sim_skipped")},
                **{"pools_" + key: pools.get(key) for key in ("learned", "learn_failed", "queued",
                                                              "not_confirmed", "not_learnable",
                                                              "warm_rows", "warm_loaded",
                                                              "warm_dropped", "warm_unread",
                                                              "rechecked", "repointed",
                                                              "recheck_failed", "store_write_failed",
                                                              "route_rows", "route_scan_ok",
                                                              "route_scan_failed", "route_queued",
                                                              "route_loaded", "route_read_failed",
                                                              "route_dropped", "route_refused",
                                                              "route_suppressed")}}
        ondemand = slot_lane_status.get("ondemand_leg")
        if ondemand is not None:
            # The on-demand quote leg (LaunchLab phase (b), CPMM exotic quotes), on the paper key:
            # offers per venue, busy drops, failed legs, builds, refusals, and the paper key's
            # 429/401s (leg_rate_limited) with the backoff they started. Whitelisted counters only.
            payload["slot_lane"]["ondemand_leg"] = {
                key: ondemand.get(key) for key in ("offered", "offered_raydium_cpmm",
                                                   "offered_raydium_launchlab", "busy", "leg_failed",
                                                   "built", "refused", "leg_rate_limited", "leg_backoff")}
    if fomo_activity_ws_status is not None:
        payload["fomo_activity_ws"] = {
            key: fomo_activity_ws_status.get(key)
            for key in _FOMO_ACTIVITY_WS_STATUS_FIELDS
        }
    if j7_feed_status is not None:
        # Named explicitly; the assembly is a whitelist. See sniper/feeds/j7_feed.py.
        from sniper.feeds.j7_feed import STATUS_FIELDS as _J7_FIELDS
        payload["j7_feed"] = {key: j7_feed_status.get(key) for key in _J7_FIELDS}
    if onchain_volume_feed is not None:
        # Named explicitly; the assembly is a whitelist. See memecoin_volume_writer.feed_status.
        from sniper.services.memecoin_volume_writer import FEED_STATUS_FIELDS as _FEED_FIELDS
        payload["onchain_volume_feed"] = {
            str(chain): {key: (status or {}).get(key) for key in _FEED_FIELDS}
            for chain, status in dict(onchain_volume_feed).items()}
    if rails_observability is not None:
        # NAMED EXPLICITLY. This assembly is a whitelist: a field not listed here never reaches the
        # file. The RPC pool's `degraded` was added to its snapshot on 2026-08-25 and silently
        # dropped one layer up for exactly this reason.
        payload["rails_observability"] = dict(rails_observability)
    if pool_resolver is not None:
        # Named explicitly: this assembly is a whitelist and an unnamed field never reaches the file.
        payload["pool_resolver"] = dict(pool_resolver)
    if jupiter_load is not None:
        # Named explicitly; the assembly is a whitelist.
        payload["jupiter_load"] = dict(jupiter_load)
    if research_rpc_pool is not None:
        # Named explicitly; the assembly is a whitelist. Key ids only, never urls (a Helius url
        # IS the credential). See sniper/services/memecoin_research_rpc.py.
        payload["research_rpc_pool"] = {
            str(lane): [{key: (row or {}).get(key) for key in _RESEARCH_KEY_FIELDS}
                        for row in (rows or [])]
            for lane, rows in dict(research_rpc_pool).items()}
    if research_capture is not None:
        # Named explicitly; the assembly is a whitelist. A research thread that stops shows here as
        # a stale last_pass_at while the funded loop keeps running.
        payload["research_capture"] = {
            key: dict(research_capture).get(key) for key in _RESEARCH_CAPTURE_FIELDS}
    if paper_fanout is not None:
        # The paper copy fan-out's own thread (memecoin_paper_fanout): queue depth, drops, censored
        # late jobs, failures and queue lag. A stuck paper quote shows here, not as a stale loop.
        payload["paper_fanout"] = {key: dict(paper_fanout).get(key) for key in _PAPER_FANOUT_FIELDS}
    if paper_bracket is not None:
        payload["paper_bracket"] = {key: dict(paper_bracket).get(key)
                                    for key in _PAPER_BRACKET_FIELDS}
    if other_holdings is not None:
        # Solana worker health and EVM context readiness; no wallet credentials are exposed.
        payload["other_holdings"] = {key: dict(other_holdings).get(key)
                                     for key in _OTHER_HOLDINGS_FIELDS}
    if evm_gas_topup is not None:
        # The EVM gas top-up's own thread: a stuck pass shows as a stale last_pass_at here.
        payload["evm_gas_topup"] = {key: dict(evm_gas_topup).get(key)
                                    for key in _EVM_GAS_TOPUP_FIELDS}
    if early_quote is not None:
        # The J7 early entry quote (memecoin_early_quote.stats): quotes made vs used, and why the
        # rest were skipped or discarded. wasted_quotes is the added Jupiter load that bought nothing.
        payload["early_quote"] = {key: dict(early_quote).get(key) for key in (
            "skipped", "started", "quoted", "sizing_refused", "used", "discarded",
            "wasted_quotes", "untaken", "refusal_requoted", "refusal_agreed")}
    if raydium_late_shadow is not None:
        # The late Raydium second opinion's shadow writer (solana_real_seams.LATE_RAYDIUM_SHADOW):
        # rows offered, written, dropped on a full queue, and write errors (Gamma LOW, 2026-10-02).
        payload["raydium_late_shadow"] = {key: dict(raydium_late_shadow).get(key) for key in (
            "offered", "written", "dropped", "errors")}
    if coin_age is not None:
        # The coin-age floor's mint evidence (memecoin_coin_age.stats): Helius calls on the watcher
        # key and how they ended, the tape reads, and bounded waits that expired.
        payload["coin_age"] = {key: dict(coin_age).get(key) for key in (
            "warmed", "cache_hits", "cached", "helius_calls", "helius_ok", "helius_429",
            "helius_errors", "wait_expired", "tape_reads", "tape_found")}
    if desk_prequote is not None:
        # Named explicitly; the assembly is a whitelist. The desk prequote book
        # (memecoin_desk_prequote.stats): taken vs missed is how often a held quote served the entry.
        payload["desk_prequote"] = {key: dict(desk_prequote).get(key) for key in (
            "quotes", "quote_errors", "taken", "missed", "pass_errors")}
    if route_series is not None:
        # Named explicitly; the assembly is a whitelist.
        payload["route_series"] = dict(route_series)
    if j7_refusal_diag is not None:
        # J7 receipt refusal diagnostics (the refusal observer status): a prebuilt
        # snapshot from its own thread; whitelisted keys only, diagnostics, no authority.
        from sniper.services.memecoin_j7_refusal_diag import HEARTBEAT_KEYS
        payload["j7_refusal_diag"] = {key: dict(j7_refusal_diag).get(key) for key in HEARTBEAT_KEYS
                                      if key in dict(j7_refusal_diag)}
    if address_coverage is not None:
        # Named explicitly; the assembly is a whitelist. Armed Solana wallets by on-chain address
        # coverage (memecoin_address_registry.solana_coverage): counts per label plus the handles
        # with no verified address. Readiness only; it grants and removes no authority.
        payload["address_coverage"] = {
            key: dict(address_coverage).get(key) for key in ("armed", "labels", "j7_only")}
    if submit_audit is not None:
        # Named explicitly; the assembly is a whitelist. The async submission audit writer
        # (submission_wiring._AuditWriter): a stuck `queued`, or any `dropped`/`failed`, means send
        # receipts are not reaching the ledger. Keyed by file name, never a path.
        payload["submit_audit"] = {
            os.path.basename(str(db)): {key: dict(stats).get(key) for key in _SUBMIT_AUDIT_FIELDS}
            for db, stats in dict(submit_audit).items()}
    if lifi_status is not None:
        payload["lifi"] = {
            key: lifi_status.get(key) for key in _LIFI_STATUS_FIELDS
        }
    try:
        from sniper.util.atomic import atomic_write_json
        atomic_write_json(path, payload)   # atomic -> the web never reads a torn heartbeat
        return True
    except Exception:
        # Diagnose failed writes without letting diagnostics stop the poll loop.
        try:
            from sniper.util.log_sample import log_sampled
            log_sampled(logger.exception, ("heartbeat_write_failed",),
                        "heartbeat write FAILED path=%s unserializable=%s", path,
                        _unserializable_keys(payload))
        except Exception:
            pass
        return False


def _unserializable_keys(payload):
    """Name malformed sections without logging their values."""
    bad = []
    for key, value in payload.items():
        try:
            json.dumps(value)
        except Exception as exc:
            inner = None
            if isinstance(value, dict):
                for sub, item in value.items():
                    try:
                        json.dumps({sub: item})
                    except Exception:
                        inner = "%s:%s" % (sub, type(item).__name__)
                        break
            bad.append("%s(%s%s)" % (key, type(exc).__name__, "" if inner is None else " at " + inner))
    return bad


def read_heartbeat(path=HEARTBEAT_PATH):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def refresh_idle_heartbeat(*, started_at, slot_lane_status, tpu_status,
                           rpc_pool_status, fastlane_status, path=HEARTBEAT_PATH):
    """Main-thread idle publication of live cached statuses; never polls work or providers.

    Keep the last main result, authentication and exit-sweep clocks. Reuse the normal
    whitelist for raw receiver status, including its original head/ACK clocks. Missing
    current status removes old provider evidence instead of renewing a cached proof.
    """
    beat = read_heartbeat(path)
    if (not isinstance(beat, dict) or not started_at or beat.get('started_at') != started_at
            or type(beat.get('auth_ok')) is not bool):
        return False
    for key in ('slot_lane', 'tpu', 'solana_rpc_pool', 'fastlane'):
        beat.pop(key, None)
    try:
        return write_heartbeat(beat, auth_ok=beat['auth_ok'], path=path,
            bracket_ts=beat.get('bracket_ts'), started_at=started_at,
            slot_lane_status=slot_lane_status, tpu_status=tpu_status,
            rpc_pool_status=rpc_pool_status, fastlane_status=fastlane_status,
            _base_payload=beat)
    except Exception:
        return False  # The old timestamp remains old on a malformed snapshot/write failure.


def publish_sweep(bracket_ts, *, started_at, path=HEARTBEAT_PATH, ts=None):
    """Re-publish this process's heartbeat with `bracket_ts` and a fresh `ts`. True when written.

    WHY (2026-09-27). The loop writes its heartbeat BEFORE the exit sweep, so a sweep's stamp
    reached the file one poll later, with the next cycle's heartbeat. The deploy holds the entry
    kill switch until `runner_launch_health` sees bracket_ts >= launch, so each deploy kept entries
    shut ~5 s after the new runner had proved its sweep (aqhpFfoG, refused 0.1 s before a CLEAR).
    The runner calls this once, right after its first sweep. Every other field is the heartbeat
    this cycle already wrote, and only a heartbeat stamped with this process's `started_at` is
    touched: a file the previous runner left must never pass for this one's. Best-effort."""
    beat = read_heartbeat(path)
    if not bracket_ts or not isinstance(beat, dict) or beat.get("started_at") != started_at:
        return False
    beat["ts"] = time.time() if ts is None else ts
    beat["bracket_ts"] = bracket_ts
    try:
        from sniper.util.atomic import atomic_write_json
        atomic_write_json(path, beat)
    except Exception:
        return False
    return True


def acquire_run_lock(lock_path):
    """Exclusive single-instance lock (flock). Returns the open handle (KEEP it — closing or the
    process exiting releases the lock) or None if another instance already holds it. A double
    runner would double-submit a real swap, so the armed loop MUST refuse to start on None."""
    import fcntl
    d = os.path.dirname(lock_path)
    if d:
        os.makedirs(d, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except (OSError, BlockingIOError):
        fh.close()
        return None


def runner_health(heartbeat, now_epoch, max_age_s=None, bracket_max_age_s=None):
    """Interpret the heartbeat for the watchdog: is the runner actually WORKING, or wedged /
    auth-dead / exit-sweep-dead despite the process being up (the pgrep-only watchdog couldn't
    tell)? Returns {healthy, reason, age_s, auth_ok}. reason: 'ok' | 'no_heartbeat' | 'stale' |
    'auth_dead' | 'sweep_stale'. A stale poll beacon means the loop is wedged; a stale bracket_ts
    means SL/TP is not firing even though the loop looks alive (real stop-losses failing silently)."""
    if max_age_s is None:
        from sniper.settings.memecoin import MEMECOIN_HEARTBEAT_MAX_AGE_S
        max_age_s = MEMECOIN_HEARTBEAT_MAX_AGE_S
    if bracket_max_age_s is None:
        from sniper.settings.memecoin import MEMECOIN_SWEEP_MAX_AGE_S
        bracket_max_age_s = MEMECOIN_SWEEP_MAX_AGE_S
    if not heartbeat or heartbeat.get("ts") is None:
        return {"healthy": False, "reason": "no_heartbeat", "age_s": None, "auth_ok": None}
    age_s = now_epoch - float(heartbeat["ts"])
    auth_ok = bool(heartbeat.get("auth_ok"))
    if age_s > max_age_s:
        return {"healthy": False, "reason": "stale", "age_s": age_s, "auth_ok": auth_ok}
    if not auth_ok:
        return {"healthy": False, "reason": "auth_dead", "age_s": age_s, "auth_ok": auth_ok}
    b_ts = heartbeat.get("bracket_ts")
    if b_ts is not None and (now_epoch - float(b_ts)) > bracket_max_age_s:
        return {"healthy": False, "reason": "sweep_stale", "age_s": age_s, "auth_ok": auth_ok}
    return {"healthy": True, "reason": "ok", "age_s": age_s, "auth_ok": auth_ok}


def runner_launch_health(heartbeat, launched_at, now_epoch, max_age_s=None,
                         bracket_max_age_s=None):
    """Require liveness evidence produced after this launch, including one exit sweep.

    ``runner_health`` intentionally accepts legacy heartbeats without ``bracket_ts``. That is safe
    for steady-state backward compatibility but unsafe during funded deployment: a process can
    write one poll heartbeat, die before its first bracket sweep, and otherwise be declared live.
    """
    result = runner_health(
        heartbeat, now_epoch, max_age_s=max_age_s, bracket_max_age_s=bracket_max_age_s,
    )
    if not result["healthy"]:
        return result
    if float(heartbeat["ts"]) < float(launched_at):
        return {**result, "healthy": False, "reason": "launch_heartbeat_missing"}
    bracket_ts = heartbeat.get("bracket_ts")
    if bracket_ts is None or float(bracket_ts) < float(launched_at):
        return {**result, "healthy": False, "reason": "launch_sweep_missing"}
    return result


def refresh_due(*, now, last_refresh, interval_s, ttl_s, margin_s):
    """Should the Privy token be refreshed this cycle?

    Two independent triggers, because the fixed interval alone is not safe:

      interval — the normal proactive cadence, measured from the last refresh.
      ttl      — the token's OWN remaining life. `last_refresh` is seeded at PROCESS START, so a
                 runner inheriting an already-old token (a restart, or a token pasted in from a
                 browser) would otherwise wait the full interval while the token dies under it.

    2026-08-03: the runner started 12:26:58 holding a token minted 12:21:52 and dead at 13:21:52.
    The interval put the first refresh at 13:11:58 — inside the token's life, but with only ten
    minutes of slack, and a single failed attempt was enough to lose it for good (Privy will not
    mint a new app token once the presented one has expired).

    An unreadable ttl falls back to the interval alone rather than crashing the loop.
    """
    if now - last_refresh > interval_s:
        return True
    return ttl_s is not None and ttl_s < margin_s


def classify_fomo_error(status=None, exc=None):
    """Distinguish AUTH DEATH (401/403 — a spent Privy token: a restart won't fix it, the operator
    must replace it) from a TRANSIENT feed error (5xx / timeout / connection — retry, keep the loop
    alive so open positions still get bracket exits). Returns 'auth_dead' | 'feed_error' | 'ok'.
    Only 401/403 is auth_dead; everything else non-OK is transient (fail-safe: don't kill on a blip)."""
    if status in (401, 403):
        return "auth_dead"
    if exc is not None:
        # fomo_get RAISES urllib.error.HTTPError on a spent token (it never returns status 401/403),
        # so the auth-death signal lives on the exception. Inspect .code before defaulting transient.
        from sniper.feeds.fomo_feed import PrivyAppTokenUnavailable
        if isinstance(exc, PrivyAppTokenUnavailable):
            # Privy minted no app token. Unrecoverable without a browser re-login, so page the
            # operator rather than treating it as a blip and spinning (2026-08-03).
            return "auth_dead"
        code = getattr(exc, "code", None)
        if code in (401, 403):
            return "auth_dead"
        return "feed_error"
    if status is not None and status >= 500:
        return "feed_error"
    return "ok"


def handle_auth_page(kind, auth_alerted, *, page_fn=None, now=None, repage_s=None):
    """Page on the transition into auth_dead — from EITHER the success path (feed returned a 401
    status) OR the raise path (a spent Privy token RAISES through run_once and lands in the loop's
    except). Kept OUTSIDE the try branches so it can never be stranded in only one (a spent token
    takes the except path, so an in-try page never fires on the real auth-death path).

    RE-PAGES every MEMECOIN_AUTH_REPAGE_S while it stays dead, carrying the elapsed outage. The
    one-shot version went silent after the crossing: on 2026-09-02 the feed was dead 121 min and
    the only remaining indicator was `cycles=0`, which a quiet market produces byte-identically.

    Only an `ok` cycle clears the state. A `feed_error` says NOTHING about auth health, and
    clearing on it is why 2026-09-02 paged twice (11:21:54 and 11:32:34) under what was documented
    as a one-shot latch: one non-auth_dead classification mid-outage reset it and the next 401
    re-paged. Left in place it would also restart the elapsed clock, so a 121-minute outage could
    report "down 10m" forever.

    State is opaque to the caller: falsy when healthy, a (first_paged_at, last_paged_at) tuple
    while dead. Pass it straight back in."""
    if kind == "ok":
        return False
    if kind != "auth_dead":
        return auth_alerted          # transient feed error: hold the state, page nothing
    import time as _time
    now = _time.time() if now is None else now
    if repage_s is None:
        from sniper.settings.memecoin import MEMECOIN_AUTH_REPAGE_S
        repage_s = MEMECOIN_AUTH_REPAGE_S
    first, last = auth_alerted if isinstance(auth_alerted, tuple) else (now, None)
    if last is not None and (repage_s <= 0 or now - last < repage_s):
        return (first, last)                  # still dead, not yet due to re-page
    if page_fn is None:
        from sniper.services.memecoin_alerts import alert_auth_dead
        page_fn = alert_auth_dead
    page_fn(down_s=now - first)
    return (first, now)


def is_stale_entry(created_at_iso, now_iso, max_age_s):
    """True if a buy is older than max_age_s (both real-UTC) -> drop it: the pump is over and we'd
    buy a dead bag with a burned exit signal (the outage-replay hole). Fail-CLOSED: an unparseable
    timestamp can't be age-checked, so treat as stale (don't buy what you can't verify)."""
    from datetime import datetime
    try:
        a = datetime.fromisoformat(str(created_at_iso).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
        return (b - a).total_seconds() > max_age_s
    except (ValueError, AttributeError, TypeError):
        return True


def sort_events_oldest_first(events):
    """Process a backlog chronologically (after a feed outage) rather than in delivery order, so
    the oldest queued signals are handled first and the staleness gate drops the truly-dead ones."""
    return sorted(events, key=lambda e: str(getattr(e, "created_at", "") or ""))


def _server_epoch(date_hdr):
    if not date_hdr:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_hdr)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def follow_sync_pass(conn, auth):
    """~5min sync of the fomo following graph into the curated list (fail-closed)."""
    from sniper.services.memecoin_follow_sync import sync_followed
    return sync_followed(conn, auth)


def refresh_blind_liquidity(conn, keys, now_iso, *, pool_stats_fn=None):
    """Persist pool liquidity for ONE blind-chain coin per call — absent rows first, then the
    stalest. Feeds sweep_exits' liquidity enrichment (2026-07-22) so paper exits on 4663 model
    the actual pool instead of the unknown-liquidity slippage cap. gt_pool_stats_for is
    TTL-cached, min-interval-guarded, and behind the shared 429 cooldown — one call per sweep
    pass can never burst GT. Returns the refreshed (chain, mint) key or None."""
    if pool_stats_fn is None:
        from sniper.execution.geckoterminal_quote import gt_pool_stats_for as pool_stats_fn
    from sniper.settings.memecoin import MEMECOIN_DEX_BLIND_CHAINS
    blind = [k for k in (keys or [])
             if isinstance(k, tuple) and str(k[0]) in MEMECOIN_DEX_BLIND_CHAINS]
    if not blind:
        return None
    ages = m.token_liquidity_ages(conn, blind)
    target = min(blind, key=lambda k: ages.get(k) or "")
    stats = pool_stats_fn(target[0], target[1]) or {}
    liq = stats.get("liquidity")
    if liq and liq > 0:
        m.set_token_liquidity(conn, str(target[0]), target[1], liq, now_iso)
        conn.commit()
        return target
    return None


def bracket_pass(conn, price_fn=None, now_fn=None, evm_price_for=None, evm_mcap_for=None,
                 gt_batch_fn=None, supplemental_price_map=None, normalize=True):
    """~15s exit sweep: one batched price fetch for all open mints, then run each arm's
    time/price exit policy (control SL/TP, momentum_a rollover, alpha_b rails). Called on a
    separate cadence from the poll loop (rate-safe).

    closed_at is stamped on CORRECTED real-UTC (default) so it shares one clock with opened_at
    (also real-UTC) -> holds are never the -7h illusion the host clock produced.

    `evm_price_for(chain, mint)` is the GeckoTerminal seam for DexScreener-blind chains —
    Robinhood 4663 returns `missing_pair` for 100% of tokens, so without it every 4663 position is
    blind: no marks, no price rail can nominate, and it books `unquotable` at a fictional $0.00. It
    fills ONLY the gaps; a DexScreener row (which carries liquidity + mcap) is never overwritten."""
    from sniper.execution.solana_quote import dexscreener_stats_batch_keyed
    from sniper.services.memecoin_exits import sweep_exits, recover_orphaned_paper_closes
    from sniper.util.clock import real_utc_now_iso
    now_fn = now_fn or real_utc_now_iso
    now_iso = now_fn()
    if normalize:
        # NOT paper-only: it rewrites stamps on every open row, real ones included. The paper
        # bracket worker passes normalize=False and the funded loop runs it on its own thread.
        m.normalize_open_position_hold_stamps(conn, now_iso=now_iso)
    # BEFORE list_open_positions: a stranded claim is is_open=0, so it is invisible to the sweep and
    # to the `not positions` early-return below — a book holding only orphans would never heal.
    # Recovering first also lets a reopened position be priced in THIS pass instead of the next one.
    recover_orphaned_paper_closes(conn, now_iso)
    positions = m.list_open_positions(conn)
    if not positions:
        return []
    # self-heal: a position opened 'evm-unknown' (feed omitted networkId on a 4663 mint) whose chain
    # the feed later carried on another event must be re-attributed BEFORE we build quote keys — else
    # it's keyed on 'evm-unknown', priced by nothing, and only leaves via the 24h guard. Cheap, no-op
    # when there's nothing stuck. Unambiguous-only (resolve_network_from_events refuses conflicts).
    heal_result = m.heal_open_evm_unknown_from_events(conn)
    for collision in heal_result.collisions:
        logger.warning("evm-unknown heal refused: %s", collision)
    if heal_result:
        positions = m.list_open_positions(conn)
    # PAPER STEP 2 (spec 2026-10-08 paper path without DexScreener): only coins an open v0 position
    # holds are priced here. v1 positions decide on their own ticks (memecoin_paper_ticks), which
    # also write their marks and path, and the sweep's one v1 rule, the 14-day clock backstop,
    # closes on the last tick when this map has no mid. No open v0 position: no provider call.
    from sniper.services.memecoin_paper_ticks import V1, exit_engine
    v0 = [p for p in positions if exit_engine(p) != V1]
    keys = list({m.coin_identity(p.get("network_id"), p["mint"]) for p in v0})
    if not keys:
        price_map = {}
    elif price_fn is None:
        price_map = dexscreener_stats_batch_keyed(keys)
    else:
        price_map = price_fn(list({p["mint"] for p in v0}))
    if supplemental_price_map:
        price_map = {**(price_map or {}), **supplemental_price_map}
    # The quote ledger, the mid path and the sweep read v0 coins only: the ledger is retired for v1
    # (one basis per table), and a supplemental own mark for a v1 coin belongs to its tick path.
    wanted = set(keys)
    price_map = {k: v for k, v in (price_map or {}).items()
                 if (k if isinstance(k, tuple) else None) in wanted
                 or (not isinstance(k, tuple) and any(k == key[1] for key in wanted))}
    if evm_price_for is not None:
        from sniper.services.memecoin_evm_price import evm_price_batch
        unpriced = [k for k in keys if not ((price_map.get(k) or {}).get("price") or 0) > 0]
        if unpriced:
            # Batch-GeckoTerminal price EVERY open blind-chain coin this pass (one HTTP per ~30
            # coins). The per-mint rotation below alone (cap 8, TTL/rate-guarded) refreshed only ~8
            # coins/pass, so a large open 4663 book (measured 364 positions / 48 coins) left most
            # bags — including the real ones the UI reads — ~10min stale. The batch also warms the
            # shared GT cache so the mcap pass below is cache hits, not fresh HTTP.
            batch = gt_batch_fn
            if batch is None:
                from sniper.settings import memecoin as _cfg
                if _cfg.MEMECOIN_GT_ENABLED:
                    from sniper.execution.geckoterminal_quote import gt_stats_batch_keyed as batch
            if batch is not None:
                try:
                    price_map = {**price_map, **(batch(unpriced) or {})}
                except Exception:
                    logger.exception("gt batch price failed (per-mint cache path still applies)")
            # Market-cap enrichment (RPC totalSupply, cached per mint) plus per-mint GT, rotating under the
            # cap. Rotation + failure backoff live INSIDE evm_price_batch (keyed on last ATTEMPT);
            # its per-mint GT lookups are served from the batch-warmed cache above.
            try:
                price_map = {**price_map, **evm_price_batch(unpriced, evm_price_for,
                                                            mcap_for=evm_mcap_for)}
            except Exception:
                logger.exception("blind-chain price pass failed (dexscreener prices still applied)")
            # Keep the blind-chain liquidity store warm for the sweep's enrichment pass (paper
            # exits there otherwise book the unknown-liquidity slippage CAP): one pool-stats
            # call per pass, stalest coin first. gt_pool_stats_for is TTL-cached + min-interval
            # + shared-429-guarded, so this can never burst GT.
            try:
                refresh_blind_liquidity(conn, unpriced, now_iso)
            except Exception:
                logger.exception("blind liquidity refresh failed (sweep unaffected)")
    try:   # dense quote ledger (v1 exit-search prereq) — best-effort, never blocks exits
        m.record_quote_observations(conn, price_map, now_iso)
    except Exception:
        logger.exception("quote ledger write failed (sweep unaffected)")
    try:   # per-position paper price path from this same map (no new fetch); memecoin_paper_path
        from sniper.services.memecoin_paper_path import record_paper_path
        record_paper_path(conn, positions, price_map, now_iso)
    except Exception:
        logger.exception("paper path write failed (sweep unaffected)")
    # Ticks DB for 4663 tape pricing (momentum_a only). Best-effort: if it cannot be opened the
    # sweep runs exactly as before on DexScreener quotes. Opened per sweep and closed after —
    # a SQLite open is cheap and this keeps no long-lived handle on the tape.
    tc = None
    try:
        from sniper.persistence import db_ticks as _db_ticks
        tc = _db_ticks.connect_ticks()
    except Exception:
        logger.exception("ticks DB unavailable (exits fall back to DexScreener pricing)")
    try:
        return sweep_exits(conn, price_map, now_iso, tc=tc)
    finally:
        if tc is not None:
            try:
                tc.close()
            except Exception:
                pass



# --- the paper bracket off the funded loop (2026-09-27, Alpha GO) ---------------------------------
#
# bracket_pass is paper-only apart from the hold-stamp normalize, and its DexScreener / GT / 4663
# price reads and paper exit quotes took ~2.2 s of every 15 s bracket on the loop that runs funded
# copies (maint.bracket.paper = 14.7% of loop wall, 13:59-14:05Z). A copy of the C1 start_pass: one
# worker thread per pass with its own connection, single-flight. MEMECOIN_PAPER_BRACKET_THREAD=0
# runs it inline, as before.

# Written by the loop (thread, pass_started_at) and by the worker (the rest): disjoint keys.
_PAPER_BRACKET = {"thread": None, "pass_started_at": None, "last_pass_s": None, "failures": 0}


def paper_bracket_thread_enabled(env=None):
    env = os.environ if env is None else env
    return str(env.get("MEMECOIN_PAPER_BRACKET_THREAD", "1")).strip().lower() not in {
        "0", "false", "no", "off"}


def start_paper_bracket(*, now_fn, connect=None, **bracket_kwargs):
    """Run one paper bracket_pass on a worker thread with its own connection, normalize=False.
    Returns False while the previous pass still runs (that bracket is skipped, not queued)."""
    import threading
    running = _PAPER_BRACKET["thread"]
    if running is not None and running.is_alive():
        return False

    def run():
        started, conn = time.monotonic(), None
        try:
            from sniper.persistence.db_core import get_connection
            conn = (connect or get_connection)()
            bracket_pass(conn, now_fn=now_fn, normalize=False, **bracket_kwargs)
        except Exception:
            _PAPER_BRACKET["failures"] += 1
            logger.exception("paper bracket pass failed")
        finally:
            _PAPER_BRACKET["last_pass_s"] = round(time.monotonic() - started, 2)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    _PAPER_BRACKET["pass_started_at"] = now_fn()
    thread = threading.Thread(target=run, name="memecoin-paper-bracket", daemon=True)
    _PAPER_BRACKET["thread"] = thread
    thread.start()
    return True


def paper_bracket_status():
    """For the heartbeat. A hung pass reads alive=True with an old pass_started_at."""
    t = _PAPER_BRACKET["thread"]
    return {"alive": bool(t is not None and t.is_alive()),
            "pass_started_at": _PAPER_BRACKET["pass_started_at"],
            "last_pass_s": _PAPER_BRACKET["last_pass_s"], "failures": _PAPER_BRACKET["failures"]}

_seen_networks = None   # seeded from the events table on first poll; process-lifetime cache


def _flag_new_networks(conn, events):
    """Page ONCE when fomo emits its first event on a network id we've never stored — the moment
    a new venue (e.g. Robinhood Chain 4663) goes live on the feed is a venue-support decision,
    and silently dropping it for weeks is how we missed BSC volume. Best-effort: never breaks
    the copy path."""
    global _seen_networks
    try:
        if _seen_networks is None:
            _seen_networks = {r[0] for r in conn.execute(
                "SELECT DISTINCT COALESCE(network_id,'') FROM memecoin_events")}
        for ev in events:
            n = ev.network_id or ""
            if n and n not in _seen_networks:
                _seen_networks.add(n)
                logger.warning("memecoin: FIRST feed event on new network=%s (%s %s) — "
                               "evaluate venue support", n, ev.ticker, ev.mint)
                from sniper.services.memecoin_alerts import alert_new_network
                alert_new_network(n, ev.ticker, ev.mint)
    except Exception:
        logger.exception("new-network tripwire failed (non-fatal)")


def publish_received_bsc(observations, publisher):
    """Compatibility seam for the existing BSC-only caller."""
    return publish_received_evm(observations, {"56": publisher})


def publish_received_evm(observations, publishers):
    """Bounded asynchronous measurement before roster filtering; eligibility stays unknown."""
    from sniper.util.receipt_clock import freeze_receipt_clock
    failed = {}
    for observation in observations:
        chain = str(observation.network_id)
        publisher = publishers.get(chain)
        if publisher is None or observation.normalized_side == "sell":
            continue
        try:
            publisher.record_signal({"signal_id": observation.upstream_event_id,
                "source_user_id": observation.source_user_id, "mint": observation.mint,
                "source_feed": "j7" if str(observation.upstream_event_id).startswith("j7:") else "feed",
                "receipt_clock": freeze_receipt_clock(observation.raw_payload.get("receipt_clock")),
                "eligibility": None, "reason": "received_before_policy"})
        except Exception:
            failed[chain] = failed.get(chain, 0) + 1
    for chain, count in failed.items():
        try:
            publishers[chain].record_gap({"reason": "received_publication_failed", "count": count})
        except Exception:
            pass  # Returned losses stay visible when the publisher itself is unavailable.
    return sum(failed.values())


def run_once(conn, auth, size_usd, http=None, now_fn=None, fill_fn=honest_fill, stats_fn=None,
             min_discovery_usd=None, alert_fn=None, alert_queue=None, real_now_fn=None,
             entry_max_age_s=None, real_hook=None, candidate_hook=None, receipt_observer=None, receipt_observers=None):
    # ONE clock (2026-07-11): everything stamps CORRECTED real-UTC. The old "two clocks by
    # design" (host received_at + host pump ages) kept internally-consistent ages but left the
    # STORED data split across domains — received_at sat ~+7h from created_at (fomo real-UTC),
    # silently corrupting every cross-column time window (the strategy engine's cluster windows,
    # ad-hoc research, and even this session's own diagnostics). Historic host-domain rows were
    # shifted by scripts/research/normalize_clock_domains.py; pump buy_ts/ages moved with it.
    from sniper.util.clock import real_utc_now_iso
    from sniper.settings.memecoin import MEMECOIN_FUNDED_ONLY
    now_fn = now_fn or real_utc_now_iso
    real_now_fn = real_now_fn or real_utc_now_iso
    import time as _t_prof
    _t_fetch0 = _t_prof.monotonic()
    # SKIP, DO NOT SLEEP, WHEN THE GUARD IS BACKING OFF (2026-09-06). The exit sweep shares this
    # loop, so standing inside the backoff holds every stop-loss on every open position -- on
    # 2026-09-02 a 143 s blocked cycle let a confirmed stop at -41.5% sell at -81.3%. The guard
    # refuses without ratcheting `_last_call`, so the backoff is honoured to the second and no other
    # caller loses its turn; the poll is simply not made this cycle. See
    # MEMECOIN_FOMO_POLL_MAX_WAIT_S for why shortening the backoff instead would be the ban.
    from sniper.settings.memecoin import MEMECOIN_FOMO_POLL_MAX_WAIT_S
    from sniper.feeds.fomo_feed import FomoThrottleBacklog
    # SIGNAL SOURCE SEAM (2026-09-09). With MEMECOIN_SIGNAL_SOURCE=j7 the items come from the
    # J7Tracker wallet-tracker socket already shaped like fomo /feed items, fomo is never called,
    # and everything below this block is unchanged. J7's `received_at` is its server clock in real
    # UTC, so it feeds the same host-offset learning the fomo Date header did.
    from sniper.feeds.j7_feed import j7_feed_or_none
    _j7 = j7_feed_or_none(log_fn=logger.info)
    if _j7 is not None:
        _j7_items, _j7_epoch = _j7.drain()
        status, j, date_hdr = 200, {"feed": _j7_items}, None
        # The epoch is the LAST relay's server stamp. It prices this batch's lag_s (the batch is
        # at most one cycle old), but it must never re-teach the clock at drain time: an empty
        # drain would hand a stale stamp to record_real_utc and age the corrected clock with the
        # lull (21 s behind at 20:54Z on 2026-09-09). The feed learns the clock at receipt.
        server_epoch = _j7_epoch if _j7_items else None
        _clock_learned_upstream = True
    else:
        try:
            status, j, date_hdr = fomo_get(auth, build_feed_path(), http=http,
                                         max_wait_s=MEMECOIN_FOMO_POLL_MAX_WAIT_S)
        except FomoThrottleBacklog:
            elapsed = _t_prof.monotonic() - _t_fetch0
            return {"skipped_feed_reason": "throttle_backlog", "events": 0, "opened": 0,
                    "exits": 0, "fetch_s": elapsed, "total_s": elapsed}
        server_epoch = _server_epoch(date_hdr)
        _clock_learned_upstream = False
    # SPLIT THE FETCH FROM THE FAN-OUT (2026-09-04). The runner's copy phase measured 0.16s to
    # 3.74s per cycle and dominates the 5s loop, but "fomo call" and "what we do with the events"
    # are different problems with different fixes -- and the fetch itself includes the shared rate
    # guard's inter-call sleep, which is ours, not fomo's.
    _fetch_s = _t_prof.monotonic() - _t_fetch0
    if server_epoch and not _clock_learned_upstream:   # fomo Date header is real UTC -> learn the host-clock offset (host runs +7h)
        from sniper.util.clock import record_real_utc
        record_real_utc(server_epoch)
        m.normalize_open_position_hold_stamps(conn)
    recv_iso = now_fn()
    # Per-item receipt_clock survives the drain; recv_iso retains existing admission/age authority.
    ro = j.get("responseObject") or j
    raw_items = ro.get("feed") or []
    captured_curated = m.curated_user_ids(conn)
    captured_armed = m.real_armed_any_chain_user_ids(conn)   # telemetry stamp, not authority
    real_holders = m.open_position_user_ids(conn, mode="real")
    observations = []
    for raw in raw_items:
        try:
            observations.append(normalize_feed_observation(raw, recv_iso, server_epoch))
        except Exception:
            logger.exception("v2 feed observation normalization failed (item skipped)")
    receipt_publication_failed = publish_received_evm(
        observations, receipt_observers if receipt_observers is not None else {"56": receipt_observer})
    try:
        retained = observations
        if MEMECOIN_FUNDED_ONLY:
            from sniper.settings.memecoin import MEMECOIN_REAL_ROSTER_GATE_ENABLED
            if MEMECOIN_REAL_ROSTER_GATE_ENABLED:
                retained = [o for o in observations if o.event is not None
                            and o.event.source_user_id in captured_armed | real_holders]
        m.insert_feed_observations_v2(conn, retained, captured_curated, captured_armed)
    except Exception:
        logger.exception("v2 feed observation retention failed (copy path unaffected)")
    events = [observation.event for observation in observations if observation.event is not None]
    _flag_new_networks(conn, events)   # tripwire: fomo starts carrying a new chain -> page once
    write_feed_snapshot(events)   # full herd -> UI Activity mirror
    # firehose retention (2026-07-11): keep EVERY feed event, both sides, no size floor —
    # coin episodes, unbiased lead-lag, and own-entry (cluster) research all need the full tape.
    from sniper.settings.memecoin import MEMECOIN_FIREHOSE_ENABLED
    if MEMECOIN_FIREHOSE_ENABLED and not MEMECOIN_FUNDED_ONLY:
        try:
            m.insert_firehose_events(conn, [e.__dict__ for e in events],
                                     curated_ids=captured_curated,
                                     armed_ids=captured_armed)
        except Exception:
            logger.exception("firehose retention failed (copy path unaffected)")
    curated = captured_armed if MEMECOIN_FUNDED_ONLY else captured_curated
    # sell-mirror eligibility spans BOTH ledgers: a wallet unfollowed after we opened a real
    # position must still mirror-exit it when it sells. While disarmed the real set is empty, so
    # open_holders (and paper behavior) is byte-identical.
    open_holders = real_holders if MEMECOIN_FUNDED_ONLY else m.open_position_user_ids(conn) | real_holders
    # curated theses (display/context only) from the SAME raw feed — best-effort, off the copy path
    if not MEMECOIN_FUNDED_ONLY:
        _capture_theses(conn, raw_items, recv_iso, server_epoch, curated)
    if candidate_hook is not None and not MEMECOIN_FUNDED_ONLY:
        try:
            candidate_hook(conn, recv_iso)
        except Exception:
            logger.exception("paper candidate hook failed (copy path unaffected)")
    # discovery funnel: capture non-curated large buys as copy candidates. Dedup via the
    # same insert_event OR-IGNORE, so a re-delivered event is counted once. Tagged
    # action='candidate' so the Recent-activity feed can filter the firehose out.
    from sniper.settings.memecoin import (
        MEMECOIN_DISCOVERY_ENABLED, MEMECOIN_DISCOVERY_MIN_USD)
    min_disc = MEMECOIN_DISCOVERY_MIN_USD if min_discovery_usd is None else min_discovery_usd
    discovery_alerts = []
    if MEMECOIN_DISCOVERY_ENABLED and not MEMECOIN_FUNDED_ONLY:
        for ev in events:
            if (ev.side == "buy" and ev.source_user_id and ev.source_user_id not in curated
                    and (ev.their_size_usd or 0) >= min_disc):
                if m.insert_event(conn, ev.__dict__):     # new sighting only
                    m.upsert_candidate(conn, ev)
                    m.set_event_action(conn, ev.fomo_event_id, "candidate")
                    _capture_pump_outcome(conn, ev, now_fn(), min_disc)
                    if _want_source_intent(conn, ev, curated, alert_queue):
                        discovery_alerts.append(ev)
    # buys: curated only (a paused/unfollowed wallet opens nothing new). sells: curated
    # OR still holding an open position — so an unfollowed wallet's positions still
    # mirror-exit when the trader sells, not just via SL/TP brackets.
    mine = [e for e in events if e.source_user_id in curated
            or (e.side == "sell" and e.source_user_id in open_holders)]
    # feed-staleness gate + oldest-first (item 6) — OPT-IN via entry_max_age_s so the paper A/B is
    # byte-unchanged (Gate A evidence); the real leg passes a tight age. Process any backlog
    # chronologically, then drop stale BUYS (sells still process so open positions can still exit).
    stale_now = None
    if entry_max_age_s is not None:
        mine = sort_events_oldest_first(mine)
        stale_now = real_now_fn()
    # front-run Telegram alert gate (external send -> default off; only fresh global-feed buys).
    from sniper.settings.memecoin import (
        MEMECOIN_TG_ALERTS, MEMECOIN_TG_MIN_USD, MEMECOIN_TG_MAX_PER_POLL)
    if alert_fn is None:
        from sniper.services.memecoin_telegram import send_telegram
        alert_fn = send_telegram
    alerts_this_poll = dropped_alerts = 0
    for ev in discovery_alerts:
        if alerts_this_poll >= MEMECOIN_TG_MAX_PER_POLL:
            dropped_alerts += 1
            continue
        accepted, dropped = _dispatch_front_run_alert(conn, ev, alert_fn, alert_queue)
        alerts_this_poll += accepted
        dropped_alerts += dropped
    opened = exits = 0
    processed = 0
    real_counts = {}   # real-hook outcome tally (filled/not_approved/stale/rejected/…) — operator
                       # visibility: "are we passing events to the real leg?" answerable from the log
    for ev in mine:
        # staleness gate (real leg only): drop a BUY whose pump is already over (outage replay).
        if stale_now is not None and ev.side == "buy" \
                and is_stale_entry(ev.created_at, stale_now, entry_max_age_s):
            if m.insert_event(conn, ev.__dict__):
                m.set_event_action(conn, ev.fomo_event_id, "stale")
            continue
        # The fomo /feed re-delivers the same events every poll. Only copy an event the
        # FIRST time we see it (insert_event returns True on a new fomo_event_id); otherwise
        # a re-delivered buy/sell pair churns open/close hundreds of times (N=187->6 bug).
        inserted = m.insert_event(conn, ev.__dict__)
        paper_retry = False
        if not inserted:
            row = conn.execute(
                "SELECT action FROM memecoin_events WHERE fomo_event_id=?",
                (ev.fomo_event_id,),
            ).fetchone()
            paper_retry = bool(row and row["action"] == "paper_failed")
            if not paper_retry:
                continue
        # Alert FIRST (send latency is the thing under test), best-effort. Curated buys always;
        # non-curated buys only on CONSENSUS (2nd+ distinct buyer, sub-$2M — see
        # want_front_run_alert). Never sells / the late follow path (those alert elsewhere).
        if not paper_retry:
            want_alert = _want_source_intent(conn, ev, curated, alert_queue)
            if want_alert and alerts_this_poll < MEMECOIN_TG_MAX_PER_POLL:
                accepted, dropped = _dispatch_front_run_alert(
                    conn, ev, alert_fn, alert_queue)
                alerts_this_poll += accepted
                dropped_alerts += dropped
            elif want_alert:
                dropped_alerts += 1
            if not MEMECOIN_FUNDED_ONLY:
                _capture_pump_outcome(conn, ev, now_fn(), min_disc)
        # The funded hook is the money path. Optional paper work must not delay or delete it.
        real_attempted = real_hook is not None and not paper_retry
        if real_attempted:
            try:
                rres = real_hook(conn, ev)
                if rres and rres.get("status"):
                    st = rres["status"]
                    real_counts[st] = real_counts.get(st, 0) + 1
                    if st in ("disarmed", "rejected", "aborted", "route_failed", "unsafe"):
                        logger.warning("real entry REFUSED status=%s reason=%s wallet=%s coin=%s%s",
                                       st, rres.get("reason"),
                                       getattr(ev, "source_handle", "?"),
                                       getattr(ev, "ticker", None) or ev.mint[:12],
                                       " slot_lane=%s" % rres["slot_lane_skip"]
                                       if rres.get("slot_lane_skip") else "")
            except Exception:
                logger.exception("memecoin real hook failed for %s", ev.fomo_event_id)
        if MEMECOIN_FUNDED_ONLY:
            processed += 1
            continue
        from sniper.settings.memecoin import MEMECOIN_ARMS
        from sniper.services.memecoin_copy_trader import copy_event_to_arms
        ev_size = (arm_base_size(MEMECOIN_ARMS, ev.their_size_usd, size_usd)
                   if ev.side == "buy" else size_usd)
        # OFF THIS THREAD when a fan-out is installed (memecoin_paper_fanout, 2026-09-27): a paper
        # quote that hangs must not hold the funded loop, nor the next event's funded hook.
        from sniper.services.memecoin_paper_fanout import submit_or_none as _paper_submit
        if _paper_submit(conn, ev, ev_size, arms=MEMECOIN_ARMS, fill_fn=fill_fn, stats_fn=stats_fn,
                         delete_on_failure=not (real_attempted or paper_retry),
                         action_rule="feed") is not None:
            processed += 1
            continue
        try:
            # opened_at (and mirror closed_at) on real-UTC so hold matches the real-UTC exit sweep
            outcomes = copy_event_to_arms(conn, ev, ev_size, real_now_fn(), arms=MEMECOIN_ARMS,
                                          fill_fn=fill_fn, stats_fn=stats_fn,
                                          funded_quote=m.funded_quote_for_origin(
                                              conn, getattr(ev, 'fomo_event_id', None)),
                                          funded_clip=m.funded_clip_for_origin(
                                              conn, getattr(ev, 'fomo_event_id', None)))
        except Exception as exc:
            if real_attempted or paper_retry:
                # Keep funded idempotency. A later delivery retries only the paper fan-out.
                m.set_event_action(conn, ev.fomo_event_id, "paper_failed")
                # sqlite_errorcode separates SQLITE_BUSY=5 (a real lock holder outlasted the 60s
                # busy timeout) from SQLITE_BUSY_SNAPSHOT=517 (deferred txn read a stale snapshot;
                # busy_timeout NEVER retries that) — the 2026-08-17 lock hunt needs the split.
                logger.exception("paper fan-out failed after funded hook for %s (sqlite_errorcode=%s)",
                                 ev.fomo_event_id, getattr(exc, "sqlite_errorcode", None))
            else:
                # Paper-only mode keeps its prior at-least-once behavior.
                m.delete_event(conn, ev.fomo_event_id)
            continue
        processed += 1
        vals = list(outcomes.values())
        r = "opened" if "opened" in vals else "exit" if "exit" in vals else (vals[0] if vals else "ignored")
        m.set_event_action(conn, ev.fomo_event_id, r)   # priority (any-arm) outcome for the event
        opened += ("opened" in vals)                    # unique-event counters (not summed per arm)
        exits += ("exit" in vals)
    # Holder evidence is local observation work. Run it only after every current event reached the
    # funded hook, so it adds no source-observation -> submission latency.
    try:
        from sniper.services.memecoin_holder_evidence import reconcile_feed_observations
        reconcile_feed_observations(conn, retained, captured_armed)
    except Exception:
        logger.exception("holder evidence reconciliation failed (copy path unaffected)")
    # Candidate capture cohorts (2026-07-16): open forward-quote coverage for every concrete-chain
    # buy the feed shows us, but only AFTER every current event has reached the funded hook. These
    # local research upserts must never add source-observation -> submission latency.
    if not MEMECOIN_FUNDED_ONLY:
        try:
            from sniper.services.memecoin_capture_v2 import register_candidate_cohorts
            register_candidate_cohorts(conn, [e.__dict__ for e in events], recv_iso)
        except Exception:
            logger.exception("candidate cohort registration failed (copy path unaffected)")
    if dropped_alerts:
        # burst exceeded the per-poll cap; the accrued latency sample is truncated (in feed
        # order) — surface it so the truncation and its likely bias direction are visible.
        logger.warning("memecoin front-run alert cap hit: dropped %d curated-buy alert(s) this "
                       "poll (cap=%d, feed-order truncation)", dropped_alerts, MEMECOIN_TG_MAX_PER_POLL)

    # ROSTER-GATE-OFF FUNDED WIDENING (2026-08-14). `mine` is curated-only by construction, so
    # turning the roster gate off never actually widened WHO the funded leg evaluates — COCO $7.5K,
    # AI $30K, XST and PONS all sat in the feed with real_decision NULL while the operator watched
    # them moon. When the gate is off, every fresh NON-curated feed buy now gets exactly one funded
    # evaluation: the hook stamps real_decision on the event, so re-delivered polls skip it, and
    # every existing money gate (mcap ceiling, chase, impact, bankroll, concurrency, exposure)
    # decides as usual. The paper engine remains byte-identical — it still sees only `mine`.
    if real_hook is not None:
        from sniper.settings import memecoin as _rcfg
        if not _rcfg.MEMECOIN_REAL_ROSTER_GATE_ENABLED:
            extras = [e for e in events
                      if e.side == "buy" and e.source_user_id not in curated]
            for ev in sort_events_oldest_first(extras):
                if stale_now is not None and is_stale_entry(
                        ev.created_at, stale_now, entry_max_age_s):
                    continue
                m.insert_event(conn, ev.__dict__)
                row = conn.execute(
                    "SELECT real_decision FROM memecoin_events WHERE fomo_event_id=?",
                    (ev.fomo_event_id,)).fetchone()
                if row and row[0] is not None:
                    continue
                try:
                    rres = real_hook(conn, ev)
                    st = (rres or {}).get("status")
                    if st:
                        real_counts[st] = real_counts.get(st, 0) + 1
                        if st in ("disarmed", "rejected", "aborted", "route_failed", "unsafe"):
                            logger.warning(
                                "real entry REFUSED (feed-wide) status=%s reason=%s wallet=%s coin=%s",
                                st, rres.get("reason"), getattr(ev, "source_handle", "?"),
                                getattr(ev, "ticker", None) or ev.mint[:12])
                except Exception:
                    logger.exception("funded feed-wide hook failed for %s", ev.fomo_event_id)

    return {"fetch_s": _fetch_s, "total_s": _t_prof.monotonic() - _t_fetch0,
            "events": processed, "opened": opened, "exits": exits,
            "alerts": alerts_this_poll, "alerts_dropped": dropped_alerts,
            "receipt_publication_failed": receipt_publication_failed,
            "real": real_counts,
            "feed_status": status}   # for auth_dead vs transient-feed disambiguation (item 9)
