#!/usr/bin/env python3
"""Run the Hyperlive engine. Public HL market data only, paper only, no keys/orders.
Feeds bbo/trades/activeAssetCtx into the compute layer, steps the engine, persists the
public state the UI polls. --fixture-mode runs a deterministic no-network smoke."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive.compute import RollingState  # noqa: E402
from sniper.hyperlive.engine import HyperliveEngine, Portfolio  # noqa: E402
from sniper.hyperlive import setups as HS  # noqa: E402
from sniper.hyperlive import persistence as P  # noqa: E402
from sniper.hyperlive import universe as UNI  # noqa: E402
from sniper.hyperlive import scan_log as SL  # noqa: E402

HL_WS = "wss://api.hyperliquid.xyz/ws"
HL_INFO = "https://api.hyperliquid.xyz/info"
DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite"
BINANCE_DB = "/home/bigpapi/Desktop/Sniper/data/leadlag_capture.sqlite"
BINANCE_SYMBOL_MAP = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}
SCAN_LOG_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_scan_log.sqlite"
# A real Binance<->HL basis on a perp is single/double-digit bps; anything past 10% is a
# mismatched symbol or a stale mid (e.g. HL 'PIXEL' != Binance PIXELUSDT), NOT a dislocation.
MAX_SANE_BASIS_BPS = 1000.0


def _fetch_binance_book(timeout=10) -> dict:
    """One REST snapshot of ALL Binance USD-M perp best bid/ask -> {ASSET: mid}. Used to log a
    contemporaneous cross-venue basis at refresh time. Public data; selection-shadow only.
    Sampled at refresh (same instant as HL contexts) so basis is not a clock-skew artifact."""
    import urllib.request
    req = urllib.request.Request("https://fapi.binance.com/fapi/v1/ticker/bookTicker",
                                 headers={"User-Agent": "curl/8"})
    out = {}
    for t in json.load(urllib.request.urlopen(req, timeout=timeout)):
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            bid, ask = float(t["bidPrice"]), float(t["askPrice"])
        except (KeyError, TypeError, ValueError):
            continue
        if bid > 0 and ask > 0:
            out[sym[:-4]] = (bid + ask) / 2
    return out


def _log_scan(slconn, contexts, ranking, watched_set, prev_oi, prev_ctx, prev2_ctx, scan_ts, refresh_s,
              binance_book=None):
    """Step-0 recall log: persist all ~230 scored markets + Step-1 shadow change signals +
    (when mapped) the contemporaneous Binance->HL basis. Append-only; does not touch live selection."""
    binance_book = binance_book or {}
    selected = {r["asset"]: i + 1 for i, r in enumerate(ranking)}
    rows = []
    for r in UNI.score_all(contexts, prev_oi):
        a, ctx = r["asset"], r["ctx"]
        cs = UNI.change_signals(ctx, prev_ctx.get(a), prev2_ctx.get(a))
        oi = UNI._fn(ctx, "openInterest")
        prior_oi = prev_oi.get(a)
        hl_mid = UNI._fn(ctx, "midPx") or UNI._fn(ctx, "markPx")
        bmid = binance_book.get(a)
        basis_bps = ((bmid - hl_mid) / hl_mid * 1e4) if (bmid and hl_mid and hl_mid > 0) else None
        if basis_bps is not None and abs(basis_bps) > MAX_SANE_BASIS_BPS:
            basis_bps = None        # mismatched symbol / stale mid, not a real dislocation
        rows.append({
            "asset": a, "selected_rank": selected.get(a), "selected_bool": a in selected,
            "already_watched_bool": a in watched_set,
            "hand_score_v0": r["score"], "change_score_v0": cs["change_score_v0"],
            "score_components": r["components"], "change_components": cs, "lead_reason": r["lead_reason"],
            "day_ntl_vlm": r["volume_usd"], "mark_px": UNI._fn(ctx, "markPx"),
            "oracle_px": UNI._fn(ctx, "oraclePx"), "mid_px": UNI._fn(ctx, "midPx"),
            "funding": UNI._fn(ctx, "funding"), "premium": UNI._fn(ctx, "premium"),
            "prev_day_px": UNI._fn(ctx, "prevDayPx"), "open_interest": oi, "prior_open_interest": prior_oi,
            "oi_delta": ((oi - prior_oi) / prior_oi if (oi and prior_oi) else None),
            "binance_basis_bps": (round(basis_bps, 2) if basis_bps is not None else None),
        })
    SL.log_refresh(slconn, scan_ts, rows, refresh_interval_s=refresh_s, watchlist_state=",".join(watched_set))


def _load_binance_mids(bconn) -> dict:
    """Freshest Binance bookTicker mid per mapped asset (cross-venue lead). Scans the last
    rows by PK (fast) and keeps the newest per symbol."""
    if bconn is None:
        return {}
    out = {}
    try:
        rows = bconn.execute(
            "SELECT symbol, best_bid, best_ask FROM binance_raw_events "
            "WHERE id > (SELECT MAX(id) - 800 FROM binance_raw_events) AND best_bid IS NOT NULL "
            "ORDER BY id DESC").fetchall()
    except sqlite3.Error:
        return {}
    for sym, bid, ask in rows:
        a = BINANCE_SYMBOL_MAP.get(sym)
        if a and a not in out and bid and ask:
            out[a] = (float(bid) + float(ask)) / 2
    return out


def _now_ms() -> int:
    return int(time.time() * 1000)


def _make_engines(conn):
    """Parallel horizon tracks sharing one portfolio guard: FAST (seconds-minutes) and
    SWING (minutes-hours). Each is an independent state machine over the same snapshots."""
    pf = Portfolio()

    def on_event(track):
        def cb(kind, payload):
            if kind == "evaluated":
                P.record_cycle(conn, {**payload["close"], "track": track}, cycle_id=payload["cycle"])
        return cb

    return [
        HyperliveEngine(track="fast", allowed_setups=HS.TRACK_SETUPS["fast"], portfolio=pf, on_event=on_event("fast")),
        HyperliveEngine(track="swing", allowed_setups=HS.TRACK_SETUPS["swing"], portfolio=pf, on_event=on_event("swing")),
    ]


def _step_and_save(conn, engines, snapshots, ts, universe=None):
    tracks = {eng.track: eng.step(snapshots, ts) for eng in engines}
    combined = {"tracks": tracks, "universe": universe, "updated_ts_ms": ts,
                "state": tracks.get("fast", {}).get("state")}   # top-level for back-compat
    P.save_state(conn, combined, ts_ms=ts)
    return combined


def _run_fixture(conn, args) -> int:
    rs = RollingState()
    engines = _make_engines(conn)
    assets = ["HYPE", "SOL", "BTC"]
    base = {"HYPE": 67.0, "SOL": 150.0, "BTC": 64000.0}
    ctx = {"openInterest": "1000000", "funding": "0.00001"}
    t0 = 1_800_000_000_000
    px = dict(base)
    # 60s calm warmup (builds the ret60 window), then a sharp DOWN cascade on HYPE:
    # price falls, forced sells (side A -> short continuation), engine shorts and rides it.
    for i in range(120):
        ts = t0 + i * 1000
        for a in assets:
            if a == "HYPE" and i >= 60:
                px[a] *= 0.996                      # ~-40bps/step downward cascade
            rs.on_bbo(a, ts, px[a] * 0.9999, px[a] * 1.0001, 500, 500)
            rs.on_ctx(a, ts, {**ctx, "markPx": str(px[a]), "oraclePx": str(px[a])})
            if a == "HYPE" and i >= 60:
                rs.on_trade(a, ts, px[a], 8.0, "A", {"side": "A", "liquidation": {"liquidatedUser": "0x1", "method": "market"}})
            else:
                rs.on_trade(a, ts, px[a], 0.1, "B", {"side": "B"})
        snaps = [rs.snapshot(a, ts) for a in assets]
        _step_and_save(conn, engines, snaps, ts)
    print("hyperlive fixture done: " + " ".join(
        f"{e.track}=state:{e.state}/cycles:{e.cycle_id}/wins:{sum(1 for h in e.history if h['won'])}/{len(e.history)}"
        for e in engines))
    return 0


def _fetch_contexts() -> list[dict]:
    """One cheap call: per-coin state for every HL perp (volume, mark/oracle, funding,
    premium, OI, prevDay) — the substrate for the state-aware universe scan."""
    import urllib.request
    req = urllib.request.Request(HL_INFO, data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
                                 headers={"Content-Type": "application/json"})
    meta, ctxs = json.loads(urllib.request.urlopen(req, timeout=15).read())
    return [{"name": m["name"], "ctx": c} for m, c in zip(meta.get("universe") or [], ctxs or [])]


def _run_live(conn, args) -> int:
    import websocket
    closed_exc = getattr(websocket, "WebSocketConnectionClosedException", None)
    rs = RollingState()
    engines = _make_engines(conn)
    bconn = sqlite3.connect(f"file:{BINANCE_DB}?mode=ro", uri=True) if os.path.exists(BINANCE_DB) else None
    slconn = SL.connect(SCAN_LOG_DB)
    SL.ensure_schema(slconn)
    ws = websocket.create_connection(HL_WS, timeout=15)

    def _sub(action, a_list):
        for a in a_list:
            for t in ("bbo", "trades", "activeAssetCtx", "l2Book"):
                ws.send(json.dumps({"method": action, "subscription": {"type": t, "coin": a}}))

    def _ctx_map(ctxs):
        return {str(c["name"]).upper(): c["ctx"] for c in ctxs if c.get("name")}

    weights = UNI.load_weights()        # learned weights (promoted by the */30 audit) or None→HAND
    print(f"universe weights: {'LEARNED ' + str(weights) if weights else 'HAND (no fitted file yet)'}",
          file=sys.stderr)
    def _book():
        try:
            return _fetch_binance_book()
        except Exception as e:       # cross-venue basis is shadow-only; never block selection on it
            print(f"binance book fetch failed: {e}", file=sys.stderr)
            return {}

    contexts = _fetch_contexts()
    ranking = UNI.select_universe(contexts, max_assets=args.max_assets, weights=weights)
    assets = [r["asset"] for r in ranking]
    prev_oi = UNI.oi_map(contexts)
    prev_ctx, prev2_ctx = {}, {}
    _log_scan(slconn, contexts, ranking, set(), {}, {}, {}, _now_ms(), args.universe_refresh_seconds,
              binance_book=_book())
    prev_ctx = _ctx_map(contexts)
    _sub("subscribe", assets)

    last = 0
    last_refresh = time.monotonic()
    start = time.monotonic()
    try:
        while True:
            if args.duration_seconds > 0 and time.monotonic() - start >= args.duration_seconds:
                break
            ws.settimeout(1)
            try:
                raw = ws.recv()
                if not raw:
                    return 2
                _ingest(rs, json.loads(raw), _now_ms())
            except TimeoutError:
                pass
            except Exception as e:
                if (closed_exc and isinstance(e, closed_exc)) or "socket is already closed" in str(e).lower():
                    return 2
            now = _now_ms()
            # state-aware universe refresh: re-rank all perps by opportunity, re-subscribe deltas
            if time.monotonic() - last_refresh >= args.universe_refresh_seconds:
                try:
                    new_weights = UNI.load_weights()    # hot-reload: the */30 audit may have re-fit
                    if new_weights != weights:
                        print(f"universe weights updated -> "
                              f"{'LEARNED ' + str(new_weights) if new_weights else 'HAND'}", file=sys.stderr)
                        weights = new_weights
                    contexts = _fetch_contexts()
                    ranking = UNI.select_universe(contexts, prev_oi, max_assets=args.max_assets, weights=weights)
                    # Step 0/1: log all ~230 scored markets + shadow change + cross-venue basis
                    _log_scan(slconn, contexts, ranking, set(assets), prev_oi, prev_ctx, prev2_ctx,
                              now, args.universe_refresh_seconds, binance_book=_book())
                    new_assets = [r["asset"] for r in ranking]
                    _sub("unsubscribe", [a for a in assets if a not in new_assets])
                    _sub("subscribe", [a for a in new_assets if a not in assets])
                    assets = new_assets
                    prev2_ctx, prev_ctx = prev_ctx, _ctx_map(contexts)
                    prev_oi = UNI.oi_map(contexts)
                    last_refresh = time.monotonic()
                except Exception as e:
                    print(f"universe refresh failed: {e}", file=sys.stderr)
            if now - last >= args.decision_interval_ms:
                rs.cluster = P.load_clusters(conn, now_ms=now)   # cluster-map lane feeds entry timing
                rs.binance = _load_binance_mids(bconn)           # cross-venue lead feeds setups
                snaps = [rs.snapshot(a, now) for a in assets]
                _step_and_save(conn, engines, snaps, now, ranking)
                last = now
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return 0


def _ingest(rs, msg, ts):
    ch, data = msg.get("channel"), msg.get("data")
    if ch == "bbo" and isinstance(data, dict):
        bb = data.get("bbo") or []
        if len(bb) >= 2:
            a = str(data.get("coin") or "").upper()
            try:
                rs.on_bbo(a, ts, float(bb[0]["px"]), float(bb[1]["px"]), float(bb[0]["sz"]), float(bb[1]["sz"]))
            except (TypeError, ValueError, KeyError):
                pass
    elif ch == "l2Book" and isinstance(data, dict):
        a = str(data.get("coin") or "").upper()
        lv = data.get("levels") or [[], []]
        try:
            bids = [(float(x["px"]), float(x["sz"])) for x in (lv[0] or [])[:5]]
            asks = [(float(x["px"]), float(x["sz"])) for x in (lv[1] or [])[:5]]
            rs.on_l2book(a, ts, bids, asks)
        except (TypeError, ValueError, KeyError, IndexError):
            pass
    elif ch == "trades" and isinstance(data, list):
        for tr in data:
            a = str(tr.get("coin") or "").upper()
            try:
                rs.on_trade(a, ts, float(tr.get("px") or 0), float(tr.get("sz") or 0), tr.get("side"), tr)
            except (TypeError, ValueError):
                pass
    elif ch == "activeAssetCtx" and isinstance(data, dict):
        a = str(data.get("coin") or "").upper()
        rs.on_ctx(a, ts, data.get("ctx") or {})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hyperlive engine (paper, public data only)")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--max-assets", type=int, default=12)
    ap.add_argument("--decision-interval-ms", type=int, default=2000)
    ap.add_argument("--universe-refresh-seconds", type=int, default=150)
    ap.add_argument("--duration-seconds", type=int, default=0)
    ap.add_argument("--fixture-mode", action="store_true")
    args = ap.parse_args(argv)
    conn = P.connect(args.db_path)
    P.ensure_schema(conn)
    try:
        return _run_fixture(conn, args) if args.fixture_mode else _run_live(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
