#!/usr/bin/env python3
"""What is actually earnable, and what does it risk?

The +209 bps headline is a RESIDUAL: event minus an equal-weight alt basket over the same hours.
Earning a residual requires trading both legs — short the coin AND long ~150 alts for 24h. Nobody
has costed that hedge. Traded naked, you earn the event leg only and carry full alt beta.

This separates the two, then computes the risk envelope the prereg listed as
`required_not_defined`: concurrency, capital, drawdown, ruin distance, and capacity against the
provider's fixed impact probe.

Read-only over the cached corpus.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.research import hl_squeeze_study as S  # noqa: E402
from scripts.research.hl_funding_study import CACHE, enrich, load_funding, summ  # noqa: E402
from scripts.research.hl_funding_signal import add_signals  # noqa: E402
from scripts.research.hl_squeeze_residual import build_price_index, events_with_times  # noqa: E402

CAP_H = 24
FUNDING_CUT = 0.94993          # frozen: skip when funding is top-5% of the coin's own 30d history
IMPACT_BPS = 11.54             # measured p75 two-leg impact, replaces the flat 30 bps
FEES_BPS = 9.0
HOUR_MS = 3_600_000


def deployable_events():
    series = S._load(CACHE)
    split = json.loads(Path("data/research/hl_squeeze_study/split.v1.json").read_text())
    funding = load_funding()
    ev = add_signals(enrich(events_with_times(series, CAP_H), build_price_index(series), funding),
                     funding)
    out = []
    for e in ev:
        if split[e["coin"]] != "HOLDOUT":
            continue
        pct = e.get("funding_self_pct")
        if pct is None or pct > FUNDING_CUT:
            continue
        # re-cost: strip the flat 30 bps slippage, charge measured two-leg impact
        adj = S.COST_BPS - (FEES_BPS + IMPACT_BPS)
        out.append({**e,
                    "naked_bps": e["net_full_bps"] + adj,        # short only, carries alt beta
                    "hedged_bps": e["residual_full_bps"] + adj})  # needs the ~150-leg basket long
    return out


def envelope(events, field):
    """Risk envelope on an equal-clip book: drawdown, tail, and ruin distance."""
    vals = [e[field] for e in sorted(events, key=lambda x: x["entry_ts"])]
    if not vals:
        return {}
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for v in vals:
        equity += v
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    ordered = sorted(vals)
    losers = [v for v in ordered if v < 0]
    tail = ordered[:max(1, len(ordered) // 20)]
    return {
        "n": len(vals), "mean_bps": round(st.fmean(vals), 2),
        "median_bps": round(st.median(vals), 2),
        "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 4),
        "worst_bps": round(ordered[0], 2), "p10_bps": round(ordered[int(0.10 * len(ordered))], 2),
        "expected_shortfall_95_bps": round(st.fmean(tail), 2),
        "mean_loss_bps": round(st.fmean(losers), 2) if losers else None,
        "max_drawdown_bps_per_clip": round(max_dd, 2),
        "total_bps_per_clip": round(sum(vals), 2),
    }


def concurrency(events):
    """Peak simultaneous positions on the frozen one-open-per-coin rule."""
    marks = []
    for e in events:
        marks.append((e["entry_ts"], 1))
        marks.append((e["exit_ts"], -1))
    marks.sort()
    cur = peak = 0
    for _ts, delta in marks:
        cur += delta
        peak = max(peak, cur)
    span_days = (max(e["exit_ts"] for e in events)
                 - min(e["entry_ts"] for e in events)) / 86_400_000
    hours = sum((e["exit_ts"] - e["entry_ts"]) / HOUR_MS for e in events)
    return {"peak_concurrent": peak,
            "avg_concurrent": round(hours / (span_days * 24), 2) if span_days else None,
            "span_days": round(span_days, 1),
            "events_per_day": round(len(events) / span_days, 3) if span_days else None}


def main() -> int:
    ev = deployable_events()
    naked, hedged = envelope(ev, "naked_bps"), envelope(ev, "hedged_bps")
    conc = concurrency(ev)
    nb, _lo, _hi = S.coin_block_bootstrap(
        [{**e, "net_bps": e["naked_bps"]} for e in ev])
    naked_ci = summ([{**e, "x": e["naked_bps"]} for e in ev], "naked_bps", "naked")
    hedged_ci = summ([{**e, "x": e["hedged_bps"]} for e in ev], "hedged_bps", "hedged")

    report = {"rule": {"cap_h": CAP_H, "funding_self_pct_max": FUNDING_CUT,
                       "fees_bps": FEES_BPS, "impact_bps": IMPACT_BPS,
                       "total_cost_bps": FEES_BPS + IMPACT_BPS},
              "naked_short": {**naked, "coin_block_ci": [naked_ci["ci_low"], naked_ci["ci_high"]]},
              "hedged_residual": {**hedged,
                                  "coin_block_ci": [hedged_ci["ci_low"], hedged_ci["ci_high"]],
                                  "requires": "long ~150-leg equal-weight alt basket, uncosted"},
              "book": conc}
    for clip in (5_000, 20_000, 50_000):
        gross = naked["mean_bps"] / 1e4 * clip
        per_day = gross * (conc["events_per_day"] or 0)
        capital = clip * (conc["avg_concurrent"] or 0)
        report.setdefault("sizing", {})[f"clip_{clip}"] = {
            "usd_per_event": round(gross, 2),
            "usd_per_day": round(per_day, 2),
            "capital_at_avg_concurrency": round(capital, 2),
            "daily_return_on_deployed_pct": (round(100 * per_day / capital, 3)
                                             if capital else None),
            "max_drawdown_usd": round(naked["max_drawdown_bps_per_clip"] / 1e4 * clip, 2),
        }
    Path("data/research/hl_squeeze_study/deployable.v1.json").write_text(
        json.dumps(report, indent=1, sort_keys=True))
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
