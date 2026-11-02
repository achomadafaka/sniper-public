#!/usr/bin/env python3
# scripts/run_far_whale_capture.py
"""fwlm-v0 far-whale magnet capture — forward-only, paper-only, public data only.

Spec: docs/superpowers/specs/2026-07-04-hl-far-whale-liquidation-magnet-paper-study.md
Per round (local wall clock, self-referential — spec §10):
  market_state for ALL top-N assets (Control-3 pool) -> harvest+poll whales ->
  snapshots (sweep pool) -> [if thresholds frozen] candidates/episodes/entries ->
  persistence tracking (Control 4, diagnostic only — never filters the primary set) ->
  step open paper entries. NO candidates or entries before a freeze row exists."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import far_whale as FW            # noqa: E402
from sniper.hyperlive import far_whale_sim as SIM        # noqa: E402
from sniper.hyperlive import far_whale_store as S        # noqa: E402

HL_INFO = "https://api.hyperliquid.xyz/info"
DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite"
POLL_INTERVAL_S = 180
SHRANK_FRAC = 0.50


def _info(payload: dict):
    import requests
    r = requests.post(HL_INFO, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_ctxs_live(max_assets: int = 20) -> dict:
    """top-N by 24h notional -> {ASSET: {mid, oi, day_ntl_vlm, spread_bps}}.
    spread from impactPxs (impact bid/ask); missing -> None (candidate gate fails closed)."""
    meta, ctxs = _info({"type": "metaAndAssetCtxs"})
    names = [m["name"] for m in meta.get("universe") or []]
    pairs = sorted(zip(names, ctxs or []), key=lambda x: -float(x[1].get("dayNtlVlm") or 0))
    out = {}
    for name, c in pairs[:max_assets]:
        mid = c.get("midPx") or c.get("markPx")
        if not mid:
            continue
        mid = float(mid)
        spread = None
        imp = c.get("impactPxs")
        if isinstance(imp, list) and len(imp) == 2:
            try:
                bid, ask = float(imp[0]), float(imp[1])
                if ask > bid > 0:
                    spread = (ask - bid) / mid * 1e4
            except (TypeError, ValueError):
                pass
        out[name.upper()] = {"mid": mid,
                             "oi": float(c.get("openInterest") or 0) or None,
                             "day_ntl_vlm": float(c.get("dayNtlVlm") or 0) or None,
                             "spread_bps": spread}
    return out


def harvest_live(assets: list) -> list:
    """Leaderboard whales UNION trades-WS actives — same multi-source pattern as
    run_cluster_map (whales first so the cap can't starve them)."""
    from scripts.run_cluster_map import _harvest_addresses  # reuse, don't fork
    return _harvest_addresses(assets, 20, 700, leaderboard_top=200)


def fetch_state_live(address: str) -> dict:
    return _info({"type": "clearinghouseState", "user": address})


def run_round(conn, *, fetch_ctxs, fetch_state, harvest, now_ms: int,
              max_assets: int = 20) -> dict:
    ctxs = fetch_ctxs()
    S.save_market_state(conn, {a: (c["mid"], c.get("oi"), c.get("day_ntl_vlm"),
                                   c.get("spread_bps")) for a, c in ctxs.items()},
                        ts_ms=now_ms)

    episodes = S.open_episodes(conn)
    tracked = {e["whale_address"] for e in episodes}
    addrs = list(dict.fromkeys(list(harvest(list(ctxs))) + sorted(tracked)))

    polled: dict = {}          # address -> (whale_rows | None on poll failure, raw_state)

    def _poll(a):
        try:
            st = fetch_state(a)
            return a, FW.whale_rows_from_clearinghouse(st, a), st
        except Exception as e:  # noqa: BLE001 — poll failure = source_disappeared signal
            print(f"poll fail {a[:10]}: {type(e).__name__}", file=sys.stderr)
            return a, None, None

    with ThreadPoolExecutor(max_workers=24) as ex:
        for a, rows, st in ex.map(_poll, addrs):
            polled[a] = (rows, st)

    in_band = []
    for rows, _st in polled.values():
        for r in rows or []:
            mid = (ctxs.get(r["asset"]) or {}).get("mid")
            if not mid:
                continue
            dist = abs(r["liquidation_price"] - mid) / mid * 1e4
            if FW.DIST_MIN_BPS <= dist <= FW.DIST_MAX_BPS \
                    and r["position_notional"] >= FW.SWEEP_FLOOR_NOTIONAL:
                in_band.append(r)
    S.save_snapshots(conn, now_ms, in_band)

    freeze = S.load_freeze(conn)
    stats = {"addresses_polled": len(addrs), "whales_seen": len(in_band),
             "anomalies_dropped": 0, "candidates_new": 0, "sweep_mode": freeze is None}

    if freeze is not None:
        thresholds = freeze["thresholds"]
        for addr, (rows, st) in polled.items():
            for r in rows or []:
                thr = thresholds.get(r["asset"])
                if thr is None:
                    continue
                c = ctxs.get(r["asset"]) or {}
                cand, reason = FW.evaluate_candidate(
                    r, c.get("mid"), spread_bps=c.get("spread_bps"),
                    staleness_ms=0, threshold_notional=float(thr))
                if reason == "anomaly_side_invariant":
                    stats["anomalies_dropped"] += 1
                if cand is None:
                    continue
                episode_id, is_new = S.assign_episode(conn, cand, now_ms)
                snap_hash = hashlib.sha256(
                    json.dumps(st, sort_keys=True).encode()).hexdigest()[:16]
                cid = S.insert_candidate(conn, cand, episode_id=episode_id,
                                         signal_ts_ms=now_ms, is_primary=int(is_new),
                                         source_snapshot_hash=snap_hash)
                stats["candidates_new"] += 1
                if is_new:
                    entry = SIM.plan_entry(cand, now_ms)
                    entry.update({"candidate_id": cid, "episode_id": episode_id,
                                  "is_primary": 1})
                    S.insert_entry(conn, entry)

    # --- Control 4 persistence tracking (diagnostic; never filters the primary set) ---
    events: dict = {}          # episode_id -> whale_event
    for ep in episodes:
        rows, _ = polled.get(ep["whale_address"], (None, None))
        if rows is None:
            status = "source_disappeared"
        else:
            match = [r for r in rows if r["asset"] == ep["asset"]]
            if not match:
                status = "closed"
            else:
                r = match[0]
                drift = abs(r["liquidation_price"] - ep["ref_liquidation_price"]) \
                    / ep["ref_liquidation_price"] * 1e4
                first_notl = S.first_candidate_notional(conn, ep["episode_id"])
                if drift > FW.EPISODE_LIQ_TOL_BPS:
                    status = "liq_price_moved"
                elif first_notl is not None and r["position_notional"] < SHRANK_FRAC * first_notl:
                    status = "shrank"
                else:
                    status = "persisted"
        if status == "persisted":
            S.update_episode(conn, ep["episode_id"], last_seen_ts_ms=now_ms)
        else:
            S.update_episode(conn, ep["episode_id"], status=status)
            events[ep["episode_id"]] = status

    # --- step open paper entries against current mids ---
    ep_status = S.episode_status_map(conn)
    for entry in S.open_entries(conn):
        c = ctxs.get(entry["asset"]) or {}
        mid = c.get("mid")
        if not mid:
            continue
        whale_event = events.get(entry["episode_id"]) or entry.get("whale_event")
        exit_ = SIM.step_entry(entry, mid, now_ms,
                               whale_event=("liq_price_moved"
                                            if whale_event == "liq_price_moved" else None))
        S.update_entry_path(conn, entry["entry_id"], mfe_bps=entry["mfe_bps"],
                            mae_bps=entry["mae_bps"], whale_event=whale_event)
        if exit_ is None:
            continue
        status_now = ep_status.get(entry["episode_id"], "open")
        persisted = 1 if status_now in ("open",) else 0
        # liq_fired: only knowable when the whale is definitively gone (closed /
        # source_disappeared) — approximated by "exited near the frozen level".
        # Any other status (open/shrank/liq_price_moved) means the whale's fate
        # is still unresolved as of this trade's exit -> NULL, never 0. Coding an
        # unobserved future event as "did not fire" would silently assert a fact
        # we don't have (CLAUDE.md: distinguish None from a known 0/false).
        liq_fired = None
        if status_now in ("closed", "source_disappeared"):
            dist_now = abs(mid - entry["liquidation_price"]) / mid * 1e4
            liq_fired = 1 if dist_now <= FW.EPISODE_LIQ_TOL_BPS else 0   # documented approximation
        out = SIM.settle(entry, exit_, position_persisted=persisted, liq_fired=liq_fired)
        S.settle_entry(conn, entry["entry_id"], out)

    S.save_round(conn, now_ms, stats)
    print(f"fwlm round ts={now_ms} addrs={stats['addresses_polled']} "
          f"whales={stats['whales_seen']} anomalies={stats['anomalies_dropped']} "
          f"cands={stats['candidates_new']} sweep_mode={stats['sweep_mode']}", flush=True)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="fwlm-v0 far-whale capture (paper, read-only)")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--max-assets", type=int, default=20)
    ap.add_argument("--interval-seconds", type=int, default=POLL_INTERVAL_S)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    conn = S.connect(args.db)
    S.ensure_schema(conn)
    try:
        while True:
            try:
                run_round(conn, fetch_ctxs=lambda: fetch_ctxs_live(args.max_assets),
                          fetch_state=fetch_state_live, harvest=harvest_live,
                          now_ms=int(time.time() * 1000), max_assets=args.max_assets)
            except Exception as e:  # noqa: BLE001
                print(f"fwlm round failed: {type(e).__name__}: {e}", file=sys.stderr)
            if args.once:
                return 0
            time.sleep(args.interval_seconds)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
