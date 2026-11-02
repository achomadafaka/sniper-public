#!/usr/bin/env python3
"""PREREG.v2 Claim B: is funding AT NOMINATION the discriminator OHLCV could not be?

Features are strictly causal — trailing funding up to and including the nomination hour, never the
funding realized over the hold. Selected on DISCOVERY only, one look at HOLDOUT.

The v1 holdout has already been read once, so any result here is DISCOVERY-grade by construction
and cannot be reported as confirmed. Its real test is the forward tape.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.research import hl_squeeze_study as S  # noqa: E402
from scripts.research.hl_funding_study import CACHE, enrich, load_funding, summ  # noqa: E402
from scripts.research.hl_squeeze_residual import build_price_index, events_with_times  # noqa: E402

HOUR_MS = 3_600_000
MIN_N = 80


def _trailing(rates, end_ms, hours):
    vals = [rates[b] for b in range(end_ms - hours * HOUR_MS, end_ms, HOUR_MS) if b in rates]
    return st.fmean(vals) * 1e4 if vals else None


def add_signals(events, funding_by_coin):
    """funding_24h_bps, funding_self_pct, funding_vs_basket_bps — all as of the nomination hour."""
    # cross-sectional trailing-24h funding per hour, built once
    hourly = {}
    for coin, rates in funding_by_coin.items():
        for bucket, rate in rates.items():
            hourly.setdefault(bucket, []).append(rate)
    median_by_hour = {b: st.median(v) * 1e4 for b, v in hourly.items() if v}

    out = []
    for e in events:
        rates = funding_by_coin.get(e["coin"])
        if not rates:
            continue
        nom = e["entry_ts"] - HOUR_MS                      # nomination bar precedes entry
        f24 = _trailing(rates, nom, 24)
        if f24 is None:
            continue
        hist = [rates[b] for b in range(nom - 720 * HOUR_MS, nom, HOUR_MS) if b in rates]
        self_pct = (sum(1 for v in hist if v * 1e4 <= f24) / len(hist)) if len(hist) >= 240 else None
        cross = median_by_hour.get(nom // HOUR_MS * HOUR_MS)
        out.append({**e, "funding_24h_bps": f24, "funding_self_pct": self_pct,
                    "funding_vs_basket_bps": (f24 - cross) if cross is not None else None})
    return out


def main() -> int:
    series = S._load(CACHE)
    split = json.loads(Path("data/research/hl_squeeze_study/split.v1.json").read_text())
    index = build_price_index(series)
    funding = load_funding()
    report = {}
    for cap in (24, 96):
        ev = add_signals(enrich(events_with_times(series, cap), index, funding), funding)
        for e in ev:
            e["split"] = split[e["coin"]]
        disc = [e for e in ev if e["split"] == "DISCOVERY"]
        hold = [e for e in ev if e["split"] == "HOLDOUT"]
        base_hold = summ(hold, "residual_full_bps", f"cap{cap}/holdout_base")

        cands = []
        for feat in ("funding_24h_bps", "funding_self_pct", "funding_vs_basket_bps"):
            vals = sorted(e[feat] for e in disc if e.get(feat) is not None)
            if len(vals) < MIN_N * 2:
                continue
            for q in (0.25, 1 / 3, 0.5, 2 / 3, 0.75):
                cut = vals[int(q * (len(vals) - 1))]
                for side in ("<=", ">"):
                    sub = [e for e in disc if e.get(feat) is not None
                           and (e[feat] <= cut if side == "<=" else e[feat] > cut)]
                    if len(sub) < MIN_N:
                        continue
                    cands.append({"feature": feat, "side": side, "cut": cut, "q": round(q, 3),
                                  "n": len(sub),
                                  "mean_bps": st.fmean(e["residual_full_bps"] for e in sub)})
        cands.sort(key=lambda c: -c["mean_bps"])
        sel = cands[0] if cands else None
        block = {"discovery_top5": [{**c, "mean_bps": round(c["mean_bps"], 2),
                                     "cut": round(c["cut"], 5)} for c in cands[:5]],
                 "holdout_base": base_hold}
        if sel:
            def ok(e):
                v = e.get(sel["feature"])
                return v is not None and (v <= sel["cut"] if sel["side"] == "<=" else v > sel["cut"])

            hsel = [e for e in hold if ok(e)]
            sel_summ = summ(hsel, "residual_full_bps", f"cap{cap}/holdout_selected")
            stressed = [{**e, "residual_full_bps": e["residual_full_bps"] - 30.0} for e in hsel]
            by_coin = {}
            for e in hsel:
                by_coin.setdefault(e["coin"], []).append(e["residual_full_bps"])
            tot = sum(sum(v) for v in by_coin.values())
            block["selected"] = {
                **{k: (round(v, 5) if isinstance(v, float) else v) for k, v in sel.items()},
                "holdout_selected": sel_summ,
                "holdout_improvement_bps": round(
                    (sel_summ.get("mean_bps") or 0) - (base_hold.get("mean_bps") or 0), 2),
                "top_coin_share": (round(max((sum(v) for v in by_coin.values()), default=0) / tot, 4)
                                   if tot else None),
                "holdout_2x_slippage": summ(stressed, "residual_full_bps", f"cap{cap}/sel_2x"),
            }
        report[f"cap_{cap}h"] = block

    Path("data/research/hl_squeeze_study/funding_signal.v1.json").write_text(
        json.dumps(report, indent=1, sort_keys=True))
    for cap, b in report.items():
        print(f"\n===== {cap} =====")
        print(" DISCOVERY top 5:")
        for c in b["discovery_top5"]:
            print(f"   {c['feature']:24s} {c['side']:2s} {c['cut']:>10.4f} (q={c['q']:.2f}) "
                  f"n={c['n']:4d} mean={c['mean_bps']:+8.2f}")
        hb = b["holdout_base"]
        print(f" HOLDOUT base    : n={hb['n']:4d} mean={hb['mean_bps']:+8.2f} "
              f"CI[{hb['ci_low']:+.2f},{hb['ci_high']:+.2f}]")
        s = b.get("selected")
        if s:
            hs = s["holdout_selected"]
            print(f" SELECTED {s['feature']} {s['side']} {s['cut']:.4f}")
            print(f" HOLDOUT selected: n={hs['n']:4d} mean={hs['mean_bps']:+8.2f} "
                  f"CI[{hs['ci_low']:+.2f},{hs['ci_high']:+.2f}] win={hs['win_rate']:.3f}")
            print(f" improvement={s['holdout_improvement_bps']:+.2f} bps  "
                  f"top_coin_share={s['top_coin_share']}  "
                  f"2x_cost_mean={s['holdout_2x_slippage']['mean_bps']:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
