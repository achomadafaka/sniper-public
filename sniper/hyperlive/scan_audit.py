"""Step-2 forward labels for the recall audit. For each (asset, scan_ts) in the all-230 log,
label forward moves from FUTURE CONTEXT ONLY at honest resolution (150s/300s — context
cadence; finer is impossible for the unwatched 218). Labels are universe-selection labels,
NEVER PnL."""
from __future__ import annotations

import json
import math
import sqlite3

# Phase-2 promotion bar (set BEFORE looking): mark-oracle acceleration must add at least
# this many percentage points of recall@12 to be promoted into the live score.
ACCEL_LIFT_BAR_PP = 5.0
ACCEL_WEIGHT = 1.5
ACCEL_NORM_BPS = 1.0          # 1 point per 1 bps/150s of |basis| change

HORIZONS_MS = (150_000, 300_000)
TOL_MS = 75_000              # a refresh is ~150s; accept the row nearest target within this
MIN_MOVE_BPS = 25.0         # cost-clearing hurdle: tightened from 10 so it means "tradeable
                            # move," not noise (a move must clear ~9bps fee with real margin)


def forward_labels(rows: list[dict], *, horizons_ms=HORIZONS_MS, tol_ms=TOL_MS,
                   min_move_bps=MIN_MOVE_BPS) -> list[dict]:
    """rows: one asset's scan rows [{scan_ts, mid, asset, spread_proxy_bps?}]. For each row,
    forward |move| + direction + a conservative cost-clearing flag (move > max(10bps, spread))."""
    rows = sorted((r for r in rows if r.get("mid")), key=lambda r: r["scan_ts"])
    ts = [r["scan_ts"] for r in rows]
    out = []
    for i, r in enumerate(rows):
        mid0 = r["mid"]
        if not mid0 or mid0 <= 0:
            continue
        rec = {"scan_ts": r["scan_ts"], "asset": r.get("asset")}
        any_label = False
        for H in horizons_ms:
            target = r["scan_ts"] + H
            best = None
            for j in range(i + 1, len(rows)):
                if ts[j] > target + tol_ms:
                    break
                if abs(ts[j] - target) <= tol_ms and (best is None or abs(ts[j] - target) < abs(ts[best] - target)):
                    best = j
            key = H // 1000
            if best is None:
                rec[f"abs_move_{key}s_bps"] = None
                rec[f"cost_clearing_{key}s"] = None
                continue
            move = (rows[best]["mid"] - mid0) / mid0
            hurdle = max(min_move_bps, float(r.get("spread_proxy_bps") or 0.0))
            rec[f"abs_move_{key}s_bps"] = round(abs(move) * 1e4, 2)
            rec[f"dir_{key}s"] = 1 if move > 0 else (-1 if move < 0 else 0)
            rec[f"cost_clearing_{key}s"] = bool(abs(move) * 1e4 > hurdle)
            any_label = True
        if any_label:
            out.append(rec)
    return out


def build_labels(scan_conn: sqlite3.Connection) -> list[dict]:
    """Read the all-230 scan log, group by asset, label forward moves. Returns all label rows."""
    rows = scan_conn.execute(
        "SELECT asset, scan_ts, mid_px FROM hyperlive_scan_log WHERE mid_px IS NOT NULL ORDER BY asset, scan_ts"
    ).fetchall()
    by_asset = {}
    for asset, scan_ts, mid in rows:
        by_asset.setdefault(asset, []).append({"asset": asset, "scan_ts": int(scan_ts), "mid": float(mid)})
    out = []
    for asset, ars in by_asset.items():
        out.extend(forward_labels(ars))
    return out


# Swing-horizon validation: the swing track shares the FAST universe selection (same top-12),
# but targets minutes-to-hours moves. These horizons let us measure whether that shared
# selection actually catches long-horizon movers — or whether swing needs its own ranking.
SWING_HORIZONS_MS = (1_800_000, 3_600_000)   # 30 min, 60 min
SWING_MIN_MOVE_BPS = 50.0                     # a swing worth holding clears more than the 25bps FAST floor


def recall_at_horizon(scan_conn: sqlite3.Connection, horizon_ms: int, *, top_n: int = 12,
                      min_volume: float | None = None, tol_ms: int = TOL_MS,
                      min_move_bps: float = MIN_MOVE_BPS) -> dict:
    """Recall@N of the logged top-N selection at an ARBITRARY forward horizon, tradeable pool
    only. selected_bool in the log IS the shared FAST/SWING universe pick, so this answers:
    does the universe we select catch the movers at `horizon_ms`? Forward move is measured from
    future logged mids (selection labels, never PnL)."""
    floor = _tradeable_floor(min_volume)
    rows = scan_conn.execute("""
        SELECT scan_ts, asset, mid_px, selected_bool, day_ntl_vlm
        FROM hyperlive_scan_log
        WHERE mid_px IS NOT NULL AND day_ntl_vlm IS NOT NULL AND day_ntl_vlm >= ?
        ORDER BY asset, scan_ts
    """, (floor,)).fetchall()
    by_asset: dict = {}
    for scan_ts, asset, mid, sel, _vol in rows:
        by_asset.setdefault(asset, []).append((int(scan_ts), float(mid), int(sel or 0)))
    by_ts: dict = {}
    for ars in by_asset.values():
        ars.sort(key=lambda x: x[0])
        ts = [a[0] for a in ars]
        for i, (t0, mid0, sel) in enumerate(ars):
            if mid0 <= 0:
                continue
            target = t0 + horizon_ms
            best = None
            for j in range(i + 1, len(ars)):
                if ts[j] > target + tol_ms:
                    break
                if abs(ts[j] - target) <= tol_ms and (best is None or abs(ts[j] - target) < abs(ts[best] - target)):
                    best = j
            if best is None:
                continue
            move = abs(ars[best][1] - mid0) / mid0
            by_ts.setdefault(t0, []).append({"sel": sel == 1, "mover": move * 1e4 > min_move_bps})
    movers = caught = sel_n = sel_mv = scans = 0
    for items in by_ts.values():
        tm = [x for x in items if x["mover"]]
        if not tm:
            continue
        scans += 1
        movers += len(tm)
        caught += sum(1 for x in tm if x["sel"])
        sels = [x for x in items if x["sel"]]
        sel_n += len(sels)
        sel_mv += sum(1 for x in sels if x["mover"])
    horizon_s = horizon_ms // 1000
    if movers == 0:
        return {"status": "insufficient", "horizon_s": horizon_s, "scans": scans, "movers": 0}
    return {"status": "ok", "horizon_s": horizon_s, "scans": scans, "movers": movers,
            "recall": round(caught / movers, 3),
            "precision": round(sel_mv / sel_n, 3) if sel_n else 0.0,
            "min_move_bps": min_move_bps}


def recall_lift(scan_conn: sqlite3.Connection, *, top_n: int = 12, min_volume: float = 0.0) -> dict:
    """Phase-2 shadow recall-lift: does adding mark-oracle ACCELERATION to the live score
    catch more cost-clearing movers in the top-N, on the logged data? Re-ranks each refresh
    by hand_score_v0 vs accel_score_v0 (= hand + accel component) and compares recall@12.
    min_volume restricts to the tradeable pool (production passes the floor). Changes nothing
    live — it answers whether to promote, from data."""
    rows = scan_conn.execute("""
        SELECT s.scan_ts, s.asset, s.hand_score_v0, s.change_components, l.cost_clearing_150s
        FROM hyperlive_scan_log s
        JOIN hyperlive_scan_labels l ON s.asset = l.asset AND s.scan_ts = l.scan_ts
        WHERE l.cost_clearing_150s IS NOT NULL AND s.hand_score_v0 IS NOT NULL
          AND (? <= 0 OR (s.day_ntl_vlm IS NOT NULL AND s.day_ntl_vlm >= ?))
    """, (min_volume, min_volume)).fetchall()
    by_ts: dict = {}
    for scan_ts, asset, hand, change_json, mover in rows:
        try:
            accel = abs(float((json.loads(change_json or "{}")).get("basis_accel_bps") or 0.0))
        except (ValueError, TypeError):
            accel = 0.0
        accel_score = float(hand) + ACCEL_WEIGHT * min(12.0, accel / ACCEL_NORM_BPS)
        by_ts.setdefault(scan_ts, []).append({"asset": asset, "hand": float(hand),
                                              "accel": accel_score, "mover": mover == 1})
    movers = hand_caught = accel_caught = hand_top_movers = accel_top_movers = scans = 0
    for ms in by_ts.values():
        ts_movers = [m for m in ms if m["mover"]]
        if not ts_movers:
            continue
        scans += 1
        hand_top = {m["asset"] for m in sorted(ms, key=lambda x: -x["hand"])[:top_n]}
        accel_top = {m["asset"] for m in sorted(ms, key=lambda x: -x["accel"])[:top_n]}
        movers += len(ts_movers)
        hand_caught += sum(1 for m in ts_movers if m["asset"] in hand_top)
        accel_caught += sum(1 for m in ts_movers if m["asset"] in accel_top)
        hand_top_movers += sum(1 for m in ms if m["asset"] in hand_top and m["mover"])
        accel_top_movers += sum(1 for m in ms if m["asset"] in accel_top and m["mover"])
    if movers == 0:
        return {"status": "insufficient", "scans": scans, "movers": 0}
    hand_recall, accel_recall = hand_caught / movers, accel_caught / movers
    denom = max(1, top_n * scans)
    lift_pp = (accel_recall - hand_recall) * 100
    return {
        "status": "ok", "scans": scans, "movers": movers,
        "hand_recall_at_12": round(hand_recall, 3), "accel_recall_at_12": round(accel_recall, 3),
        "lift_pp": round(lift_pp, 2),
        "hand_precision_at_12": round(hand_top_movers / denom, 3),
        "accel_precision_at_12": round(accel_top_movers / denom, 3),
        "bar_pp": ACCEL_LIFT_BAR_PP,
        "promote": bool(lift_pp >= ACCEL_LIFT_BAR_PP and accel_top_movers >= hand_top_movers),
    }


def horse_race(scan_conn: sqlite3.Connection, *, top_n: int = 12, train_frac: float = 0.6,
               min_volume: float = 0.0) -> dict:
    """Score horse-race on held-out time blocks: which scoring catches the most cost-clearing
    movers in the top-N? Compares hand_score_v0 vs +acceleration vs +BTC-lead vs a LEARNED
    linear score (weights fit on train by each signal's mover/non-mover separation). min_volume
    restricts to the tradeable pool (production passes the floor). Read-only — it recommends a
    score, it does not change live selection."""
    rows = scan_conn.execute("""
        SELECT s.scan_ts, s.asset, s.score_components, s.change_components, s.hand_score_v0, l.cost_clearing_150s
        FROM hyperlive_scan_log s
        JOIN hyperlive_scan_labels l ON s.asset = l.asset AND s.scan_ts = l.scan_ts
        WHERE l.cost_clearing_150s IS NOT NULL AND s.hand_score_v0 IS NOT NULL
          AND (? <= 0 OR (s.day_ntl_vlm IS NOT NULL AND s.day_ntl_vlm >= ?))
    """, (min_volume, min_volume)).fetchall()
    btc_move, recs = {}, []
    for scan_ts, asset, sc_json, ch_json, hand, mover in rows:
        sc, ch = json.loads(sc_json or "{}"), json.loads(ch_json or "{}")
        feat = {
            "dislocation": abs(sc.get("dislocation_bps") or 0.0), "funding": abs(sc.get("funding_bps") or 0.0),
            "premium": abs(sc.get("premium_bps") or 0.0), "move": abs(sc.get("move_pct") or 0.0),
            "oi_change": abs(sc.get("oi_change_pct") or 0.0), "basis_accel": abs(ch.get("basis_accel_bps") or 0.0),
            "oi_accel": abs(ch.get("oi_accel_pct") or 0.0),
        }
        recs.append({"ts": scan_ts, "hand": float(hand), "mover": mover == 1, "feat": feat})
        if asset == "BTC":
            btc_move[scan_ts] = abs(ch.get("mid_return_bps") or 0.0)
    for r in recs:
        r["feat"]["btc_lead"] = btc_move.get(r["ts"], 0.0)
    tss = sorted({r["ts"] for r in recs})
    if len(tss) < 4:
        return {"status": "insufficient", "scan_ts_count": len(tss)}
    cut = tss[int(len(tss) * train_frac)]
    train = [r for r in recs if r["ts"] < cut]
    test = [r for r in recs if r["ts"] >= cut]
    comps = list(train[0]["feat"].keys())
    norms = {}
    for c in comps:
        vals = [r["feat"][c] for r in train]
        lo, hi = min(vals), max(vals)
        norms[c] = (lo, hi if hi > lo else lo + 1.0)

    def nrm(r, c):
        lo, hi = norms[c]
        return (r["feat"][c] - lo) / (hi - lo)

    weights = {}
    for c in comps:
        mv = [nrm(r, c) for r in train if r["mover"]]
        nm = [nrm(r, c) for r in train if not r["mover"]]
        weights[c] = max(0.0, (sum(mv) / len(mv) if mv else 0.0) - (sum(nm) / len(nm) if nm else 0.0))

    variants = {
        "hand": lambda r: r["hand"],
        "accel": lambda r: r["hand"] + 1.5 * min(12.0, r["feat"]["basis_accel"] / 1.0),
        "btc_lead": lambda r: r["hand"] + 2.0 * min(12.0, r["feat"]["btc_lead"] / 1.0),
        "learned": lambda r: sum(weights[c] * nrm(r, c) for c in comps),
    }
    by_ts = {}
    for r in test:
        by_ts.setdefault(r["ts"], []).append(r)
    recall = {}
    for name, fn in variants.items():
        movers = caught = 0
        for rs in by_ts.values():
            tm = [r for r in rs if r["mover"]]
            if not tm:
                continue
            top = {id(r) for r in sorted(rs, key=lambda x: -fn(x))[:top_n]}
            movers += len(tm)
            caught += sum(1 for r in tm if id(r) in top)
        recall[name] = round(caught / movers, 3) if movers else None
    valid = {k: v for k, v in recall.items() if v is not None}
    return {
        "status": "ok" if valid else "insufficient",
        "train_scans": len(set(r["ts"] for r in train)), "test_scans": len(by_ts),
        "test_movers": sum(len([r for r in rs if r["mover"]]) for rs in by_ts.values()),
        "recall_at_12": recall, "learned_weights": {c: round(weights[c], 3) for c in comps},
        "winner": max(valid, key=valid.get) if valid else None,
    }


# Logged score-component field → (opportunity raw-point divisor). MUST mirror universe.N_*
# (in bps/pct units, matching what scan_log records). This is what makes a fitted weight drop
# straight into universe.opportunity(weights=...): we fit in the same raw-point space the live
# score sums over.
_FIT_RAW = {
    "dislocation": ("dislocation_bps", 2.0),     # N_DISLOCATION 0.0002 → 2 bps / pt
    "funding": ("funding_bps", 0.5),             # N_FUNDING 0.00005 → 0.5 bps / pt
    "premium": ("premium_bps", 10.0),            # N_PREMIUM 0.001 → 10 bps / pt
    "move_24h": ("move_pct", 1.0),               # N_MOVE 0.01 → 1% / pt
    "oi_shift": ("oi_change_pct", 2.0),          # N_OI_CHANGE 0.02 → 2% / pt
}
_FIT_CAP_PTS = 12.0
_FIT_TARGET_MAX = 1.5          # scale so the dominant weight == hand-weight max (score stays in-range)
_FIT_VOLUME_W = 0.5            # volume is a liquid-baseline tiebreak, fixed at hand value (not fit)


def _tradeable_floor(min_volume):
    from .universe import MIN_VOLUME_USD
    return MIN_VOLUME_USD if min_volume is None else float(min_volume)


def fit_weights(scan_conn: sqlite3.Connection, *, min_movers: int = 30,
                target_max: float = _FIT_TARGET_MAX, min_volume: float | None = None) -> dict:
    """Fit live universe weights in opportunity's RAW-POINT space from the labeled scan log.
    Fit is restricted to the TRADEABLE pool (day_ntl_vlm >= min_volume, default the live
    volume floor) — the markets selection actually picks from. Fitting over all ~230 logged
    markets would learn tiebreakers from untradeable micro-cap pumps (~80% of logged movers).
    Each component's weight = max(0, mover_mean_pts - nonmover_mean_pts), scaled so the dominant
    signal == hand-weight max. Result drops directly into universe.opportunity(weights=). Read-only;
    the caller persists via universe.save_weights. Selection labels, never PnL."""
    floor = _tradeable_floor(min_volume)
    rows = scan_conn.execute("""
        SELECT s.score_components, l.cost_clearing_150s
        FROM hyperlive_scan_log s
        JOIN hyperlive_scan_labels l ON s.asset = l.asset AND s.scan_ts = l.scan_ts
        WHERE l.cost_clearing_150s IS NOT NULL AND s.score_components IS NOT NULL
          AND s.day_ntl_vlm IS NOT NULL AND s.day_ntl_vlm >= ?
    """, (floor,)).fetchall()
    movers, nonmovers = [], []
    for sc_json, mover in rows:
        try:
            sc = json.loads(sc_json or "{}")
        except (ValueError, TypeError):
            continue
        raw = {comp: min(_FIT_CAP_PTS, abs(float(sc.get(field) or 0.0)) / div)
               for comp, (field, div) in _FIT_RAW.items()}
        (movers if mover == 1 else nonmovers).append(raw)
    if len(movers) < min_movers:
        return {"status": "insufficient", "movers": len(movers), "min_movers": min_movers}

    def _mean(rs, comp):
        return (sum(r[comp] for r in rs) / len(rs)) if rs else 0.0

    sep = {comp: max(0.0, _mean(movers, comp) - _mean(nonmovers, comp)) for comp in _FIT_RAW}
    top = max(sep.values())
    if top <= 0.0:
        # no component separates movers — fall back to the hand baseline rather than a zero score.
        from .universe import HAND_WEIGHTS
        return {"status": "degenerate", "movers": len(movers),
                "weights": dict(HAND_WEIGHTS), "separation": sep}
    weights = {comp: round(target_max * sep[comp] / top, 4) for comp in _FIT_RAW}
    weights["volume"] = _FIT_VOLUME_W
    return {"status": "ok", "movers": len(movers), "nonmovers": len(nonmovers),
            "weights": weights, "separation": {c: round(v, 4) for c, v in sep.items()}}


PROMOTE_MARGIN = 0.01          # learned must beat hand by ≥1pp recall@12 on held-out to promote


def _weighted_score(raw: dict, vol: float, w: dict) -> float:
    s = sum(w.get(c, 0.0) * raw[c] for c in _FIT_RAW)
    return s + w.get("volume", 0.0) * max(0.0, math.log10(max(vol, 1.0)) - 6.0)


def evaluate_weights(scan_conn: sqlite3.Connection, cand_weights: dict, base_weights: dict, *,
                     top_n: int = 12, train_frac: float = 0.6, min_volume: float | None = None) -> dict:
    """Held-out recall@12 for cand vs base weights over the TRADEABLE pool: on test-block
    refreshes, rank each refresh's markets by each weight vector and count cost-clearing movers
    caught in the top-N. This is the promotion gate — fit_weights producing a vector is not
    enough; it must actually beat hand on data it wasn't fit on. Read-only. Selection labels,
    never PnL."""
    floor = _tradeable_floor(min_volume)
    rows = scan_conn.execute("""
        SELECT s.scan_ts, s.asset, s.score_components, s.day_ntl_vlm, l.cost_clearing_150s
        FROM hyperlive_scan_log s
        JOIN hyperlive_scan_labels l ON s.asset = l.asset AND s.scan_ts = l.scan_ts
        WHERE l.cost_clearing_150s IS NOT NULL AND s.score_components IS NOT NULL
          AND s.day_ntl_vlm IS NOT NULL AND s.day_ntl_vlm >= ?
    """, (floor,)).fetchall()
    recs = []
    for scan_ts, asset, sc_json, vol, mover in rows:
        try:
            sc = json.loads(sc_json or "{}")
        except (ValueError, TypeError):
            continue
        raw = {comp: min(_FIT_CAP_PTS, abs(float(sc.get(field) or 0.0)) / div)
               for comp, (field, div) in _FIT_RAW.items()}
        recs.append({"ts": scan_ts, "asset": asset, "raw": raw, "vol": float(vol or 0.0),
                     "mover": mover == 1})
    tss = sorted({r["ts"] for r in recs})
    if len(tss) < 4:
        return {"status": "insufficient", "scan_ts_count": len(tss)}
    cut = tss[int(len(tss) * train_frac)]
    by_ts = {}
    for r in recs:
        if r["ts"] >= cut:
            by_ts.setdefault(r["ts"], []).append(r)

    def _recall(w):
        movers = caught = 0
        for rs in by_ts.values():
            tm = [r for r in rs if r["mover"]]
            if not tm:
                continue
            top = {id(r) for r in sorted(rs, key=lambda x: -_weighted_score(x["raw"], x["vol"], w))[:top_n]}
            movers += len(tm)
            caught += sum(1 for r in tm if id(r) in top)
        return (caught / movers) if movers else None, movers

    cand_recall, movers = _recall(cand_weights)
    base_recall, _ = _recall(base_weights)
    if cand_recall is None or base_recall is None:
        return {"status": "insufficient", "test_movers": movers or 0}
    return {
        "status": "ok", "test_scans": len(by_ts), "test_movers": movers,
        "cand_recall": round(cand_recall, 3), "base_recall": round(base_recall, 3),
        "margin": PROMOTE_MARGIN,
        "promote": bool(cand_recall >= base_recall + PROMOTE_MARGIN),
    }


# Cross-venue (Binance->HL) basis as a SELECTION feature. Pre-registered constants.
BASIS_NORM_BPS = 2.0           # 1 point per 2bps of |basis|
BASIS_CAP_PTS = 12.0
BASIS_WEIGHT = 1.5             # same scale as a strong hand component when combined
CROSS_VENUE_MIN_MOVERS = 40    # don't trust the verdict below this many mapped tradeable movers


def cross_venue_lift(scan_conn: sqlite3.Connection, *, top_n: int = 12, train_frac: float = 0.6,
                     min_volume: float | None = None, min_movers: int = CROSS_VENUE_MIN_MOVERS) -> dict:
    """Held-out test of the lead-lag thesis as a SELECTION signal: on mapped∩tradeable markets,
    does ranking by |Binance->HL basis| (alone, and added to hand) catch more cost-clearing
    movers in the top-N than hand alone? Restricted to rows with a logged basis (so it reads
    'insufficient' until cross-venue data accrues). Read-only. Selection labels, never PnL."""
    floor = _tradeable_floor(min_volume)
    rows = scan_conn.execute("""
        SELECT s.scan_ts, s.asset, s.hand_score_v0, s.binance_basis_bps, l.cost_clearing_150s
        FROM hyperlive_scan_log s
        JOIN hyperlive_scan_labels l ON s.asset = l.asset AND s.scan_ts = l.scan_ts
        WHERE l.cost_clearing_150s IS NOT NULL AND s.hand_score_v0 IS NOT NULL
          AND s.binance_basis_bps IS NOT NULL
          AND s.day_ntl_vlm IS NOT NULL AND s.day_ntl_vlm >= ?
    """, (floor,)).fetchall()
    recs = []
    for scan_ts, asset, hand, basis, mover in rows:
        basis_pts = min(BASIS_CAP_PTS, abs(float(basis)) / BASIS_NORM_BPS)
        recs.append({"ts": scan_ts, "hand": float(hand), "basis_pts": basis_pts, "mover": mover == 1})
    total_movers = sum(1 for r in recs if r["mover"])
    tss = sorted({r["ts"] for r in recs})
    if len(tss) < 4 or total_movers < min_movers:
        return {"status": "insufficient", "scan_ts_count": len(tss), "mapped_movers": total_movers,
                "min_movers": min_movers}
    cut = tss[int(len(tss) * train_frac)]
    by_ts: dict = {}
    for r in recs:
        if r["ts"] >= cut:
            by_ts.setdefault(r["ts"], []).append(r)

    def _recall(key):
        movers = caught = 0
        for rs in by_ts.values():
            tm = [r for r in rs if r["mover"]]
            if not tm:
                continue
            top = {id(r) for r in sorted(rs, key=lambda x: -key(x))[:top_n]}
            movers += len(tm)
            caught += sum(1 for r in tm if id(r) in top)
        return (caught / movers) if movers else None, movers

    hand_recall, hm = _recall(lambda x: x["hand"])
    basis_recall, _ = _recall(lambda x: x["basis_pts"])
    combined_recall, _ = _recall(lambda x: x["hand"] + BASIS_WEIGHT * x["basis_pts"])
    if hand_recall is None:
        return {"status": "insufficient", "test_movers": 0}
    lift_pp = (combined_recall - hand_recall) * 100
    return {
        "status": "ok", "test_scans": len(by_ts), "test_movers": hm,
        "hand_recall": round(hand_recall, 3), "basis_recall": round(basis_recall, 3),
        "combined_recall": round(combined_recall, 3), "lift_pp": round(lift_pp, 2),
        "promote": bool(combined_recall >= hand_recall + PROMOTE_MARGIN),
    }


DIR_FEE_BPS = 9.0              # HL round-trip taker fee (models.ROUND_TRIP_FEE_BPS)
DIR_MIN_N = 30                 # don't trust a directional verdict below this many signals


def basis_direction_edge(scan_conn: sqlite3.Connection, *, horizon_ms: int = 150_000,
                         min_volume: float | None = None, basis_threshold_bps: float = 2.0,
                         tol_ms: int = TOL_MS, fee_bps: float = DIR_FEE_BPS,
                         min_n: int = DIR_MIN_N) -> dict:
    """Directional lead-lag test (the classic use of cross-venue basis): when |basis| clears a
    threshold, does HL's forward move go IN the basis direction (HL chases Binance), and does
    that aligned move clear the round-trip fee? aligned_bps = sign(basis) * forward_move. Edge
    requires both EV-after-fee > 0 AND the mean aligned move significant (t>2) — hit-rate alone
    is not enough (must clear cost). Mapped∩tradeable only; mid-to-mid (spread not modeled, so
    a positive after-fee here is necessary-not-sufficient). Selection/entry labels, never PnL."""
    floor = _tradeable_floor(min_volume)
    rows = scan_conn.execute("""
        SELECT scan_ts, asset, mid_px, binance_basis_bps
        FROM hyperlive_scan_log
        WHERE mid_px IS NOT NULL AND binance_basis_bps IS NOT NULL
          AND day_ntl_vlm IS NOT NULL AND day_ntl_vlm >= ?
        ORDER BY asset, scan_ts
    """, (floor,)).fetchall()
    by_asset: dict = {}
    for scan_ts, asset, mid, basis in rows:
        by_asset.setdefault(asset, []).append((int(scan_ts), float(mid), float(basis)))
    aligned = []
    for ars in by_asset.values():
        ars.sort(key=lambda x: x[0])
        ts = [a[0] for a in ars]
        for i, (t0, mid0, basis) in enumerate(ars):
            if mid0 <= 0 or abs(basis) < basis_threshold_bps:
                continue
            target = t0 + horizon_ms
            best = None
            for j in range(i + 1, len(ars)):
                if ts[j] > target + tol_ms:
                    break
                if abs(ts[j] - target) <= tol_ms and (best is None or abs(ts[j] - target) < abs(ts[best] - target)):
                    best = j
            if best is None:
                continue
            move_bps = (ars[best][1] - mid0) / mid0 * 1e4
            aligned.append((1.0 if basis > 0 else -1.0) * move_bps)
    n = len(aligned)
    horizon_s = horizon_ms // 1000
    if n < min_n:
        return {"status": "insufficient", "n": n, "min_n": min_n, "horizon_s": horizon_s,
                "threshold_bps": basis_threshold_bps}
    mean = sum(aligned) / n
    std = math.sqrt(sum((a - mean) ** 2 for a in aligned) / (n - 1)) if n > 1 else 0.0
    hits = sum(1 for a in aligned if a > 0)
    after_fee = mean - fee_bps
    t_stat = (mean / (std / math.sqrt(n))) if std > 0 else (math.inf if mean > 0 else 0.0)
    z_hit = (hits - 0.5 * n) / math.sqrt(0.25 * n)
    return {
        "status": "ok", "n": n, "horizon_s": horizon_s, "threshold_bps": basis_threshold_bps,
        "hit_rate": round(hits / n, 3), "mean_aligned_bps": round(mean, 2),
        "after_fee_bps": round(after_fee, 2), "fee_bps": fee_bps,
        "t_stat": round(t_stat, 2) if t_stat != math.inf else 999.0, "z_hit": round(z_hit, 2),
        "edge": bool(after_fee > 0 and t_stat > 2.0),
    }


def ensure_labels_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hyperlive_scan_labels (
            asset TEXT NOT NULL, scan_ts INTEGER NOT NULL,
            abs_move_150s_bps REAL, dir_150s INTEGER, cost_clearing_150s INTEGER,
            abs_move_300s_bps REAL, dir_300s INTEGER, cost_clearing_300s INTEGER,
            PRIMARY KEY (asset, scan_ts)
        );
    """)
    conn.commit()


def write_labels(conn: sqlite3.Connection, labels: list[dict]) -> int:
    ensure_labels_schema(conn)
    payload = [(
        r["asset"], r["scan_ts"], r.get("abs_move_150s_bps"), r.get("dir_150s"),
        _b(r.get("cost_clearing_150s")), r.get("abs_move_300s_bps"), r.get("dir_300s"),
        _b(r.get("cost_clearing_300s")),
    ) for r in labels]
    conn.executemany(
        """INSERT INTO hyperlive_scan_labels
           (asset, scan_ts, abs_move_150s_bps, dir_150s, cost_clearing_150s,
            abs_move_300s_bps, dir_300s, cost_clearing_300s)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(asset, scan_ts) DO UPDATE SET
             abs_move_150s_bps=excluded.abs_move_150s_bps, dir_150s=excluded.dir_150s,
             cost_clearing_150s=excluded.cost_clearing_150s,
             abs_move_300s_bps=excluded.abs_move_300s_bps, dir_300s=excluded.dir_300s,
             cost_clearing_300s=excluded.cost_clearing_300s""",
        payload)
    conn.commit()
    return len(payload)


def _b(v):
    return None if v is None else (1 if v else 0)
