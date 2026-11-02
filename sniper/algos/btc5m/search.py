"""Walk-forward feature/param SEARCH over the BTC-algo signals — the honest way to look for an edge
across many candidates without fooling ourselves.

Discipline (same as the single backtest, enforced here):
- Each fold computes features on its OWN slice (no train->oos leakage).
- 1-bar execution lag + realistic costs (via backtest.backtest).
- SELECTION is on TRAIN aggregates only (gate + rank by train Sharpe). The chosen config's OOS is
  REPORTED untouched and a `confirmed` flag says whether it actually held out-of-sample. Ranking by
  the OOS you report is the selection bias we refuse to commit.
- Robustness: OOS is aggregated over k expanding walk-forward folds; we surface the WORST-fold OOS
  Sharpe (oos_sharpe_min) so a config that only works in one regime is visible as such.
"""
from __future__ import annotations

from sniper.algos.btc5m import backtest as BT

PASS = {"min_trades": 200, "min_oos_pf": 1.1, "min_oos_sharpe": 1.0}


def walk_forward_folds(bars: list[dict], k: int = 4) -> list[tuple[list, list]]:
    """k expanding-window (train, oos) folds. Fold i trains on everything before segment i+1 and
    tests on segment i+1, so train is always chronologically before oos and grows each fold."""
    n = len(bars)
    if k < 1 or n < (k + 1):
        return []
    seg = n // (k + 1)
    folds = []
    for i in range(1, k + 1):
        cut = seg * i
        oos_end = seg * (i + 1) if i < k else n
        train, oos = bars[:cut], bars[cut:oos_end]
        if train and oos:
            folds.append((train, oos))
    return folds


def _pooled_sharpe(bar_returns: list[float]) -> float:
    """One Sharpe over the CONCATENATED bar-return stream (not a mean of per-fold Sharpes — averaging
    annualized ratios over unequal/short folds is the wrong estimator and over-weights quiet folds)."""
    import math
    if len(bar_returns) < 2:
        return 0.0
    mu = sum(bar_returns) / len(bar_returns)
    var = sum((r - mu) ** 2 for r in bar_returns) / len(bar_returns)
    sd = math.sqrt(var)
    return (mu / sd) * math.sqrt(BT.PERIODS_PER_YEAR) if sd > 0 else 0.0


def _pooled_pf(gross_win: float, gross_loss: float) -> float:
    return (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)


def evaluate_config(bars: list[dict], signal_fn, params: dict, *, cost: dict | None = None,
                    k: int = 4) -> dict:
    """Run one (signal, params) config through walk-forward. Sharpe/PF are POOLED over the folds'
    concatenated return streams; per-fold Sharpe is kept only as a robustness floor (oos_sharpe_min)
    and per-fold trade counts as a coverage floor (*_trades_min). Features recomputed per slice (no leak)."""
    cost = cost or {"taker_fee": 0.0002, "slippage": 0.0}
    folds = walk_forward_folds(bars, k)
    tr_ret, oo_ret_stream = [], []                      # concatenated bar-return streams (pooled Sharpe)
    tr_gw = tr_gl = oo_gw = oo_gl = 0.0                 # summed gross win/loss (pooled PF)
    oo_fold_sharpe, oo_total_ret = [], []
    tr_trades, oo_trades = [], []
    for train, oos in folds:
        bt = BT.backtest(train, signal_fn(train, **params), **cost)
        bo = BT.backtest(oos, signal_fn(oos, **params), **cost)
        tr_ret += bt["bar_returns"]; tr_gw += bt["gross_win"]; tr_gl += bt["gross_loss"]
        oo_ret_stream += bo["bar_returns"]; oo_gw += bo["gross_win"]; oo_gl += bo["gross_loss"]
        oo_fold_sharpe.append(bo["sharpe"]); oo_total_ret.append(bo["total_return"])
        tr_trades.append(bt["n_trades"]); oo_trades.append(bo["n_trades"])
    return {
        "signal": getattr(signal_fn, "__name__", str(signal_fn)),
        "params": params,
        "folds": len(folds),
        "train_sharpe": _pooled_sharpe(tr_ret),
        "train_pf": _pooled_pf(tr_gw, tr_gl),
        "train_trades": sum(tr_trades),
        "train_trades_min": min(tr_trades) if tr_trades else 0,
        "oos_sharpe": _pooled_sharpe(oo_ret_stream),
        "oos_sharpe_min": min(oo_fold_sharpe) if oo_fold_sharpe else 0.0,    # worst-fold robustness guard
        "oos_pf": _pooled_pf(oo_gw, oo_gl),
        "oos_ret": sum(oo_total_ret),
        "oos_trades": sum(oo_trades),
        "oos_trades_min": min(oo_trades) if oo_trades else 0,
    }


def select_best(results: list[dict], *, min_trades: int = None, min_fold_trades: int = 10) -> dict | None:
    """Pick the best config — gate + rank on TRAIN ONLY (train_sharpe>0, train_pf>1.0,
    train_trades>=min, AND a per-fold floor train_trades_min>=min_fold_trades so a single-regime
    winner can't pass on a lopsided total). Returns it with a `confirmed` flag = did its OOS aggregate
    clear the pass bar (pooled pf+sharpe, worst-fold>0, enough total AND per-fold trades). None if no
    config survives the TRAIN gate. (Per-fold floors only apply when *_trades_min is present.)"""
    mt = PASS["min_trades"] if min_trades is None else min_trades

    def _fold_floor_ok(r, key):
        v = r.get(key)
        return v is None or v >= min_fold_trades         # enforced only when the per-fold count exists

    gated = [r for r in results
             if r.get("train_sharpe", 0) > 0 and r.get("train_pf", 0) > 1.0
             and r.get("train_trades", 0) >= mt and _fold_floor_ok(r, "train_trades_min")]
    if not gated:
        return None
    best = dict(max(gated, key=lambda r: r["train_sharpe"]))
    best["confirmed"] = bool(
        best.get("oos_trades", 0) >= mt
        and _fold_floor_ok(best, "oos_trades_min")
        and best.get("oos_sharpe", 0) >= PASS["min_oos_sharpe"]
        and best.get("oos_sharpe_min", -1) > 0           # every fold positive (robustness)
        and best.get("oos_pf", 0) >= PASS["min_oos_pf"])
    return best
