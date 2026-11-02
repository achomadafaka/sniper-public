"""Forward paper engine for the BTC 5m algo: advance a paper book one closed bar at a time against a
FROZEN config, open/close fee-adjusted paper positions on signal change, and gate on pre-registered
kill criteria. Pure step/decide/evaluate (unit-testable); the poller script does the I/O.

Paper-only — NEVER places live orders. EXECUTION LAG: the signal is decided on the just-closed bar
and the paper fill is booked at the NEXT bar's close (step() decides on bars[:-1], fills at
bars[-1]). You cannot trade at the close of the bar that triggered the signal — for mean-reversion
that close IS the extreme, and filling there is the artifact that inflates backtest Sharpe. This is
signal-at-next-bar-close PAPER accounting; it does NOT model limit-order fill probability (a resting
quote at the band has adverse selection — real fill rate < 100%), so live results carry an extra,
unmeasured drag beyond what this records. Stated on the live surface."""
from __future__ import annotations

from sniper.algos.btc5m import signals as S

# Forward monitor config — the most-principled mean-reversion CANDIDATE. NOTE: after the honest fix
# (1-bar execution lag + train-side selection) NO signal cleared the train gates on 45d of BTC 5m
# data — i.e. there is no validated edge; this config is a candidate being paper-monitored live, not
# a proven strategy. cost_model labels the fee tier only; fills are booked at the next bar close (see
# module docstring), not at a modeled limit. Do NOT treat forward PnL as evidence of an edge.
FROZEN = {"signal": "meanrev_zscore", "params": {"n": 20, "entry": 2.5, "exit_z": 0.5},
          "per_side": 0.0002, "cost_model": "maker"}

# Pre-registered pass bar (see design doc). Until cleared the surface shows "gathering evidence".
PASS = {"min_trades": 200, "min_sharpe": 1.0, "min_profit_factor": 1.1, "max_drawdown": 0.25,
        "max_single_trade_share": 0.40}

# Assets the forward paper book runs on. The SAME frozen signal is forward-tested INDEPENDENTLY on
# each (the multi-asset search found no robust per-asset edge — n_confirmed=0 — so per-asset tuned
# configs would be overfit; one frozen meanrev signal across assets is the honest forward test).
# Env-overridable (anti-hardcoding). Each asset keeps its own state + paper book (tables key by symbol).
import os as _os  # noqa: E402

SYMBOLS = [s.strip().upper() for s in
           _os.getenv("BTC5M_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()]
_LABELS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}


def label(symbol: str) -> str:
    """Short display ticker for an asset symbol (BTCUSDT -> BTC)."""
    return _LABELS.get(symbol, symbol.replace("USDT", "").replace("USD", "") or symbol)


def new_state() -> dict:
    return {"position": 0, "entry_px": None, "entry_ms": None}


def target_position(bars: list[dict], config: dict | None = None) -> int:
    """Target position at the LAST bar from the configured signal."""
    cfg = config or FROZEN
    fn = S.SIGNALS[cfg["signal"]]
    pos = fn(bars, **cfg.get("params", {}))
    return pos[-1] if pos else 0


def step(state: dict, bars: list[dict], config: dict | None = None) -> tuple[dict, list[dict]]:
    """Advance the paper book one fill. EXECUTION LAG: the target is decided on the prior closed bar
    (bars[:-1]) and the fill is booked at the latest bar's close (bars[-1]) — you act one bar after
    the signal, never at the signal bar's own (extreme) close. Returns (new_state, closed_trades)."""
    cfg = config or FROZEN
    per_side = float(cfg.get("per_side", 0.0002))
    if len(bars) < 2:                                       # need a prior bar to decide + a bar to fill
        return dict(state), []
    last = bars[-1]
    px, ms = float(last["close"]), int(last["close_ms"])
    target = target_position(bars[:-1], cfg)               # decide on the just-closed PRIOR bar
    closed: list[dict] = []
    pos = int(state.get("position", 0))
    if target == pos:
        return dict(state), closed
    if pos != 0 and state.get("entry_px"):                  # close existing
        entry = float(state["entry_px"])
        gross = pos * (px / entry - 1.0)
        closed.append({"side": pos, "entry_px": entry, "exit_px": px,
                       "entry_ms": state.get("entry_ms"), "exit_ms": ms,
                       "fee_pct": 2 * per_side, "pnl_pct": gross - 2 * per_side})
    ns = new_state()
    if target != 0:                                          # open new
        ns = {"position": target, "entry_px": px, "entry_ms": ms}
    return ns, closed


def advance(state: dict, bars: list[dict], *, since_ms: int, config: dict | None = None) -> tuple[dict, list[dict]]:
    """REPLAY every closed bar whose close_ms > since_ms, applying step() one bar at a time. This is
    what makes the forward book correct regardless of how often the poller runs: a fire+exit that
    happens between two infrequent polls (or during a skipped cron run) is still recorded, because we
    re-decide at each intermediate bar instead of only looking at the latest one. No look-ahead — each
    replayed bar's decision uses only bars up to (and excluding) it."""
    st = dict(state)
    closed_all: list[dict] = []
    for i in range(len(bars)):
        if int(bars[i]["close_ms"]) <= int(since_ms):
            continue
        st, closed = step(st, bars[:i + 1], config)      # fill bar = bars[i]; decision on bars[:i]
        closed_all += closed
    return st, closed_all


def evaluate(trades: list[dict], *, min_trades: int = None) -> dict:
    """Out-of-sample verdict over closed paper trades vs the pass bar. Never claims success before
    min_trades — status stays 'gathering evidence'. Fees are already embedded in each trade's
    pnl_pct by step(), so there is no per-side parameter here (it would double-count)."""
    mt = PASS["min_trades"] if min_trades is None else min_trades
    n = len(trades)
    pnls = [float(t["pnl_pct"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    total = sum(pnls)
    largest_share = (max(pnls) / total) if (pnls and total > 0) else 0.0
    # equity drawdown
    eq, peak, mdd = 1.0, 1.0, 0.0
    for p in pnls:
        eq *= (1.0 + p)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak if peak > 0 else 0.0)
    import math
    if len(pnls) > 1:
        mu = sum(pnls) / len(pnls)
        sd = math.sqrt(sum((p - mu) ** 2 for p in pnls) / len(pnls))
        sharpe = (mu / sd) * math.sqrt(288 * 365) if sd > 0 else 0.0
    else:
        sharpe = 0.0
    enough = n >= mt
    passed = bool(enough and sharpe >= PASS["min_sharpe"] and pf >= PASS["min_profit_factor"]
                  and mdd <= PASS["max_drawdown"] and largest_share <= PASS["max_single_trade_share"])
    status = ("PASSED" if passed else
              (f"gathering evidence — {n}/{mt} trades" if not enough else "FAILED pass bar"))
    return {"n": n, "win_rate": (len(wins) / n) if n else 0.0, "profit_factor": pf,
            "total_return": eq - 1.0, "max_drawdown": mdd, "sharpe": sharpe,
            "largest_trade_share": largest_share, "passed": passed, "status": status}
