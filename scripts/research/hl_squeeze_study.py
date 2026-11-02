#!/usr/bin/env python3
"""Squeeze-vs-exhaustion study on the HL out-of-sample corpus.

Executes data/research/hl_squeeze_study/PREREG.v1.md verbatim. The coin-level split is sealed to
split.v1.json before any outcome is computed; exactly one discriminator is carried from DISCOVERY
to HOLDOUT and it is never re-selected there.

Read-only over the cached corpus. Writes only this study's own result files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics as st
from pathlib import Path

MAJORS = {"BTC", "ETH", "SOL"}
RUNUP_N = 72
RUNUP_X = 0.30
TP_MULT = 0.85          # short target
SL_MULT = 1.20          # short stop
CAP_H = 8
COST_BPS = 39.0
MIN_HISTORY = 240
BOOT_REPS = 5000
BOOT_SEED = 20260727
MIN_DISCOVERY_N = 100

FEATURES = ("runup_72h", "runup_velocity", "accel_ratio", "volume_surge", "volume_trend",
            "range_expansion", "pullback_depth", "higher_low_intact", "consecutive_up_bars",
            "dist_from_high")


def _load(cache: Path):
    manifest = json.loads((cache / "manifest.json").read_text())
    out = {}
    for coin, info in manifest["coins"].items():
        if coin in MAJORS or not info.get("n"):
            continue
        path = cache / "candles" / f"candles_{coin}.json"
        if not path.exists():
            continue
        rows = json.loads(path.read_text())
        rows.sort(key=lambda r: r["t"])
        series = {
            "t": [int(r["t"]) for r in rows],
            "o": [float(r["o"]) for r in rows], "h": [float(r["h"]) for r in rows],
            "l": [float(r["l"]) for r in rows], "c": [float(r["c"]) for r in rows],
            "v": [float(r.get("v") or 0.0) for r in rows],
            "is_delisted": bool(info["is_delisted"]),
        }
        if len(rows) >= MIN_HISTORY:
            out[coin] = series
    return out


def _safe_div(a, b):
    return a / b if b not in (0, 0.0) and b == b else None


def _features(s, t):
    c, h, l, v, o = s["c"], s["h"], s["l"], s["v"], s["o"]
    window = c[t - RUNUP_N:t]
    trough = min(window)
    runup = c[t] / trough - 1.0
    hours_since_trough = RUNUP_N - window.index(trough)
    ret24 = _safe_div(c[t], c[t - 24])
    ret72 = _safe_div(c[t], c[t - RUNUP_N])
    ret24 = (ret24 - 1.0) if ret24 else None
    ret72 = (ret72 - 1.0) if ret72 else None
    med_v = st.median(v[t - 24:t]) or 0.0
    med_range = st.median([(h[i] - l[i]) / c[i] for i in range(t - 24, t) if c[i]]) or 0.0
    high24 = max(h[t - 24:t + 1])
    up = 0
    for i in range(t, max(t - 48, 0), -1):
        if c[i] > o[i]:
            up += 1
        else:
            break
    return {
        "runup_72h": runup,
        "runup_velocity": _safe_div(runup, hours_since_trough),
        "accel_ratio": _safe_div(ret24, ret72) if (ret24 is not None and ret72) else None,
        "volume_surge": _safe_div(v[t], med_v),
        "volume_trend": _safe_div(sum(v[t - 5:t + 1]), sum(v[t - 23:t + 1])),
        "range_expansion": _safe_div((h[t] - l[t]) / c[t] if c[t] else None, med_range)
        if c[t] and med_range else None,
        "pullback_depth": _safe_div(high24 - c[t], high24),
        "higher_low_intact": 1.0 if min(l[t - 5:t + 1]) > min(l[t - 17:t - 5]) else 0.0,
        "consecutive_up_bars": float(up),
        "dist_from_high": _safe_div(c[t], max(c[:t + 1])),
    }


def build_events(series_by_coin):
    """Frozen nomination + trade. One open event per coin at a time; next-bar-open entry."""
    events = []
    for coin, s in sorted(series_by_coin.items()):
        c, h, l, o = s["c"], s["h"], s["l"], s["o"]
        n = len(c)
        blocked_until = 0
        prior_runup = None
        for t in range(RUNUP_N + 24, n - CAP_H - 1):
            trough = min(c[t - RUNUP_N:t])
            runup = c[t] / trough - 1.0 if trough > 0 else None
            crossed = (runup is not None and prior_runup is not None
                       and runup >= RUNUP_X > prior_runup)
            prior_runup = runup
            if not crossed or t < blocked_until:
                continue
            entry = o[t + 1]
            if not entry or entry <= 0:
                continue
            stop, target = entry * SL_MULT, entry * TP_MULT
            exit_px, exit_reason, exit_i = None, None, None
            for i in range(t + 1, min(t + 1 + CAP_H, n)):
                if h[i] >= stop:                      # SL assumed first within a bar
                    exit_px, exit_reason, exit_i = stop, "squeeze", i
                    break
                if l[i] <= target:
                    exit_px, exit_reason, exit_i = target, "exhaustion", i
                    break
            if exit_px is None:
                exit_i = min(t + CAP_H, n - 1)
                exit_px, exit_reason = c[exit_i], "chop"
            net = (entry - exit_px) / entry * 1e4 - COST_BPS
            events.append({
                "coin": coin, "is_delisted": s["is_delisted"], "t_ms": s["t"][t],
                "entry": entry, "exit": exit_px, "exit_reason": exit_reason,
                "held_h": exit_i - t, "net_bps": net, **_features(s, t),
            })
            blocked_until = exit_i + 1
    return events


def seal_split(coins, path: Path):
    split = {coin: ("DISCOVERY" if int(hashlib.sha256(coin.encode()).hexdigest(), 16) % 2 == 0
                    else "HOLDOUT") for coin in sorted(coins)}
    path.write_text(json.dumps(split, indent=1, sort_keys=True))
    return split


def coin_block_bootstrap(events, reps=BOOT_REPS, seed=BOOT_SEED):
    """Resample COINS with replacement — events within a coin are correlated."""
    if not events:
        return None, None, None
    by_coin = {}
    for e in events:
        by_coin.setdefault(e["coin"], []).append(e["net_bps"])
    coins = list(by_coin)
    point = st.fmean([v for e in coins for v in by_coin[e]])
    rng = random.Random(seed)
    means = []
    for _ in range(reps):
        pool = []
        for _ in range(len(coins)):
            pool.extend(by_coin[coins[rng.randrange(len(coins))]])
        if pool:
            means.append(st.fmean(pool))
    means.sort()
    return point, means[int(0.025 * len(means))], means[int(0.975 * len(means))]


def summarize(events, label):
    if not events:
        return {"label": label, "n": 0}
    point, lo, hi = coin_block_bootstrap(events)
    reasons = {}
    for e in events:
        reasons[e["exit_reason"]] = reasons.get(e["exit_reason"], 0) + 1
    nets = sorted(e["net_bps"] for e in events)
    return {
        "label": label, "n": len(events), "coins": len({e["coin"] for e in events}),
        "mean_bps": round(point, 2), "ci_low": round(lo, 2), "ci_high": round(hi, 2),
        "median_bps": round(nets[len(nets) // 2], 2),
        "win_rate": round(sum(1 for x in nets if x > 0) / len(nets), 4),
        "worst_bps": round(nets[0], 2),
        "p10_bps": round(nets[int(0.10 * len(nets))], 2),
        "squeeze_rate": round(reasons.get("squeeze", 0) / len(events), 4),
        "exhaustion_rate": round(reasons.get("exhaustion", 0) / len(events), 4),
        "chop_rate": round(reasons.get("chop", 0) / len(events), 4),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default="data/research/hl_full_history")
    parser.add_argument("--out-dir", default="data/research/hl_squeeze_study")
    args = parser.parse_args(argv)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    series = _load(Path(args.cache_dir))
    split = seal_split(series.keys(), out / "split.v1.json")     # sealed BEFORE any outcome
    events = build_events(series)
    for e in events:
        e["split"] = split[e["coin"]]

    report = {"corpus": {"coins": len(series), "events": len(events),
                         "delisted_coins": sum(1 for s in series.values() if s["is_delisted"])}}
    report["base"] = {
        "all": summarize(events, "base/all"),
        "delisted": summarize([e for e in events if e["is_delisted"]], "base/delisted_OOS"),
        "live": summarize([e for e in events if not e["is_delisted"]], "base/live"),
        "discovery": summarize([e for e in events if e["split"] == "DISCOVERY"], "base/DISCOVERY"),
        "holdout": summarize([e for e in events if e["split"] == "HOLDOUT"], "base/HOLDOUT"),
    }

    # --- DISCOVERY-only selection: one rule, highest mean net_bps subject to n >= 100 ---
    discovery = [e for e in events if e["split"] == "DISCOVERY"]
    candidates = []
    for feature in FEATURES:
        vals = sorted(e[feature] for e in discovery if e.get(feature) is not None)
        if len(vals) < MIN_DISCOVERY_N * 2:
            continue
        for q in (0.25, 1 / 3, 0.5, 2 / 3, 0.75):
            cut = vals[int(q * (len(vals) - 1))]
            for side in ("<=", ">"):
                subset = [e for e in discovery if e.get(feature) is not None
                          and (e[feature] <= cut if side == "<=" else e[feature] > cut)]
                if len(subset) < MIN_DISCOVERY_N:
                    continue
                candidates.append({
                    "feature": feature, "side": side, "cut": cut, "q": round(q, 4),
                    "n": len(subset), "mean_bps": st.fmean(e["net_bps"] for e in subset),
                })
    candidates.sort(key=lambda c: -c["mean_bps"])
    report["discovery_candidates_top10"] = [
        {**c, "mean_bps": round(c["mean_bps"], 2), "cut": round(c["cut"], 6)}
        for c in candidates[:10]]
    selected = candidates[0] if candidates else None

    if selected:
        def passes(e):
            v = e.get(selected["feature"])
            if v is None:
                return False
            return v <= selected["cut"] if selected["side"] == "<=" else v > selected["cut"]

        holdout = [e for e in events if e["split"] == "HOLDOUT"]
        hold_sel = [e for e in holdout if passes(e)]
        base_hold = summarize(holdout, "holdout/base")
        sel_hold = summarize(hold_sel, "holdout/selected")
        improvement = ((sel_hold.get("mean_bps") or 0) - (base_hold.get("mean_bps") or 0))
        # concentration: does one coin carry the improvement?
        by_coin = {}
        for e in hold_sel:
            by_coin.setdefault(e["coin"], []).append(e["net_bps"])
        total = sum(sum(v) for v in by_coin.values())
        top_share = (max((sum(v) for v in by_coin.values()), default=0) / total
                     if total else None)
        # 2x slippage stress: an extra 30bps round trip
        stressed = [{**e, "net_bps": e["net_bps"] - 30.0} for e in hold_sel]
        report["selected_rule"] = {
            **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in selected.items()},
            "holdout_base": base_hold, "holdout_selected": sel_hold,
            "holdout_improvement_bps": round(improvement, 2),
            "top_coin_share_of_selected_pnl": (round(top_share, 4)
                                               if top_share is not None else None),
            "holdout_selected_2x_slippage": summarize(stressed, "holdout/selected_2x_cost"),
        }

    (out / "report.v1.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    with (out / "events.v1.json").open("w") as fh:
        json.dump(events, fh)
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def sensitivity(cache_dir="data/research/hl_full_history",
                out_dir="data/research/hl_squeeze_study"):
    """PREREG §3 sensitivity: 24h and 96h caps, and 2x slippage. Base rule only — the
    discriminator's single holdout look is already spent and is not repeated here."""
    global CAP_H
    series = _load(Path(cache_dir))
    split = json.loads((Path(out_dir) / "split.v1.json").read_text())
    original = CAP_H
    rows = {}
    for cap in (8, 24, 96):
        CAP_H = cap
        events = build_events(series)
        for e in events:
            e["split"] = split[e["coin"]]
        rows[f"cap_{cap}h"] = {
            "all": summarize(events, f"cap{cap}/all"),
            "delisted_OOS": summarize([e for e in events if e["is_delisted"]], f"cap{cap}/delisted"),
            "live": summarize([e for e in events if not e["is_delisted"]], f"cap{cap}/live"),
            "holdout": summarize([e for e in events if e["split"] == "HOLDOUT"], f"cap{cap}/holdout"),
            "2x_slippage_all": summarize(
                [{**e, "net_bps": e["net_bps"] - 30.0} for e in events], f"cap{cap}/2x_all"),
        }
    CAP_H = original
    (Path(out_dir) / "sensitivity.v1.json").write_text(json.dumps(rows, indent=1, sort_keys=True))
    return rows
