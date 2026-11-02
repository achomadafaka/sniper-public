#!/usr/bin/env python3
"""Leave-one-out alt-basket residual for the HL run-up short.

The decisive control: over each event's EXACT holding interval, short every other alt with data
and take the equal-weight mean. `residual = event_gross - basket_gross`. If the raw edge is only
alts drifting down over the sample, the residual collapses to zero. This can only weaken or
confirm the raw result — it cannot manufacture one.

Same frozen rule, costs, and coin-block bootstrap as hl_squeeze_study.py.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.research import hl_squeeze_study as S  # noqa: E402


def build_price_index(series_by_coin):
    """coin -> {ts_ms: close}, for exact-interval benchmark lookup."""
    return {coin: dict(zip(s["t"], s["c"])) for coin, s in series_by_coin.items()}


def events_with_times(series_by_coin, cap_h):
    """Re-run the frozen rule, retaining entry/exit timestamps for interval matching."""
    S.CAP_H = cap_h
    events = []
    for coin, s in sorted(series_by_coin.items()):
        c, h, l, o, ts = s["c"], s["h"], s["l"], s["o"], s["t"]
        n = len(c)
        blocked_until, prior = 0, None
        for t in range(S.RUNUP_N + 24, n - cap_h - 1):
            trough = min(c[t - S.RUNUP_N:t])
            runup = c[t] / trough - 1.0 if trough > 0 else None
            crossed = (runup is not None and prior is not None
                       and runup >= S.RUNUP_X > prior)
            prior = runup
            if not crossed or t < blocked_until:
                continue
            entry = o[t + 1]
            if not entry or entry <= 0:
                continue
            stop, target = entry * S.SL_MULT, entry * S.TP_MULT
            exit_px = exit_i = None
            for i in range(t + 1, min(t + 1 + cap_h, n)):
                if h[i] >= stop:
                    exit_px, exit_i = stop, i
                    break
                if l[i] <= target:
                    exit_px, exit_i = target, i
                    break
            if exit_px is None:
                exit_i = min(t + cap_h, n - 1)
                exit_px = c[exit_i]
            events.append({
                "coin": coin, "is_delisted": s["is_delisted"],
                "entry_ts": ts[t + 1], "exit_ts": ts[exit_i],
                "gross_bps": (entry - exit_px) / entry * 1e4,
            })
            blocked_until = exit_i + 1
    return events


def add_residuals(events, price_index, *, min_basket=20):
    """Attach the exact-interval leave-one-out equal-weight alt-basket short return."""
    out = []
    for e in events:
        legs = []
        for coin, prices in price_index.items():
            if coin == e["coin"]:
                continue
            a, b = prices.get(e["entry_ts"]), prices.get(e["exit_ts"])
            if a and b and a > 0:
                legs.append((a - b) / a * 1e4)
        if len(legs) < min_basket:
            continue
        basket = st.fmean(legs)
        out.append({**e, "basket_n": len(legs), "basket_bps": basket,
                    "residual_bps": e["gross_bps"] - basket,
                    "net_bps": e["gross_bps"] - S.COST_BPS,
                    "net_residual_bps": e["gross_bps"] - basket - S.COST_BPS})
    return out


def _summ(events, field, label):
    if not events:
        return {"label": label, "n": 0}
    scored = [{**e, "net_bps": e[field], "exit_reason": "n/a"} for e in events]
    s = S.summarize(scored, label)
    return {k: s[k] for k in ("label", "n", "coins", "mean_bps", "ci_low", "ci_high",
                              "median_bps", "win_rate", "p10_bps")}


def main() -> int:
    series = S._load(Path("data/research/hl_full_history"))
    split = json.loads(Path("data/research/hl_squeeze_study/split.v1.json").read_text())
    index = build_price_index(series)
    report = {}
    for cap in (8, 24, 96):
        ev = add_residuals(events_with_times(series, cap), index)
        for e in ev:
            e["split"] = split[e["coin"]]
        delisted = [e for e in ev if e["is_delisted"]]
        holdout = [e for e in ev if e["split"] == "HOLDOUT"]
        report[f"cap_{cap}h"] = {
            "net_all": _summ(ev, "net_bps", f"cap{cap}/net_all"),
            "basket_all": _summ(ev, "basket_bps", f"cap{cap}/basket_short_all"),
            "residual_all": _summ(ev, "net_residual_bps", f"cap{cap}/net_residual_all"),
            "residual_delisted_OOS": _summ(delisted, "net_residual_bps",
                                           f"cap{cap}/net_residual_delisted"),
            "residual_holdout": _summ(holdout, "net_residual_bps",
                                      f"cap{cap}/net_residual_holdout"),
        }
    Path("data/research/hl_squeeze_study/residual.v1.json").write_text(
        json.dumps(report, indent=1, sort_keys=True))
    hdr = (f"{'cap':>5} {'slice':22} {'n':>5} {'coins':>5} {'mean':>9} "
           f"{'ci_low':>9} {'ci_high':>9} {'median':>9} {'win':>6}")
    print(hdr)
    print("-" * len(hdr))
    for cap, blocks in report.items():
        for name, s in blocks.items():
            if not s.get("n"):
                continue
            print(f"{cap:>5} {name:22} {s['n']:5d} {s['coins']:5d} {s['mean_bps']:+9.2f} "
                  f"{s['ci_low']:+9.2f} {s['ci_high']:+9.2f} {s['median_bps']:+9.1f} "
                  f"{s['win_rate']:6.3f}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
