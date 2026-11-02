"""Pure, look-ahead-free, cost-honest backtest for the BTC 5m algo.

Timing convention: positions[i] is decided from information up to bar i's close, held over the next
bar, and its return realized at bar i+1's close. The cost to ESTABLISH positions[i] (turnover ×
per-side cost) is charged at bar i; the final open position is liquidated at the last close. This is
the part that kills naive crypto signals — every position change pays fees + slippage.

PERIODS_PER_YEAR for 5m bars = 288 × 365 = 105120, used to annualize the per-bar Sharpe.
"""
from __future__ import annotations

import math

PERIODS_PER_YEAR = 288 * 365


def split(bars: list[dict], train_frac: float = 0.7) -> tuple[list[dict], list[dict]]:
    """Chronological train/out-of-sample split (no shuffle — time series)."""
    k = int(len(bars) * train_frac)
    return bars[:k], bars[k:]


def _max_drawdown(equity: list[float]) -> float:
    """Largest peak-to-trough drop of the equity curve, as a positive fraction."""
    peak = equity[0] if equity else 1.0
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def _trades(closes: list[float], positions: list[int], per_side: float) -> list[float]:
    """Per-trade net returns (each pays entry + exit cost). A flip closes one trade and opens another."""
    trades: list[float] = []
    pos, entry = 0, None
    for i in range(len(positions)):
        p = positions[i]
        if p != pos:
            if pos != 0 and entry:
                trades.append(pos * (closes[i] / entry - 1) - 2 * per_side)
            entry = closes[i] if p != 0 else None
            pos = p
    if pos != 0 and entry:
        trades.append(pos * (closes[-1] / entry - 1) - 2 * per_side)
    return trades


def backtest(bars: list[dict], positions: list[int], *, taker_fee: float = 0.0005,
             slippage: float = 0.0002, execution_lag: int = 1) -> dict:
    """Run the cost-adjusted backtest. Returns metrics + the equity curve.

    `positions` is the target DECIDED from info up to bar i's close. `execution_lag` (default 1) is
    the number of bars between the decision and the fill: you cannot trade AT the signal bar's close
    (especially for mean-reversion, where bar i's close is the very extreme that triggered the
    signal — filling there is the look-ahead-flavored artifact that inflates Sharpe). With lag=1 the
    position decided at bar i is entered at bar i+1's close, capturing only the post-signal move.
    Pass execution_lag=0 only to unit-test the raw return/cost mechanics."""
    if len(positions) != len(bars):
        raise ValueError(f"positions ({len(positions)}) must align 1:1 with bars ({len(bars)})")
    closes = [float(b["close"]) for b in bars]
    n = len(closes)
    per_side = float(taker_fee) + float(slippage)
    lag = max(0, int(execution_lag))
    # effective position held over each window = the one decided `lag` bars earlier
    eff = ([0] * lag + list(positions[:max(0, n - lag)])) if lag else list(positions)
    # Charge establishment turnover at EVERY bar (incl. the last — a position opened/flipped on the
    # final bar must still pay its entry), realize gross only where a next bar exists, then liquidate
    # whatever is held at the end. (Was: turnover loop ran n-1, so a final-bar entry/flip went
    # uncharged and equity/Sharpe diverged from the trade-PnL path on the last bar.)
    bar_ret: list[float] = []
    for i in range(n):
        turnover = abs(eff[i] - (eff[i - 1] if i > 0 else 0))
        gross = eff[i] * (closes[i + 1] / closes[i] - 1.0) if i < n - 1 else 0.0
        bar_ret.append(gross - turnover * per_side)
    if eff and eff[-1] != 0:                                    # liquidate the final open position
        bar_ret.append(-abs(eff[-1]) * per_side)

    equity = [1.0]
    for r in bar_ret:
        equity.append(equity[-1] * (1.0 + r))
    total_return = equity[-1] - 1.0

    if len(bar_ret) > 1:
        mu = sum(bar_ret) / len(bar_ret)
        var = sum((r - mu) ** 2 for r in bar_ret) / len(bar_ret)
        sd = math.sqrt(var)
        sharpe = (mu / sd) * math.sqrt(PERIODS_PER_YEAR) if sd > 0 else 0.0
    else:
        sharpe = 0.0

    trades = _trades(closes, eff, per_side)
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    total_pnl = sum(trades)
    largest_share = (max(trades) / total_pnl) if (trades and total_pnl > 0) else 0.0
    exposure = (sum(1 for p in eff if p != 0) / len(eff)) if eff else 0.0

    return {
        "total_return": total_return,
        "sharpe": sharpe,
        "profit_factor": profit_factor,
        "max_drawdown": _max_drawdown(equity),
        "n_trades": len(trades),
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "exposure": exposure,
        "avg_trade": (total_pnl / len(trades)) if trades else 0.0,
        "largest_trade_share": largest_share,
        "equity": equity,
        # exposed so a walk-forward search can POOL across folds (concat bar_returns for one Sharpe,
        # sum gross_win/gross_loss for one PF) instead of averaging per-fold scalars.
        "bar_returns": bar_ret,
        "gross_win": gross_win,
        "gross_loss": gross_loss,
    }
