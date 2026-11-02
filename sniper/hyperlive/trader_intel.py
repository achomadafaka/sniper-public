"""HL trader intelligence — the Polymarket wallet analyzer, repurposed for Hyperliquid.

Takes a trader's public fills (+ optional current state) and produces an analyzer verdict:
true realized PnL (fee-aware), skill metrics, archetype, and a copyability gate. The point is
to REJECT uncopyable winners — market-makers whose PnL is rebate, and degen-leverage accounts
whose PnL is one lucky hit — exactly the gate that downgraded every Polymarket 'copyable' wallet.

Pure functions over fill dicts (keys: coin, dir, closedPnl, fee, crossed, sz, px). Funding is
flagged-when-absent, never silently assumed. Read-only research; not a live trigger, not copy
advice. See docs/superpowers/specs/2026-06-22-hl-trader-intelligence-design.md."""
from __future__ import annotations

import math
from collections import defaultdict

# Gates (pre-registered; conservative).
MIN_CLOSES = 20               # below this = low_sample (leaderboard PnL is mostly small-sample noise)
MAKER_FRAC_MM = 0.6           # >= this share of maker fills = market-maker (rebate-driven, uncopyable)
TAKER_FRAC_MIN = 0.5          # a copyable directional trader must be taker-dominant (catchable flow)
SINGLE_FILL_DOMINANCE = 0.5   # one close >= 50% of gross profit = leveraged-variance, not skill
TOP_COIN_DOMINANCE = 0.9      # >90% of PnL from one coin = not breadth, fragile
MIN_PROFIT_FACTOR = 1.1       # gross wins / gross losses

# Phase-2 (forward-follow proof) — frozen.
PHASE2_MIN_CLOSES = 30
PHASE2_MIN_T = 2.0
PHASE2_MAX_CONCENTRATION = 0.40   # max share of PnL OR of closes from one asset
PHASE2_STALE_DAYS = 14


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _closes(fills: list[dict]) -> list[dict]:
    return [f for f in fills if str(f.get("dir", "")).startswith("Close")]


def reconstruct_ledger(fills: list[dict], funding: float | None = None) -> dict:
    """Realized PnL net of fees (fee sign-aware: a negative fee is a maker rebate, so it ADDS).
    funding is folded in when provided; funding_included flags honesty when it isn't."""
    closes = _closes(fills)
    pnls = [_f(f.get("closedPnl")) for f in closes]
    total_fees = sum(_f(f.get("fee")) for f in fills)      # over ALL fills (open legs pay/earn too)
    gross = sum(pnls)
    wins = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    realized = gross - total_fees + (funding or 0.0)
    return {
        "gross_closed_pnl": round(gross, 6),
        "total_fees": round(total_fees, 6),
        "funding": round(funding, 6) if funding is not None else 0.0,
        "funding_included": funding is not None,
        "realized_pnl": round(realized, 6),
        "gross_win": round(wins, 6), "gross_loss": round(losses, 6),
        "profit_factor": round(wins / losses, 2) if losses > 0 else (999.0 if wins > 0 else 0.0),
        "n_closes": len(closes),
    }


def skill_metrics(fills: list[dict]) -> dict:
    """Sample, win rate, profit factor, and the two concentration tests (one-coin / one-fill)
    that separate real breadth from leveraged variance."""
    closes = _closes(fills)
    pnls = [_f(f.get("closedPnl")) for f in closes]
    n = len(closes)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    by_coin = defaultdict(float)
    for f in closes:
        by_coin[f.get("coin")] += _f(f.get("closedPnl"))
    top_coin_pnl = max(by_coin.values(), default=0.0)
    total_pos_pnl = sum(v for v in by_coin.values() if v > 0) or 1e-9
    # single biggest winning close as a share of gross profit
    single_fill_frac = (max(wins) / gross_win) if gross_win > 0 else 0.0
    return {
        "n_closes": n,
        "win_rate": round(len(wins) / n, 3) if n else 0.0,
        "profit_factor": round(gross_win / -sum(losses), 2) if losses else (999.0 if wins else 0.0),
        "top_coin_pnl_frac": round(max(0.0, top_coin_pnl) / total_pos_pnl, 3),
        "single_fill_frac": round(single_fill_frac, 3),
        "realized_gross": round(sum(pnls), 2),
    }


def archetype(fills: list[dict]) -> dict:
    """Classify from execution + concentration, in priority order:
    market_maker (rebate-driven) > degen_leverage (one-hit/one-coin) > directional > mixed."""
    if not fills:
        return {"archetype": "low_sample", "maker_frac": 0.0, "taker_frac": 0.0}
    maker = sum(1 for f in fills if f.get("crossed") is False)
    maker_frac = maker / len(fills)
    taker_frac = 1.0 - maker_frac
    m = skill_metrics(fills)
    if maker_frac >= MAKER_FRAC_MM:
        arch = "market_maker"
    elif m["single_fill_frac"] >= SINGLE_FILL_DOMINANCE or m["top_coin_pnl_frac"] >= TOP_COIN_DOMINANCE:
        arch = "degen_leverage"
    elif taker_frac >= TAKER_FRAC_MIN and m["n_closes"] >= MIN_CLOSES:
        arch = "directional"
    else:
        arch = "mixed"
    return {"archetype": arch, "maker_frac": round(maker_frac, 3), "taker_frac": round(taker_frac, 3)}


def copyability(metrics: dict, arch: dict) -> dict:
    """Copyable only if the edge is directional skill not explained by maker rebate or one
    leveraged hit, with enough sample and real profitability. Returns verdict + reason.
    NOT yet the price-band counterfactual (phase 2) — maker_frac is the available proxy."""
    if metrics["n_closes"] < MIN_CLOSES:
        return {"verdict": "low_sample", "copyable": False, "archetype_pass": False,
                "reason": f"{metrics['n_closes']} closes < {MIN_CLOSES}"}
    if arch["archetype"] == "market_maker":
        return {"verdict": "market_maker", "copyable": False, "archetype_pass": False,
                "reason": f"maker_frac {arch['maker_frac']:.0%} — PnL is rebate, not selection"}
    if arch["archetype"] == "degen_leverage":
        return {"verdict": "degen_leverage", "copyable": False, "archetype_pass": False,
                "reason": f"PnL concentrated (top-coin {metrics['top_coin_pnl_frac']:.0%}, "
                          f"single-fill {metrics['single_fill_frac']:.0%}) — leveraged variance"}
    if metrics["profit_factor"] < MIN_PROFIT_FACTOR:
        return {"verdict": "unprofitable", "copyable": False, "archetype_pass": False,
                "reason": f"profit factor {metrics['profit_factor']} < {MIN_PROFIT_FACTOR}"}
    if arch["archetype"] == "directional":
        return {"verdict": "watch", "copyable": False, "archetype_pass": True,
                "reason": f"directional, taker {arch['taker_frac']:.0%}, PF {metrics['profit_factor']}, "
                          f"n={metrics['n_closes']} (phase-1 archetype-pass; unproven until forward-follow)"}
    return {"verdict": "mixed", "copyable": False, "archetype_pass": False, "reason": "not cleanly directional"}


def assess_trader(fills: list[dict], state: dict | None = None, funding: float | None = None) -> dict:
    """Combined analyzer verdict for one trader."""
    led = reconstruct_ledger(fills, funding=funding)
    m = skill_metrics(fills)
    arch = archetype(fills)
    cop = copyability(m, arch)
    return {**cop, "archetype": arch["archetype"], "maker_frac": arch["maker_frac"],
            "taker_frac": arch["taker_frac"], "ledger": led, "metrics": m}


def _clustered_t(by_cluster: dict) -> float:
    """One-sample t over cluster (coin×day) mean net_bps; H0: mean <= 0."""
    vals = list(by_cluster.values())
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    if var <= 0:
        return 0.0 if mean <= 0 else 999.0
    return mean / math.sqrt(var / n)


def copyability_phase2(follow_stats: dict | None, *, now_ms: int | None = None) -> dict:
    """Promote archetype-pass -> proven only on a real forward-follow record."""
    s = follow_stats or {}
    n = int(s.get("n_closed", 0))
    if n < PHASE2_MIN_CLOSES:
        return {"phase2": "watch", "n_closed": n, "clustered_t": 0.0,
                "reason": f"{n} closes < {PHASE2_MIN_CLOSES}"}
    if max(s.get("top_asset_pnl_frac", 1.0), s.get("top_asset_count_frac", 1.0)) > PHASE2_MAX_CONCENTRATION:
        return {"phase2": "watch", "n_closed": n, "clustered_t": 0.0,
                "reason": "single-asset concentration > 40%"}
    t = _clustered_t(s.get("by_cluster", {}))
    if s.get("mean_net_bps", 0.0) <= 0 or t <= PHASE2_MIN_T:
        return {"phase2": "watch", "n_closed": n, "clustered_t": round(t, 2),
                "reason": f"net {s.get('mean_net_bps')}bps, t={t:.2f} (need >{PHASE2_MIN_T})"}
    if now_ms is not None and (now_ms - int(s.get("last_close_ts", 0))) > PHASE2_STALE_DAYS * 86_400_000:
        return {"phase2": "watch", "n_closed": n, "clustered_t": round(t, 2), "reason": "stale (>14d)"}
    return {"phase2": "proven", "n_closed": n, "clustered_t": round(t, 2),
            "reason": f"forward net +{s.get('mean_net_bps')}bps, t={t:.2f}, n={n}"}


def effective_verdict(stored_verdict: str, archetype_pass, follow_stats: dict | None,
                      *, now_ms: int | None = None) -> str:
    """Two-tier display verdict. archetype-pass -> proven|watch; else the stored reject verdict."""
    if archetype_pass in (1, True) or stored_verdict == "copyable":
        return copyability_phase2(follow_stats, now_ms=now_ms)["phase2"]
    return stored_verdict
