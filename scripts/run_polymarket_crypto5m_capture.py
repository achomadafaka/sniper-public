#!/usr/bin/env python3
"""Phase-0 capture CLI for the Polymarket crypto-5m binary (BTC Up/Down).

CAPTURE-ONLY. Read-only public endpoints (Gamma REST, CLOB REST/WS, Chainlink RTDS WS, Binance REST
for the proxy lane), local SQLite only. NO order submission, NO private keys, NO authenticated
channels, NO production DB, NO service changes — there are deliberately no flags for any of those.

Examples:
  python3 scripts/run_polymarket_crypto5m_capture.py --discover-only
  python3 scripts/run_polymarket_crypto5m_capture.py --capture-duration 60 --db-path data/crypto5m_capture.sqlite
  python3 scripts/run_polymarket_crypto5m_capture.py --report --db-path data/crypto5m_capture.sqlite
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import types
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.algos.polymarket_crypto5m import book_integrity as BOOKINT  # noqa: E402
from sniper.algos.polymarket_crypto5m import capture as C          # noqa: E402
from sniper.algos.polymarket_crypto5m import chainlink_rtds as RTDS  # noqa: E402
from sniper.algos.polymarket_crypto5m import market_resolver as MR   # noqa: E402
from sniper.algos.polymarket_crypto5m import persistence as P        # noqa: E402
from sniper.algos.polymarket_crypto5m import reconciliation as RECON  # noqa: E402
from sniper.algos.polymarket_crypto5m import report as REP           # noqa: E402
from sniper.algos.polymarket_crypto5m import settlement as S         # noqa: E402
from sniper.algos.polymarket_crypto5m.lifecycle import WindowLifecycle  # noqa: E402

log = logging.getLogger("crypto5m_capture")
CODE_VERSION = "phase0-capture-1"
DEFAULT_DB = "data/crypto5m_capture.sqlite"
UA = {"User-Agent": "sniper-research/0.1"}


def _get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def server_now() -> int:
    """Binance server time (seconds). Authoritative clock — never the local +7h-skewed clock."""
    return int(_get_json("https://api.binance.com/api/v3/time")["serverTime"]) // 1000


def binance_btc_price() -> float:
    return float(_get_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")["price"])


def discover_current(now_ts: int, *, max_markets: int, fixture_mode: bool):
    """Return capture-ready BTC 5m market specs for the current/active window(s)."""
    if fixture_mode:
        fx = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "tests", "fixtures", "crypto5m")
        raws = [json.load(open(os.path.join(fx, f))) for f in
                ("event_resolved_up.json", "event_live.json")]
    else:
        cur = now_ts - (now_ts % 300)
        raws = []
        for ws in (cur, cur + 300):  # current + next window
            try:
                ev = _get_json(f"https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{ws}")
            except Exception as e:  # noqa: BLE001
                log.warning("Gamma discovery fetch failed for btc-updown-5m-%s: %s", ws, e)
                continue
            if ev:
                raws.append(ev[0])
    out = []
    for raw in raws:
        spec, verdict = MR.parse_event(raw)
        ready, reason = MR.capture_ready(raw, now_ts=now_ts)
        log.info("discovered slug=%s rule_supported=%s capture_ready=%s/%s p2b=%s",
                 raw.get("slug"), verdict.rule_supported, ready, reason,
                 (spec.price_to_beat if spec else None))
        if spec is not None and (ready or fixture_mode):
            out.append(spec)
        if len(out) >= max_markets:
            break
    return out


def _rtds_thread(stop: threading.Event, conn, lock, window_start_ts: int):
    """Capture Chainlink RTDS ticks until stopped. RTDS only backfills ON SUBSCRIBE (it does NOT stream
    in-window — verified 2026-06-21), so re-subscribe periodically to pull fresh in-window ticks; a single
    subscribe would leave chainlink_now stale by the 120-270s checkpoints. Persists each tick (idempotent;
    overlapping batches dedup by dedup_key)."""
    import websocket

    def _connect():
        ws = websocket.create_connection(RTDS.RTDS_URL, timeout=10)
        ws.settimeout(2)                       # bounded recv so the cadence/stop loop can tick
        return ws

    def _persist(t):
        with lock:
            P.append_chainlink_tick(conn, t, window_start_ts=window_start_ts)

    RTDS.collect_resubscribing(_connect, stop_fn=stop.is_set, on_tick=_persist,
                               now_fn=server_now_cached, clock_fn=time.monotonic)


_NOW_CACHE = {"ts": 0, "mono": 0.0}


def server_now_cached() -> int:
    """Cheap monotonic-extrapolated server time so threads don't hammer the time endpoint."""
    if _NOW_CACHE["ts"] == 0:
        _NOW_CACHE["ts"] = server_now()
        _NOW_CACHE["mono"] = time.monotonic()
    return _NOW_CACHE["ts"] + int(time.monotonic() - _NOW_CACHE["mono"])


def _rest_snapshot(conn, lock, coord, spec, token, *, source_event, flags):
    """Fetch a CLOB REST book for ``token`` and ingest+persist it (init / gap recovery baseline)."""
    try:
        book = _get_json(f"{C.CLOB_BOOK_URL}?token_id={token}")  # network OUTSIDE the lock
    except Exception as e:  # noqa: BLE001
        log.warning("REST book fetch failed for %s: %s", token[:12], e)
        return False
    # the coordinator mutation + the read-and-persist must happen as ONE critical section so a
    # concurrent WS update can't interleave between ingest and snapshot.
    with lock:
        # Reconcile the reconstructed book against the REST truth BEFORE overwriting it, so divergence
        # is MEASURED + quarantined (not silently overwritten). Skip the init baseline (no prior state).
        if source_event != "rest_init":
            prior = coord.book_for(token)
            if prior is not None:
                cmp = BOOKINT.compare_books(prior, {"bids": book.get("bids"), "asks": book.get("asks")})
                if not cmp["indeterminate"]:
                    P.record_book_reconciliation(
                        conn, condition_id=spec.condition_id, token_id=token,
                        book_state_match=bool(cmp["match"]),
                        book_divergence_levels=cmp["divergence_levels"],
                        book_divergence_duration_ms=0,
                        rest_reconciliation_ts=server_now_cached(),
                        # quarantine only on a top-of-book reconstruction failure (best bid/ask mismatch),
                        # NOT benign interior movement between the WS state and a later REST refetch.
                        quarantined_interval=BOOKINT.should_quarantine(cmp),
                        code_version=CODE_VERSION, now_ts=server_now_cached())
        coord.ingest_clob({"event_type": "book", "asset_id": token, "timestamp": book.get("timestamp"),
                           "bids": book.get("bids"), "asks": book.get("asks"), "hash": book.get("hash"),
                           "tick_size": book.get("tick_size"),
                           "min_order_size": book.get("min_order_size")}, recv_ts=server_now_cached())
        coord.persist_snapshot(conn, token, code_version=CODE_VERSION, source_event=source_event,
                               recv_ts=server_now_cached(), now_ts=server_now_cached(), flags=flags)
    return True


def _monitor_thread(stop: threading.Event, conn, lock, coord, spec, *, stale_book_ms,
                    rest_reconcile_s):
    """Detect stale/gapped books and force REST recovery; periodic REST reconcile. Records gaps so a
    'clean' report can never hide a silently-missing book."""
    from sniper.algos.polymarket_crypto5m.models import CaptureGap
    max_gap_s = stale_book_ms / 1000.0
    was_stale = {spec.up_token_id: False, spec.down_token_id: False}
    last_reconcile = time.monotonic()
    while not stop.is_set():
        time.sleep(1)
        now = server_now_cached()
        for tok in (spec.up_token_id, spec.down_token_id):
            with lock:   # is_stale reads coordinator state; mark_reconnect mutates it
                stale = coord.is_stale(tok, now=now, max_gap=max_gap_s)
                first_stale = stale and not was_stale[tok]
                if first_stale:
                    gen = coord.mark_reconnect()
                    P.record_gap(conn, CaptureGap(spec.condition_id, spec.window_start_ts,
                                 "book_stale", now, "rest_resnapshot", gen), now_ts=now)
            if first_stale:
                _rest_snapshot(conn, lock, coord, spec, tok, source_event="gap_recovery",
                               flags=["book_stale", "rest_recovery"])  # locks internally
            was_stale[tok] = stale
        if time.monotonic() - last_reconcile >= rest_reconcile_s:
            for tok in (spec.up_token_id, spec.down_token_id):
                _rest_snapshot(conn, lock, coord, spec, tok, source_event="rest_reconcile",
                               flags=["rest_recovery"])
            last_reconcile = time.monotonic()


def _persist_book_events(conn, coord, spec, res, *, now_fn=None):
    """Persist one crypto5m_book_events row per coordinator event — one per changed level for a
    multi-asset ``price_change`` (the add/cancel delta stream), one for any other message. Uses the
    per-level ``payload`` so a replay can reconstruct queue depth. commit=False: high-rate events are
    batched and flushed by the periodic snapshot commit (and on exit)."""
    now_fn = now_fn or server_now_cached
    for ev in res.get("events", ()):
        if not ev["token_id"]:
            continue
        P.append_book_event(conn, condition_id=spec.condition_id, token_id=ev["token_id"],
                            side=ev["side"], event_type=ev["event_type"],
                            exchange_ts=ev["payload"].get("timestamp"), recv_ts=now_fn(),
                            reconnect_generation=coord.reconnect_generation,
                            quality_flags=list(ev["flags"]), raw_payload=ev["payload"],
                            now_ts=now_fn(), commit=False)


def _clob_thread(stop: threading.Event, conn, lock, coord: "C.CaptureCoordinator", spec):
    """Capture CLOB market-channel updates for both tokens; persist events + periodic snapshots."""
    import websocket
    # REST snapshot init for both books first (recovery baseline)
    for tok in (spec.up_token_id, spec.down_token_id):
        _rest_snapshot(conn, lock, coord, spec, tok, source_event="rest_init",
                       flags=["rest_recovery"])
    try:
        ws = websocket.create_connection(C.CLOB_WS_URL, timeout=10)
        ws.send(json.dumps({"type": "market",
                            "assets_ids": [spec.up_token_id, spec.down_token_id],
                            "custom_feature_enabled": True}))
    except Exception as e:  # noqa: BLE001
        log.warning("CLOB WS connect failed: %s", e)
        return
    last_snap = time.monotonic()
    while not stop.is_set():
        ws.settimeout(2)
        try:
            raw = ws.recv()
        except Exception:  # noqa: BLE001
            continue
        try:
            msgs = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for msg in (msgs if isinstance(msgs, list) else [msgs]):
            if not isinstance(msg, dict):
                continue
            with lock:   # mutate the book and append its event(s) as one critical section
                res = coord.ingest_clob(msg, recv_ts=server_now_cached())
                # commit=False: batch high-rate events; flushed by the periodic snapshot commit
                # below (and on exit), dropping per-event fsync from ~40+/s.
                _persist_book_events(conn, coord, spec, res)
        if time.monotonic() - last_snap >= 5:
            for tok in (spec.up_token_id, spec.down_token_id):
                with lock:
                    coord.persist_snapshot(conn, tok, code_version=CODE_VERSION,
                                           source_event="periodic", recv_ts=server_now_cached(),
                                           now_ts=server_now_cached())   # commits -> flushes batched events
            last_snap = time.monotonic()
    with lock:        # final flush of any batched events before the thread exits
        conn.commit()
    try:
        ws.close()
    except Exception:  # noqa: BLE001
        pass


def _binance_thread(stop: threading.Event, conn, lock, *, poll_interval_s: int = 1):
    """#7: continuously poll Binance BTCUSDT (public REST, no auth) and record observations into
    crypto5m_binance_observations (predictor lane). Idempotent on dedup_key (one per source-second), so
    feature families C/D have a real Binance series instead of just a single window-start reference."""
    from sniper.algos.polymarket_crypto5m.binance_feed import BinanceObservation
    while not stop.is_set():
        try:
            now = server_now_cached()
            price = binance_btc_price()
            obs = BinanceObservation("BTCUSDT", now * 1000, now, price, None, None, None,
                                     "binance_rest_ticker", {"src": "rest_ticker", "price": price})
            with lock:
                P.record_binance_observation(conn, obs, now_ts=now)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(poll_interval_s)


def capture_window(spec, conn, *, duration_s: int, stale_book_ms: int = 3000,
                   rest_reconcile_s: int = 30, lock=None):
    """Capture RTDS + CLOB for one market window for ``duration_s`` seconds, then attempt settlement.

    ``lock`` serializes every write on ``conn``. Pass a SHARED lock (and reuse the same ``conn``) when
    driving multiple windows so writes stay serialized; a fresh per-window lock is only safe when one
    window runs at a time."""
    lock = lock if lock is not None else threading.Lock()
    with lock:
        P.upsert_market(conn, spec, code_version=CODE_VERSION, source_lineage="gamma",
                        now_ts=server_now_cached())
        from sniper.algos.polymarket_crypto5m import rule_contract as RC
        P.record_rule(conn, condition_id=spec.condition_id, rule_text=spec.raw_rule_text,
                      rule_text_hash=spec.rule_text_hash, resolution_source=spec.resolution_source,
                      verdict=RC._verdict(RC.RuleReason.SUPPORTED), now_ts=server_now_cached())
        # Binance proxy reference at window start (Lane B)
        try:
            from sniper.algos.polymarket_crypto5m.models import ReferenceObservation
            P.record_reference_observation(conn, ReferenceObservation(
                spec.condition_id, "binance", "start", server_now_cached(), binance_btc_price()),
                code_version=CODE_VERSION, now_ts=server_now_cached())
        except Exception as e:  # noqa: BLE001
            log.warning("binance start ref failed: %s", e)

    coord = C.CaptureCoordinator(spec, stale_book_ms=stale_book_ms)
    stop = threading.Event()
    threads = [
        threading.Thread(target=_rtds_thread, args=(stop, conn, lock, spec.window_start_ts), daemon=True),
        threading.Thread(target=_clob_thread, args=(stop, conn, lock, coord, spec), daemon=True),
        threading.Thread(target=_monitor_thread, args=(stop, conn, lock, coord, spec),
                         kwargs={"stale_book_ms": stale_book_ms, "rest_reconcile_s": rest_reconcile_s},
                         daemon=True),
        threading.Thread(target=_binance_thread, args=(stop, conn, lock),
                         kwargs={"poll_interval_s": 1}, daemon=True),   # #7 continuous Binance
    ]
    for t in threads:
        t.start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < duration_s:
        time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    try:    # per-window data-quality record (delivery mode / staleness / clock) — never fails capture
        _record_window_integrity(conn, lock, spec, now=server_now_cached())
    except Exception as e:  # noqa: BLE001
        log.warning("window integrity record failed: %s", e)

    # settlement attempt (only meaningful if the window already resolved in gamma)
    try:
        ev = _get_json(f"https://gamma-api.polymarket.com/events?slug={spec.slug}")
        if ev:
            rspec, _ = MR.parse_event(ev[0])
            if rspec is not None:
                st = S.from_gamma(rspec)
                with lock:
                    P.upsert_market(conn, rspec, code_version=CODE_VERSION, source_lineage="gamma",
                                    now_ts=server_now_cached())
                    P.record_settlement(conn, st, code_version=CODE_VERSION, now_ts=server_now_cached())
                log.info("settlement: winner=%s uma=%s p2b_oracle=%s", st.winning_outcome,
                         st.uma_resolution_status, st.gamma_price_to_beat)
                # #7: reconstruct Price-to-Beat candidates from captured Chainlink ticks vs the oracle
                try:
                    from sniper.algos.polymarket_crypto5m import price_to_beat_resolver as P2BR
                    oracle = rspec.price_to_beat if rspec.price_to_beat is not None else st.gamma_price_to_beat
                    with lock:
                        P2BR.reconstruct_window(conn, condition_id=spec.condition_id,
                                                window_start_ts=spec.window_start_ts, oracle=oracle,
                                                code_version=CODE_VERSION, now_ts=server_now_cached())
                except Exception as e:  # noqa: BLE001
                    log.warning("p2b reconstruction failed: %s", e)
    except Exception as e:  # noqa: BLE001
        log.warning("settlement fetch failed: %s", e)


def _record_window_integrity(conn, lock, spec, *, now) -> None:
    """Record per-window RTDS delivery mode + freshest-tick staleness + clock source/offset, so a
    window's data quality is auditable: snapshot-only vs live updates, how stale Chainlink was, and that
    Binance server time was the clock (with the local-vs-server offset, not a hidden assumption). Reads
    the window's persisted ticks; classify_delivery/staleness_ms are the tested pure helpers."""
    rows = conn.execute("SELECT is_snapshot, source_ts_ms FROM crypto5m_chainlink_ticks "
                        "WHERE window_start_ts=?", (spec.window_start_ts,)).fetchall()
    ticks = [types.SimpleNamespace(is_snapshot=bool(r["is_snapshot"]), source_ts_ms=r["source_ts_ms"])
             for r in rows]
    mode = RTDS.classify_delivery(ticks)
    stale = RTDS.staleness_ms(ticks, now_ms=int(now) * 1000)
    offset_ms = int((time.time() - int(now)) * 1000)        # local wall vs binance-server (records skew)
    with lock:
        P.record_clock(conn, clock_source="binance_server", clock_offset_ms=offset_ms,
                       clock_last_verified_ts=int(now), now_ts=int(now),
                       delivery_mode=mode, staleness_ms=stale)


def _window_capture_seconds(spec, now, *, cap=None) -> int:
    """How long to capture a window: until it CLOSES (window_end - now), not a fixed slice. A fixed
    60s duration captured only the first 60s of a 300s window and missed the run-up to close. ``cap``
    (optional, from --duration-minutes) bounds it for tests/smokes; None = full remaining window."""
    remaining = int(spec.window_end_ts) - int(now)
    dur = remaining if remaining > 0 else 0
    if cap and cap > 0:
        dur = min(dur, cap)
    return dur


def run_continuous(conn, *, max_windows, discovery_interval_s, settlement_timeout_s,
                   capture_duration, stale_book_ms, rest_reconcile_s, max_concurrent_windows=2):
    """Continuous rollover driver (process-level loop, not a shell loop). Uses the tested
    WindowLifecycle + reconciliation cores: rediscover -> capture current+next CONCURRENTLY ->
    tick -> auto-reconcile closed windows -> prune. All conn writes serialized under one shared lock.
    Capture-only; no orders."""
    mgr = WindowLifecycle(settlement_timeout_s=settlement_timeout_s, max_windows=max_windows)
    lock = threading.Lock()   # ONE lock for all writes on this conn (shared across windows + threads)
    reconciled = 0
    while max_windows is None or reconciled < max_windows:
        now = server_now()
        for spec in discover_current(now, max_markets=2, fixture_mode=False):
            if mgr.discover(spec, now=now):
                with lock:
                    P.upsert_market(conn, spec, code_version=CODE_VERSION, source_lineage="gamma",
                                    now_ts=now)
        # capture DISCOVERED windows CONCURRENTLY (current + next). start_capture is what moves a
        # window DISCOVERED->CAPTURING, so it MUST be driven off discovered_windows(), not live.
        to_capture = mgr.discovered_windows()[:max_concurrent_windows]
        cap_threads = []
        for spec in to_capture:
            mgr.start_capture(spec.condition_id)
            dur = _window_capture_seconds(spec, server_now(), cap=capture_duration)
            log.info("[continuous] capturing %s for %ds until close (%d reconciled so far)",
                     spec.slug, dur, reconciled)
            t = threading.Thread(target=capture_window, args=(spec, conn),
                                 kwargs={"duration_s": dur, "stale_book_ms": stale_book_ms,
                                         "rest_reconcile_s": rest_reconcile_s, "lock": lock},
                                 daemon=True)
            t.start()
            cap_threads.append(t)
        for t in cap_threads:
            t.join()
        mgr.tick(now=server_now())
        for spec in mgr.awaiting_windows():
            try:
                st = RECON.reconcile_market(_get_json, spec)
            except Exception as e:  # noqa: BLE001
                log.warning("[continuous] reconcile fetch failed for %s: %s", spec.slug, e)
                continue
            if st and st.winning_outcome:
                with lock:
                    P.record_settlement(conn, st, code_version=CODE_VERSION, now_ts=server_now())
                try:    # #7: reconstruct P2B candidates now that the oracle is known
                    from sniper.algos.polymarket_crypto5m import price_to_beat_resolver as P2BR
                    with lock:
                        P2BR.reconstruct_window(conn, condition_id=spec.condition_id,
                                                window_start_ts=spec.window_start_ts,
                                                oracle=st.gamma_price_to_beat, code_version=CODE_VERSION,
                                                now_ts=server_now())
                except Exception as e:  # noqa: BLE001
                    log.warning("[continuous] p2b reconstruction failed for %s: %s", spec.slug, e)
                mgr.mark_reconciled(spec.condition_id)
                reconciled += 1
                log.info("[continuous] reconciled %s winner=%s", spec.slug, st.winning_outcome)
        mgr.prune_terminal()
        if not to_capture and not mgr.awaiting_windows():
            time.sleep(min(discovery_interval_s, 30))
    log.info("[continuous] done: %d windows reconciled", reconciled)
    return reconciled


def main(argv=None):
    ap = argparse.ArgumentParser(description="Polymarket crypto-5m Phase-0 capture (capture-only).")
    ap.add_argument("--asset", default="BTC", choices=["BTC"])
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--capture-duration", type=int, default=60, help="seconds to capture per window")
    ap.add_argument("--duration-minutes", type=float, default=None,
                    help="alternative to --capture-duration, in minutes")
    ap.add_argument("--discover-only", action="store_true")
    ap.add_argument("--report", action="store_true", help="print the Phase-0 report and exit")
    ap.add_argument("--continuous", action="store_true",
                    help="continuous rollover (process loop) accumulating resolved windows")
    ap.add_argument("--max-windows", type=int, default=None,
                    help="stop continuous mode after N reconciled windows")
    ap.add_argument("--discovery-interval-seconds", type=int, default=60)
    ap.add_argument("--settlement-timeout-seconds", type=int, default=600)
    ap.add_argument("--max-concurrent-windows", type=int, default=2,
                    help="windows captured concurrently in --continuous mode (current+next)")
    ap.add_argument("--rest-reconcile-seconds", type=int, default=30)
    ap.add_argument("--stale-book-ms", type=int, default=3000)
    ap.add_argument("--max-markets", type=int, default=1)
    ap.add_argument("--fixture-mode", action="store_true",
                    help="discover from local fixtures, NO network (implies --discover-only)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

    # fixture mode is a no-network mode: there is no fixtured live capture, so it never runs the live
    # RTDS/CLOB/Binance loop — force discover/report-only.
    if args.fixture_mode:
        args.discover_only = True

    # capture runs RTDS + CLOB in worker threads; writes are serialized under a lock (see capture_window)
    conn = P.connect(args.db_path, check_same_thread=False)
    P.ensure_schema(conn)
    log.info("db ready at %s", args.db_path)

    if args.report:
        print(REP.format_report(REP.build_report(conn)))
        return 0

    if args.continuous:
        # continuous captures each window to its close; --duration-minutes is an OPTIONAL cap (None =
        # full window). --capture-duration is the single-shot duration only, not a continuous truncation.
        cap = int(args.duration_minutes * 60) if args.duration_minutes else None
        run_continuous(conn, max_windows=args.max_windows,
                       discovery_interval_s=args.discovery_interval_seconds,
                       settlement_timeout_s=args.settlement_timeout_seconds,
                       capture_duration=cap, stale_book_ms=args.stale_book_ms,
                       rest_reconcile_s=args.rest_reconcile_seconds,
                       max_concurrent_windows=args.max_concurrent_windows)
        print(REP.format_report(REP.build_report(conn)))
        return 0

    now = 0 if args.fixture_mode else server_now()
    specs = discover_current(now, max_markets=args.max_markets, fixture_mode=args.fixture_mode)
    log.info("capture-ready markets: %d", len(specs))
    if args.discover_only or not specs:
        for spec in specs:
            P.upsert_market(conn, spec, code_version=CODE_VERSION, source_lineage="gamma",
                            now_ts=(now or 1))
        print(REP.format_report(REP.build_report(conn)))
        return 0

    duration = int(args.duration_minutes * 60) if args.duration_minutes else args.capture_duration
    for spec in specs:
        log.info("capturing %s for %ds (window %d-%d)", spec.slug, duration,
                 spec.window_start_ts, spec.window_end_ts)
        capture_window(spec, conn, duration_s=duration, stale_book_ms=args.stale_book_ms,
                       rest_reconcile_s=args.rest_reconcile_seconds)
    print(REP.format_report(REP.build_report(conn)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
