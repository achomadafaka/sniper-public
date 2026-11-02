#!/usr/bin/env python3
"""Funding-complete PnL for the HL run-up short, and funding-as-signal.

Two separate claims, kept apart on purpose:

A. CORRECTION (not a discovery). v1 excluded funding from both the event leg and the alt-basket
   leg. Long pays positive funding; a SHORT RECEIVES it. Alt funding is persistently positive, so
   the v1 numbers were incomplete, not conservative. This recomputes both legs with exact hourly
   funding over each event's actual holding interval. Nothing is selected here.

   The decisive quantity is the funding DIFFERENTIAL: a run-up coin's funding minus the
   equal-weight alt basket's funding over the same hours. Only that survives the basket control.
   If every alt pays the same, funding cancels and adds nothing.

B. DISCOVERY (needs its own prereg — see PREREG.v2.md). Funding at nomination was NOT in the v1
   feature family, which is why the v1 OHLCV discriminator had nothing to work with: crowded-long
   positioning is not a price or volume fact.

Read-only over the cached corpus.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.research import hl_squeeze_study as S  # noqa: E402
from scripts.research.hl_squeeze_residual import build_price_index, events_with_times  # noqa: E402

HOUR_MS = 3_600_000
CACHE = Path("data/research/hl_full_history")


def load_funding():
    out = {}
    for path in (CACHE / "funding").glob("funding_*.json"):
        coin = path.stem.removeprefix("funding_")          # provider casing verbatim
        try:
            out[coin] = {int(k): float(v) for k, v in json.loads(path.read_text()).items()}
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return out


def funding_bps(rates, start_ms, end_ms):
    """Hourly funding a SHORT receives over [start, end). Positive rate -> short is paid.
    Returns (bps, hours_covered, hours_expected) so missing coverage is visible, never zeroed."""
    if end_ms <= start_ms:
        return 0.0, 0, 0
    expected = int((end_ms - start_ms) // HOUR_MS)
    total = 0.0
    covered = 0
    bucket = start_ms // HOUR_MS * HOUR_MS
    while bucket < end_ms:
        rate = rates.get(bucket)
        if rate is not None:
            total += rate
            covered += 1
        bucket += HOUR_MS
    return total * 1e4, covered, expected


def enrich(events, price_index, funding_by_coin, *, min_basket=20, min_coverage=0.8):
    """Attach exact-interval basket price AND basket funding, then the differential."""
    out = []
    for e in events:
        rates = funding_by_coin.get(e["coin"])
        if not rates:
            continue
        own_fund, covered, expected = funding_bps(rates, e["entry_ts"], e["exit_ts"])
        if expected and covered / expected < min_coverage:
            continue                                    # refuse a partly-unfunded event
        legs_px, legs_fund = [], []
        for coin, prices in price_index.items():
            if coin == e["coin"]:
                continue
            a, b = prices.get(e["entry_ts"]), prices.get(e["exit_ts"])
            if not (a and b and a > 0):
                continue
            legs_px.append((a - b) / a * 1e4)
            other = funding_by_coin.get(coin)
            if other:
                bps, cov, exp = funding_bps(other, e["entry_ts"], e["exit_ts"])
                if not exp or cov / exp >= min_coverage:
                    legs_fund.append(bps)
        if len(legs_px) < min_basket or len(legs_fund) < min_basket:
            continue
        basket_px = st.fmean(legs_px)
        basket_fund = st.fmean(legs_fund)
        out.append({
            **e,
            "own_funding_bps": own_fund,
            "basket_px_bps": basket_px,
            "basket_funding_bps": basket_fund,
            "funding_differential_bps": own_fund - basket_fund,
            # price-only residual (what v1 reported)
            "residual_px_bps": e["gross_bps"] - basket_px - S.COST_BPS,
            # funding-complete: both legs carry their own funding
            "residual_full_bps": ((e["gross_bps"] + own_fund)
                                  - (basket_px + basket_fund) - S.COST_BPS),
            "net_full_bps": e["gross_bps"] + own_fund - S.COST_BPS,
        })
    return out


def summ(events, field, label):
    if not events:
        return {"label": label, "n": 0}
    scored = [{**e, "net_bps": e[field], "exit_reason": "n/a"} for e in events]
    s = S.summarize(scored, label)
    return {k: s[k] for k in ("label", "n", "coins", "mean_bps", "ci_low", "ci_high",
                              "median_bps", "win_rate")}


def main() -> int:
    series = S._load(CACHE)
    split = json.loads(Path("data/research/hl_squeeze_study/split.v1.json").read_text())
    index = build_price_index(series)
    funding = load_funding()
    print(f"funding loaded for {len(funding)} coins\n")
    report = {}
    for cap in (8, 24, 96):
        ev = enrich(events_with_times(series, cap), index, funding)
        for e in ev:
            e["split"] = split[e["coin"]]
        hold = [e for e in ev if e["split"] == "HOLDOUT"]
        report[f"cap_{cap}h"] = {
            "A1_price_residual": summ(ev, "residual_px_bps", f"cap{cap}/residual_price_only"),
            "A2_own_funding": summ(ev, "own_funding_bps", f"cap{cap}/own_funding"),
            "A3_basket_funding": summ(ev, "basket_funding_bps", f"cap{cap}/basket_funding"),
            "A4_funding_differential": summ(ev, "funding_differential_bps",
                                            f"cap{cap}/funding_DIFFERENTIAL"),
            "A5_residual_FULL": summ(ev, "residual_full_bps", f"cap{cap}/residual_FUNDING_COMPLETE"),
            "A6_residual_full_holdout": summ(hold, "residual_full_bps",
                                             f"cap{cap}/residual_FULL_holdout"),
            "A7_net_full_no_benchmark": summ(ev, "net_full_bps", f"cap{cap}/net_with_funding"),
        }
    Path("data/research/hl_squeeze_study/funding.v1.json").write_text(
        json.dumps(report, indent=1, sort_keys=True))
    hdr = (f"{'cap':>5} {'measure':32} {'n':>5} {'coins':>5} {'mean':>9} "
           f"{'ci_low':>9} {'ci_high':>9} {'median':>9} {'win':>6}")
    print(hdr)
    print("-" * len(hdr))
    for cap, blocks in report.items():
        for name, s in blocks.items():
            if not s.get("n"):
                continue
            print(f"{cap:>5} {name:32} {s['n']:5d} {s['coins']:5d} {s['mean_bps']:+9.2f} "
                  f"{s['ci_low']:+9.2f} {s['ci_high']:+9.2f} {s['median_bps']:+9.1f} "
                  f"{s['win_rate']:6.3f}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
