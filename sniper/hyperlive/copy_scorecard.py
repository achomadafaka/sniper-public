"""Honest scorecard math for the paper copy book — pure, injectable, deterministic.

Turns raw hl_copy_book rows into the numbers that survive scrutiny:
  * bootstrap CIs        — is the edge distinguishable from zero, given fat tails?
  * robust central tend. — winsorized/trimmed mean + median next to the mean
  * concentration        — does one trade / one wallet carry the whole result?
  * beta-neutral alpha   — skill vs riding the market
  * size-decay           — deployable PnL at real notional, from captured L2 depth
  * total-book MTM        — realized + unrealized, defeats target-close censoring

No I/O. The CLI feeds rows + marks + a market series; every function here is a pure transform
so the fidelity math is unit-tested without touching Hyperliquid or the clock."""
from __future__ import annotations

import json
import random
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone

from .fills import capacity_with_provenance, max_size_within_slippage, round_trip_slippage_bps


def _clean(xs) -> list[float]:
    return [float(x) for x in xs if x is not None]


def bootstrap_ci(xs, *, iters: int = 20000, alpha: float = 0.05, seed: int = 17):
    """Percentile bootstrap CI of the MEAN. Seeded -> deterministic (tests + reproducible runs)."""
    xs = _clean(xs)
    if len(xs) < 3:
        return (None, None)
    rnd = random.Random(seed)
    n = len(xs)
    means = []
    for _ in range(iters):
        acc = 0.0
        for _ in range(n):
            acc += xs[rnd.randrange(n)]
        means.append(acc / n)
    means.sort()
    lo = means[int((alpha / 2) * iters)]
    hi = means[int((1 - alpha / 2) * iters)]
    return (round(lo, 3), round(hi, 3))


def cluster_bootstrap_ci(xs, groups, *, iters: int = 20000, alpha: float = 0.05,
                         seed: int = 17):
    """Percentile CI of the row mean while resampling whole dependent clusters."""
    if len(xs) != len(groups):
        raise ValueError("xs and groups must align")
    buckets = defaultdict(list)
    for value, group in zip(xs, groups):
        if value is not None:
            buckets[str(group)].append(float(value))
    keys = list(buckets)
    if len(keys) < 2:
        return (None, None)
    rnd = random.Random(seed)
    means = []
    for _ in range(iters):
        sample = []
        for _ in keys:
            sample.extend(buckets[keys[rnd.randrange(len(keys))]])
        means.append(sum(sample) / len(sample))
    means.sort()
    return (round(means[int((alpha / 2) * iters)], 3),
            round(means[int((1 - alpha / 2) * iters)], 3))


def episode_stats(rows) -> dict:
    """Aggregate coin x UTC-entry-day episodes before inference."""
    episodes = defaultdict(list)
    for row in rows:
        value = row.get("pnl_bps")
        ts_ms = row.get("detect_ts_ms")
        if value is None or ts_ms is None:
            continue
        day = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).date().isoformat()
        episodes[(str(row.get("coin")), day)].append(float(value))
    values = [sum(v) / len(v) for v in episodes.values()]
    result = summary_stats(values)
    if values:
        result["trade_n"] = sum(len(v) for v in episodes.values())
    return result


def winsorized_mean(xs, p: float = 0.05):
    xs = sorted(_clean(xs))
    n = len(xs)
    if n == 0:
        return None
    k = int(n * p)
    if k == 0:
        return round(sum(xs) / n, 3)
    lo, hi = xs[k], xs[n - 1 - k]
    w = [lo] * k + xs[k:n - k] + [hi] * k
    return round(sum(w) / n, 3)


def trimmed_mean(xs, p: float = 0.05):
    xs = sorted(_clean(xs))
    n = len(xs)
    if n == 0:
        return None
    k = int(n * p)
    core = xs[k:n - k] if n - 2 * k > 0 else xs
    return round(sum(core) / len(core), 3) if core else None


def summary_stats(xs, *, ci: bool = True) -> dict:
    xs = _clean(xs)
    n = len(xs)
    if n == 0:
        return {"n": 0}
    lo, hi = bootstrap_ci(xs) if (ci and n >= 3) else (None, None)
    return {
        "n": n,
        "mean": round(st.mean(xs), 3),
        "median": round(st.median(xs), 3),
        "stdev": round(st.pstdev(xs), 3) if n > 1 else 0.0,
        "winsor_mean": winsorized_mean(xs),
        "sum": round(sum(xs), 1),
        "ci_lo": lo,
        "ci_hi": hi,
        "spans_zero": (lo is not None and lo < 0 < hi),
        "win_rate": round(sum(1 for x in xs if x > 0) / n, 3),
    }


def concentration(xs) -> dict:
    """How top-heavy is the PnL? top-k share of the total sum. >1.0 means the rest is net negative."""
    xs = _clean(xs)
    tot = sum(xs)
    if not xs or tot == 0:
        return {"top1_share": None, "top5_share": None, "n": len(xs)}
    s = sorted(xs)
    return {
        "top1_share": round(s[-1] / tot, 3),
        "top5_share": round(sum(s[-5:]) / tot, 3),
        "n_positive": sum(1 for x in xs if x > 0),
        "n": len(xs),
    }


def per_wallet(rows, *, key: str = "target", value: str = "pnl_bps", min_n: int = 1) -> list[dict]:
    """Group rows by wallet, summary each. Sorted by mean desc. Only wallets with >= min_n closes."""
    from collections import defaultdict
    buckets = defaultdict(list)
    for r in rows:
        v = r.get(value)
        if v is not None:
            buckets[r.get(key)].append(float(v))
    out = []
    for wallet, vals in buckets.items():
        if len(vals) < min_n:
            continue
        s = summary_stats(vals)
        s["wallet"] = wallet
        out.append(s)
    return sorted(out, key=lambda d: d["mean"], reverse=True)


def beta_neutral(gross_bps, beta_bps) -> dict:
    """Decompose gross directional return into market-beta contribution + residual alpha.
    gross_bps[i] = signed directional return; beta_bps[i] = sign*market_return over the hold.
    alpha[i] = gross[i] - beta[i]. Returns means + alpha summary (CI). Lists must align."""
    g = _clean(gross_bps)
    b = _clean(beta_bps)
    if not g or len(g) != len(b):
        return {"n": len(g), "alpha": {"n": len(g)}}
    alpha = [g[i] - b[i] for i in range(len(g))]
    mg = st.mean(g)
    mb = st.mean(b)
    return {
        "n": len(g),
        "gross_mean": round(mg, 3),
        "beta_mean": round(mb, 3),
        "beta_share_pct": round(100 * mb / mg, 1) if mg else None,
        "alpha": summary_stats(alpha),
    }


def size_decay(trades, sizes) -> list[dict]:
    """Deployable PnL vs notional. trades: [{pnl_bps, entry_depth, exit_depth}] where *_depth are
    compact book dicts (fills.compact_book output). For each size: real = paper - round-trip VWAP
    slippage on the captured books; feasible only where the book could absorb it both legs."""
    out = []
    for size in sizes:
        reals, slips, feasible, considered, censored = [], [], 0, 0, 0
        for t in trades:
            ed = t.get("entry_depth")
            if not ed:
                continue
            considered += 1
            xd = t.get("exit_depth") or ed
            rt = round_trip_slippage_bps(ed, size, xd)
            if rt is None:
                censored += 1
                continue
            feasible += 1
            slips.append(rt)
            reals.append(float(t["pnl_bps"]) - rt)
        out.append({
            "size_usd": size,
            "considered": considered,
            "feasible": feasible,
            "censored_depth": censored,
            "measured": considered - censored,
            "feasible_pct": (
                round(100 * feasible / (considered - censored), 1)
                if considered > censored else None
            ),
            "mean_slippage_bps": round(st.mean(slips), 2) if slips else None,
            "mean_real_bps": round(st.mean(reals), 2) if reals else None,
        })
    return out


def total_book_mtm(realized_bps, open_mtm_bps) -> dict:
    """Honest total: realized closes + open positions marked to market (net-of-fee, caller-computed).
    Defeats target-close censoring, where winners realize fast and losers linger unrealized."""
    r = _clean(realized_bps)
    o = _clean(open_mtm_bps)
    total = sum(r) + sum(o)
    n = len(r) + len(o)
    return {
        "realized_n": len(r), "realized_sum_bps": round(sum(r), 1),
        "realized_mean_bps": round(st.mean(r), 2) if r else None,
        "open_n": len(o), "open_mtm_sum_bps": round(sum(o), 1),
        "open_mtm_mean_bps": round(st.mean(o), 2) if o else None,
        "total_bps": round(total, 1),
        "total_mean_bps": round(total / n, 2) if n else None,
    }


def source_latency(rows) -> dict:
    """Summarize only causally matched rows with one monotonic corrected-UTC clock."""
    provenance = defaultdict(int)
    components = defaultdict(list)
    invalid_clock = 0
    fields = (
        "source_event_ts_ms", "source_received_ts_ms", "decision_ts_ms",
        "book_request_ts_ms", "book_received_ts_ms", "mirror_open_ts_ms",
    )
    for row in rows:
        status = str(row.get("source_latency_status") or "legacy_unknown")
        provenance[status] += 1
        if status != "matched" or any(row.get(field) is None for field in fields):
            continue
        event, received, decision, request, book, opened = (
            int(row[field]) for field in fields
        )
        if not event <= received <= decision <= request <= book <= opened:
            invalid_clock += 1
            continue
        components["source_to_receive_ms"].append(received - event)
        components["receive_to_decision_ms"].append(decision - received)
        components["decision_to_book_ms"].append(book - request)
        components["book_to_persist_ms"].append(opened - book)
        components["source_to_mirror_ms"].append(opened - event)
    complete = len(components["source_to_mirror_ms"])
    return {
        "provenance": dict(sorted(provenance.items())),
        "complete_monotonic_n": complete,
        "invalid_clock_n": invalid_clock,
        **{name: summary_stats(values, ci=False) for name, values in components.items()},
    }


def censoring(rows) -> dict:
    """Winner vs loser hold-time — a big gap means realized PnL is biased by target close-timing."""
    def hrs(r):
        return (r["exit_ts_ms"] - r["detect_ts_ms"]) / 3.6e6

    def timed(r):                                          # 0 is a valid ts -> check is-not-None
        return r.get("exit_ts_ms") is not None and r.get("detect_ts_ms") is not None
    wins = [hrs(r) for r in rows if r.get("pnl_bps") is not None and r["pnl_bps"] > 0 and timed(r)]
    loss = [hrs(r) for r in rows if r.get("pnl_bps") is not None and r["pnl_bps"] <= 0 and timed(r)]
    return {
        "winners_n": len(wins), "winners_hold_median_h": round(st.median(wins), 2) if wins else None,
        "losers_n": len(loss), "losers_hold_median_h": round(st.median(loss), 2) if loss else None,
    }


def load_depth(row, which: str) -> dict | None:
    """Rehydrate a captured *_depth_json column into a book dict for size_decay."""
    raw = row.get(f"{which}_depth_json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def shadow_real(closed_rows, *, size: float, fee_bps: float = 9.0) -> dict:
    """Deployable-at-size net PnL per closed trade: paper pnl_bps (already net of the modeled fee)
    minus real round-trip VWAP slippage on the CAPTURED books minus funding paid. Only trades with
    captured entry depth are scored; feasible = the book could absorb ``size`` both legs."""
    reals, considered, feasible, censored = [], 0, 0, 0
    for r in closed_rows:
        if r.get("pnl_bps") is None:
            continue
        ed = load_depth(r, "entry")
        if not ed:
            continue
        considered += 1
        xd = load_depth(r, "exit") or ed
        rt = round_trip_slippage_bps(ed, size, xd)
        if rt is None:
            censored += 1
            continue
        feasible += 1
        if ("funding_pnl_bps" in r
                and r.get("funding_provenance_status") in {"complete", "not_applicable"}):
            fund = r.get("funding_pnl_bps")
            if fund is None:
                continue
            contribution = float(fund)
        elif r.get("funding_provenance_status") is None and r.get("funding_bps") is not None:
            contribution = -float(r["funding_bps"])
        else:
            continue
        reals.append(float(r["pnl_bps"]) - rt + contribution)
    return {
        "size_usd": size, "considered": considered, "feasible": feasible,
        "censored_depth": censored, "measured": considered - censored,
        "funding_complete_n": len(reals),
        "provenance_status": "complete" if len(reals) == feasible else "incomplete_funding",
        "feasible_pct": (
            round(100 * feasible / (considered - censored), 1)
            if considered > censored else None
        ),
        "deployable": summary_stats(reals) if reals else {"n": 0},
    }


def capacity(book: dict, *, budget_bps: float = 25.0) -> float:
    """Max deployable notional for a single book within a round-trip slippage budget."""
    return max_size_within_slippage(book, budget_bps)


def capacity_result(book: dict, *, budget_bps: float = 25.0) -> dict:
    """Capacity with returned-depth censoring provenance."""
    return capacity_with_provenance(book, budget_bps)


def _returns(candles) -> dict:
    """{t: simple_return} from consecutive closes of a sorted candle list ([{t,c}] or [(t,c)])."""
    pts = []
    for k in (candles or []):
        if isinstance(k, dict):
            pts.append((int(k["t"]), float(k["c"])))
        else:
            pts.append((int(k[0]), float(k[1])))
    pts.sort()
    out = {}
    for i in range(1, len(pts)):
        p0 = pts[i - 1][1]
        if p0 > 0:
            out[pts[i][0]] = (pts[i][1] - p0) / p0
    return out


def aligned_returns(market_candles, asset_candles):
    """Return (market_returns, asset_returns) as two lists aligned on shared candle timestamps."""
    mr, ar = _returns(market_candles), _returns(asset_candles)
    ts = sorted(set(mr) & set(ar))
    return [mr[t] for t in ts], [ar[t] for t in ts]


def estimate_beta(market_returns, asset_returns):
    """OLS beta of asset on market = cov(asset, market) / var(market). None if degenerate.
    Removes the 'alts are high-beta to BTC' inflation from a naive beta=1 neutralization."""
    m = [float(x) for x in market_returns]
    a = [float(x) for x in asset_returns]
    n = min(len(m), len(a))
    if n < 3:
        return None
    m, a = m[:n], a[:n]
    mbar, abar = sum(m) / n, sum(a) / n
    var = sum((mi - mbar) ** 2 for mi in m)
    if var <= 0:
        return None
    cov = sum((m[i] - mbar) * (a[i] - abar) for i in range(n))
    return round(cov / var, 4)


def price_at(candles, ms: int):
    """Step price at timestamp ms from sorted candles ([{t,c}] or [(t,c)]). Clamps to ends."""
    pts = []
    for k in (candles or []):
        if isinstance(k, dict):
            pts.append((int(k["t"]), float(k["c"])))
        else:
            pts.append((int(k[0]), float(k[1])))
    if not pts:
        return None
    pts.sort()
    if ms <= pts[0][0]:
        return pts[0][1]
    if ms >= pts[-1][0]:
        return pts[-1][1]
    lo, hi = 0, len(pts) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if pts[mid][0] < ms:
            lo = mid + 1
        else:
            hi = mid
    return pts[lo][1]


def _gross_bps(side: str, entry, exit_) -> float | None:
    try:
        e, x = float(entry), float(exit_)
    except (TypeError, ValueError):
        return None
    if e <= 0 or x <= 0:
        return None
    g = (x - e) / e * 1e4
    return -g if side == "short" else g


def build_report(closed, open_rows, *, marks: dict, market_candles=None, asset_candles=None,
                 sizes=(1000, 5000, 10000, 25000, 50000, 100000), fee_bps: float = 9.0) -> dict:
    """Assemble the full honest scorecard from raw rows + live marks (+ optional market series).
    Pure: injected marks/candles make it deterministic and unit-testable."""
    pnls = [r["pnl_bps"] for r in closed if r.get("pnl_bps") is not None]
    clean_pnls = [r["pnl_bps"] for r in closed
                  if r.get("pnl_bps") is not None and not r.get("is_cold_start")]
    cold_pnls = [r["pnl_bps"] for r in closed
                 if r.get("pnl_bps") is not None and r.get("is_cold_start")]
    report = {
        "realized": summary_stats(pnls),
        "episode_realized": episode_stats(closed),
        "clean_realized": summary_stats(clean_pnls),   # new-entry-only (excludes cold-start mirror)
        "cold_start": summary_stats(cold_pnls),         # pre-existing book mirrored on first sight
        "concentration": concentration(pnls),
        "censoring": censoring(closed),
        "per_wallet": per_wallet([r for r in closed if not r.get("is_cold_start")], min_n=5),
        "source_latency": source_latency([*closed, *open_rows]),
    }

    # total-book MTM (realized + open marked to current mark, net of fee)
    open_net, open_net_clean = [], []
    for r in open_rows:
        g = _gross_bps(r.get("side"), r.get("entry_mark"), marks.get(str(r.get("coin", "")).upper()))
        if g is not None:
            open_net.append(g - fee_bps)
            if not r.get("is_cold_start"):
                open_net_clean.append(g - fee_bps)
    report["total_book"] = total_book_mtm(pnls, open_net)
    # clean total: cold-start mirrors excluded on BOTH legs — cold-start opens (mirrors of a
    # target's pre-existing long-term holds) can linger unrealized for weeks and dominate the drag
    report["total_book_clean"] = total_book_mtm(clean_pnls, open_net_clean)

    # beta-neutral alpha (needs a market series)
    if market_candles:
        gross, beta = [], []
        coin_betas = {}
        for coin, candles in (asset_candles or {}).items():
            market_returns, asset_returns = aligned_returns(market_candles, candles)
            estimate = estimate_beta(market_returns, asset_returns)
            if estimate is not None:
                coin_betas[str(coin)] = estimate
        for r in closed:
            if r.get("pnl_bps") is None or not r.get("entry_mark") or not r.get("exit_mark"):
                continue
            g = _gross_bps(r["side"], r["entry_mark"], r["exit_mark"])
            p0 = price_at(market_candles, r.get("detect_ts_ms"))
            p1 = price_at(market_candles, r.get("exit_ts_ms"))
            if g is None or not p0 or not p1:
                continue
            sign = 1.0 if r["side"] == "long" else -1.0
            coefficient = coin_betas.get(str(r.get("coin")))
            if asset_candles is not None and coefficient is None:
                continue
            gross.append(g)
            beta.append(sign * (coefficient if coefficient is not None else 1.0)
                        * (p1 - p0) / p0 * 1e4)
        report["beta_neutral"] = beta_neutral(gross, beta) if gross else {"n": 0}
        report["beta_neutral"]["method"] = (
            "estimated_per_coin_ols" if asset_candles is not None else "invalid_beta_1_diagnostic"
        )
        report["beta_neutral"]["coin_betas"] = coin_betas

    # size-decay from captured depth
    with_depth = [{"pnl_bps": r["pnl_bps"], "entry_depth": load_depth(r, "entry"),
                   "exit_depth": load_depth(r, "exit")}
                  for r in closed if r.get("pnl_bps") is not None and load_depth(r, "entry")]
    current_depth = [
        row for row in with_depth
        if int(row["entry_depth"].get("capture_format_version") or 0) >= 2
    ]
    legacy_depth = [row for row in with_depth if row not in current_depth]
    selected_depth = current_depth if current_depth else legacy_depth
    report["size_decay"] = {
        "coverage": f"{len(selected_depth)}/{len(pnls)} closes in selected depth cohort",
        "cohort": "provider_returned_v2" if current_depth else "legacy_15",
        "provenance": {
            "provider_returned_v2": len(current_depth), "legacy_15": len(legacy_depth),
        },
        "rows": size_decay(selected_depth, list(sizes)) if selected_depth else [],
    }

    # funding + spread haircuts from captured columns
    fund = [r["funding_bps"] for r in closed if r.get("funding_bps") is not None]
    funding_pnl = []
    for row in closed:
        if (row.get("funding_provenance_status") in {"complete", "not_applicable"}
                and row.get("funding_pnl_bps") is not None):
            funding_pnl.append(float(row["funding_pnl_bps"]))
        elif row.get("funding_provenance_status") is None and row.get("funding_bps") is not None:
            funding_pnl.append(-float(row["funding_bps"]))
    spr = [r["entry_spread_bps"] for r in closed if r.get("entry_spread_bps") is not None]
    lag = [r["lag_bps"] for r in closed if r.get("lag_bps") is not None]
    report["haircuts"] = {
        "funding_n": len(fund), "funding_mean_bps": round(st.mean(fund), 3) if fund else None,
        "funding_pnl_n": len(funding_pnl),
        "funding_pnl_mean_bps": round(st.mean(funding_pnl), 3) if funding_pnl else None,
        "entry_spread_n": len(spr), "entry_spread_mean_bps": round(st.mean(spr), 3) if spr else None,
        "lag_n": len(lag), "lag_mean_bps": round(st.mean(lag), 3) if lag else None,
    }
    return report
