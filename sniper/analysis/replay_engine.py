"""Forward Replay Engine for Sniper

Deterministic forward simulation of copy-trade strategies against curated
historical signal data.  Tracks bankroll, exposure limits, position counts,
and circuit breakers.

The engine NEVER reads signal_history directly -- all signal data flows
through ``db.replay_curated_signals()``.

Design constraints (from brainstorm):
- Simple fill assumptions (instant fill at signal entry_price)
- No order book simulation
- No fake microstructure precision
- Deterministic, explainable rules
"""

from __future__ import annotations

import json
import sqlite3
from heapq import heappop, heappush
from dataclasses import dataclass, field
from typing import Any

import db
from sniper.replay.adapters import (
    ReplayCandidate,
    ReplayLifecycleState,
    get_replay_adapter_for_strategy,
)


@dataclass
class ReplayConfig:
    """All parameters for a replay run.  Mirrors paper_trader gates."""

    strategy_type: str = "insider_copy_v1"

    # Capital
    initial_bankroll: float = 1000.0
    base_size_usdc: float = 25.0

    # Position limits
    max_concurrent_positions: int = 20
    max_exposure_per_market_usdc: float = 200.0
    max_total_exposure_usdc: float = 1000.0

    # Entry gates (mirror paper_trader where data is available)
    min_insider_score: float = 55.0
    max_speculator_score: float = 70.0
    max_entry_price: float = 0.70
    skip_classifications: set[str] = field(default_factory=lambda: {"arb_fund"})
    # NOTE: qualifying_roi gate is omitted in v1 -- signal_history does not carry
    # wallet-level qualifying_roi.  Future: join score_history for this gate.

    # Circuit breakers (None = disabled)
    circuit_breaker_max_consecutive_losses: int | None = None
    circuit_breaker_max_drawdown_pct: float | None = None

    # Date range (entry_anchor_ts bounds)
    start_ts: int | None = None
    end_ts: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.skip_classifications, list):
            self.skip_classifications = set(self.skip_classifications)
        self.strategy_type = str(self.strategy_type or "insider_copy_v1").strip().lower()
        if self.start_ts is not None:
            self.start_ts = int(self.start_ts)
        if self.end_ts is not None:
            self.end_ts = int(self.end_ts)
        if self.start_ts is not None and self.end_ts is not None and self.start_ts > self.end_ts:
            raise ValueError("start_ts must be <= end_ts")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe dict."""
        d = {}
        for k, v in self.__dict__.items():
            if isinstance(v, set):
                d[k] = sorted(v)
            else:
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReplayConfig":
        """Deserialize from dict (e.g. from DB JSON column)."""
        d = dict(d)
        if "skip_classifications" in d and isinstance(d["skip_classifications"], list):
            d["skip_classifications"] = set(d["skip_classifications"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ReplayResult:
    """Output of a single replay run."""

    config: ReplayConfig
    trades: list[dict[str, Any]]        # executed trades with entry/exit/pnl
    skipped: list[dict[str, Any]]       # skipped signals with skip_reason
    equity_curve: list[dict[str, Any]]  # [{ts, bankroll, event}]
    summary: dict[str, Any]             # aggregate stats

    def to_dict(self) -> dict[str, Any]:
        """Serialize entire result to JSON-safe dict."""
        return {
            "config": self.config.to_dict(),
            "trades": self.trades,
            "skipped": self.skipped,
            "equity_curve": self.equity_curve,
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Entry gate logic
# ---------------------------------------------------------------------------

def _check_entry_gates(
    signal: dict,
    config: ReplayConfig,
    *,
    open_positions: dict[str, float],
    concurrent_count: int,
    total_exposure: float,
    current_bankroll: float | None = None,
) -> str | None:
    """Check all entry gates.  Returns None if pass, or skip_reason string."""
    if signal.get("position_classification") in config.skip_classifications:
        return "classification_skipped"
    if signal["entry_price"] > config.max_entry_price:
        return "entry_price_above_max"
    if signal.get("position_insider_score", 0) < config.min_insider_score:
        return "insider_score_below_min"
    if signal.get("position_speculator_score", 0) >= config.max_speculator_score:
        return "speculator_score_above_max"
    if signal.get("resolved_price") is None:
        return "unresolved_market"
    size = config.base_size_usdc
    if current_bankroll is not None and current_bankroll < size:
        return "bankroll_depleted"
    if concurrent_count >= config.max_concurrent_positions:
        return "max_concurrent_positions"
    if total_exposure + size > config.max_total_exposure_usdc:
        return "total_exposure_exceeded"
    cond = signal["condition_id"]
    market_exp = open_positions.get(cond, 0.0)
    if market_exp + size > config.max_exposure_per_market_usdc:
        return "market_exposure_exceeded"
    return None


# ---------------------------------------------------------------------------
# Trade P&L computation
# ---------------------------------------------------------------------------

def _compute_trade_pnl(
    signal: dict,
    config: ReplayConfig,
) -> dict[str, Any] | None:
    """Compute trade entry/exit/pnl for a resolved signal.

    Returns a trade dict or None if entry_price <= 0 (guard against division
    by zero).
    """
    entry = signal["entry_price"]
    if entry <= 0:
        return None  # guard: never divide by zero

    resolved_price = signal["resolved_price"]
    size_usdc = config.base_size_usdc
    shares = size_usdc / entry
    exit_value = shares * resolved_price
    pnl_usdc = exit_value - size_usdc
    roi = (resolved_price - entry) / entry

    return {
        "wallet": signal["wallet"],
        "condition_id": signal["condition_id"],
        "outcome": signal["outcome"],
        "title": signal.get("title", ""),
        "entry_price": entry,
        "resolved_price": resolved_price,
        "size_usdc": size_usdc,
        "shares": shares,
        "exit_value": exit_value,
        "pnl_usdc": pnl_usdc,
        "roi": roi,
        "entry_anchor_ts": signal.get("entry_anchor_ts"),
        "resolved_at": signal.get("resolved_at"),
        "position_insider_score": signal.get("position_insider_score"),
        "position_speculator_score": signal.get("position_speculator_score"),
        "position_classification": signal.get("position_classification"),
    }


# ---------------------------------------------------------------------------
# Circuit breaker checks
# ---------------------------------------------------------------------------

def _max_drawdown(equity_curve: list[dict[str, Any]]) -> float:
    """Compute max drawdown percentage from equity curve.

    Returns 0.0 if no drawdown (curve is empty or monotonically increasing).
    """
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]["bankroll"]
    max_dd = 0.0
    for pt in equity_curve:
        bal = pt["bankroll"]
        if bal > peak:
            peak = bal
        if peak > 0:
            dd = (peak - bal) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _circuit_breaker_reason(
    config: ReplayConfig,
    consecutive_losses: int,
    equity_curve: list[dict[str, Any]],
) -> str | None:
    """Check circuit breakers.  Returns reason string or None."""
    if (config.circuit_breaker_max_consecutive_losses is not None
            and consecutive_losses >= config.circuit_breaker_max_consecutive_losses):
        return "circuit_breaker_consecutive_losses"
    if config.circuit_breaker_max_drawdown_pct is not None:
        dd = _max_drawdown(equity_curve)
        if dd >= config.circuit_breaker_max_drawdown_pct:
            return "circuit_breaker_max_drawdown"
    return None


# ---------------------------------------------------------------------------
# Signal summary helper
# ---------------------------------------------------------------------------

def _signal_summary(signal: dict) -> dict[str, Any]:
    """Extract key fields from a signal for skip/audit records."""
    return {
        "wallet": signal.get("wallet"),
        "condition_id": signal.get("condition_id"),
        "outcome": signal.get("outcome"),
        "entry_price": signal.get("entry_price"),
        "entry_anchor_ts": signal.get("entry_anchor_ts"),
        "resolved_at": signal.get("resolved_at"),
        "position_insider_score": signal.get("position_insider_score"),
        "position_speculator_score": signal.get("position_speculator_score"),
        "position_classification": signal.get("position_classification"),
    }


def _current_equity(cash_balance: float, total_exposure: float) -> float:
    """Approximate equity as cash plus open exposure carried at cost basis."""
    return cash_balance + total_exposure


def _candidate_to_signal_dict(
    conn: sqlite3.Connection,
    adapter,
    candidate: ReplayCandidate,
) -> dict[str, Any]:
    """Convert a replay candidate into the signal dict shape used by the core loop."""
    resolved_at = int(candidate.metadata.get("resolved_at") or 0)
    resolved_price = None
    if resolved_at > 0:
        state = adapter.get_lifecycle_state(conn, candidate, resolved_at)
        if state != ReplayLifecycleState.ACTIVE:
            resolved_price = adapter.get_exit_price(conn, candidate, resolved_at)
    return {
        "wallet": candidate.wallet,
        "condition_id": candidate.condition_id,
        "outcome": candidate.outcome,
        "title": candidate.metadata.get("title", ""),
        "entry_price": candidate.entry_price,
        "entry_notional": candidate.metadata.get("entry_notional", 0.0),
        "position_insider_score": candidate.metadata.get("position_insider_score", 0.0),
        "position_speculator_score": candidate.metadata.get("position_speculator_score", 0.0),
        "position_classification": candidate.metadata.get("position_classification", ""),
        "entry_anchor_ts": candidate.entry_ts,
        "is_open": candidate.metadata.get("is_open", 0),
        "resolved_price": resolved_price,
        "resolved_at": resolved_at,
    }


def _settle_due_positions(
    *,
    up_to_ts: int,
    pending_positions: list[tuple[int, int, dict[str, Any]]],
    cash_balance: float,
    open_positions: dict[str, float],
    total_exposure: float,
    trades: list[dict[str, Any]],
    equity_curve: list[dict[str, Any]],
    consecutive_losses: int,
) -> tuple[float, float, int]:
    """Settle all positions whose resolved_at is <= up_to_ts."""
    while pending_positions and pending_positions[0][0] <= up_to_ts:
        resolved_at, _, trade = heappop(pending_positions)
        size_usdc = float(trade.get("size_usdc") or 0.0)
        cash_balance += float(trade["exit_value"])
        total_exposure = max(0.0, total_exposure - size_usdc)

        cond = str(trade["condition_id"])
        remaining = max(0.0, float(open_positions.get(cond, 0.0)) - size_usdc)
        if remaining > 0:
            open_positions[cond] = remaining
        else:
            open_positions.pop(cond, None)

        trades.append(trade)
        if trade["pnl_usdc"] < 0:
            consecutive_losses += 1
        else:
            consecutive_losses = 0

        equity_curve.append({
            "ts": resolved_at,
            "bankroll": _current_equity(cash_balance, total_exposure),
            "cash_balance": cash_balance,
            "open_exposure": total_exposure,
            "event": "resolution",
            "condition_id": trade["condition_id"],
            "outcome": trade["outcome"],
        })

    return cash_balance, total_exposure, consecutive_losses


# ---------------------------------------------------------------------------
# Core replay loop
# ---------------------------------------------------------------------------

def _run_replay_loop(
    signals: list[dict],
    config: ReplayConfig,
) -> ReplayResult:
    """Run the replay loop over pre-fetched signals.
    """
    cash_balance = config.initial_bankroll
    trades: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = [
        {
            "ts": 0,
            "bankroll": config.initial_bankroll,
            "cash_balance": config.initial_bankroll,
            "open_exposure": 0.0,
            "event": "initial",
        },
    ]
    consecutive_losses = 0

    open_positions: dict[str, float] = {}
    total_exposure = 0.0
    pending_positions: list[tuple[int, int, dict[str, Any]]] = []
    max_concurrent_observed = 0
    max_total_exposure_observed = 0.0
    seq = 0

    for signal in signals:
        entry_ts = int(signal.get("entry_anchor_ts") or 0)
        cash_balance, total_exposure, consecutive_losses = _settle_due_positions(
            up_to_ts=entry_ts,
            pending_positions=pending_positions,
            cash_balance=cash_balance,
            open_positions=open_positions,
            total_exposure=total_exposure,
            trades=trades,
            equity_curve=equity_curve,
            consecutive_losses=consecutive_losses,
        )

        # --- circuit breaker check (before gates) ---
        cb_reason = _circuit_breaker_reason(config, consecutive_losses, equity_curve)
        if cb_reason is not None:
            rec = _signal_summary(signal)
            rec["skip_reason"] = cb_reason
            skipped.append(rec)
            continue

        # --- entry gates ---
        skip_reason = _check_entry_gates(
            signal,
            config,
            open_positions=open_positions,
            concurrent_count=len(pending_positions),
            total_exposure=total_exposure,
            current_bankroll=cash_balance,
        )
        if skip_reason is not None:
            rec = _signal_summary(signal)
            rec["skip_reason"] = skip_reason
            skipped.append(rec)
            continue

        # --- compute trade ---
        trade = _compute_trade_pnl(signal, config)
        if trade is None:
            rec = _signal_summary(signal)
            rec["skip_reason"] = "invalid_entry_price"
            skipped.append(rec)
            continue

        resolved_at = int(signal.get("resolved_at") or 0)
        if resolved_at == 0:
            rec = _signal_summary(signal)
            rec["skip_reason"] = "missing_resolution_timestamp"
            skipped.append(rec)
            continue
        if resolved_at <= entry_ts:
            rec = _signal_summary(signal)
            rec["skip_reason"] = "resolved_before_entry"
            skipped.append(rec)
            continue

        # --- open position and reserve capital ---
        size_usdc = float(trade["size_usdc"])
        cash_balance -= size_usdc
        open_positions[trade["condition_id"]] = float(open_positions.get(trade["condition_id"], 0.0)) + size_usdc
        total_exposure += size_usdc
        heappush(pending_positions, (resolved_at, seq, trade))
        seq += 1

        max_concurrent_observed = max(max_concurrent_observed, len(pending_positions))
        max_total_exposure_observed = max(max_total_exposure_observed, total_exposure)

    cash_balance, total_exposure, consecutive_losses = _settle_due_positions(
        up_to_ts=10**18,
        pending_positions=pending_positions,
        cash_balance=cash_balance,
        open_positions=open_positions,
        total_exposure=total_exposure,
        trades=trades,
        equity_curve=equity_curve,
        consecutive_losses=consecutive_losses,
    )

    # --- summary ---
    total_trades = len(trades)
    wins = sum(1 for t in trades if t["pnl_usdc"] > 0)
    losses = sum(1 for t in trades if t["pnl_usdc"] < 0)
    total_pnl = sum(t["pnl_usdc"] for t in trades)
    win_rate = wins / total_trades if total_trades > 0 else 0.0
    avg_roi = (sum(t["roi"] for t in trades) / total_trades) if total_trades > 0 else 0.0
    final_bankroll = _current_equity(cash_balance, total_exposure)

    # Aggregate skip reasons so operators can immediately see why signals
    # were filtered without digging into the full skipped list.
    skip_counts: dict[str, int] = {}
    for s in skipped:
        reason = s.get("skip_reason", "unknown")
        skip_counts[reason] = skip_counts.get(reason, 0) + 1
    top_skip_reasons = sorted(skip_counts.items(), key=lambda x: -x[1])

    summary = {
        "total_signals": len(signals),
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl_usdc": total_pnl,
        "avg_roi": avg_roi,
        "final_bankroll": final_bankroll,
        "max_drawdown_pct": _max_drawdown(equity_curve),
        "total_skipped": len(skipped),
        "top_skip_reasons": top_skip_reasons,
        "max_concurrent_observed": max_concurrent_observed,
        "max_total_exposure_observed": max_total_exposure_observed,
    }

    return ReplayResult(
        config=config,
        trades=trades,
        skipped=skipped,
        equity_curve=equity_curve,
        summary=summary,
    )


def run_replay(
    conn: sqlite3.Connection,
    config: ReplayConfig | None = None,
) -> ReplayResult:
    """Run a full replay against the curated signal feed.

    Parameters
    ----------
    conn : sqlite3.Connection
        Database connection (passed to ``db.replay_curated_signals``).
    config : ReplayConfig, optional
        Replay parameters.  Defaults to ``ReplayConfig()`` if not provided.

    Returns
    -------
    ReplayResult
    """
    if config is None:
        config = ReplayConfig()

    adapter = get_replay_adapter_for_strategy(config.strategy_type)
    if adapter is None:
        raise ValueError(f"Replay not supported for strategy_type '{config.strategy_type}'")

    candidates = adapter.get_candidates(
        conn,
        config.to_dict(),
        config.start_ts,
        config.end_ts,
    )
    signals = [_candidate_to_signal_dict(conn, adapter, candidate) for candidate in candidates]

    return _run_replay_loop(signals, config)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

import argparse
import sys


def _parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sniper Replay Engine")
    parser.add_argument("--bankroll", type=float, default=1000.0)
    parser.add_argument("--size", type=float, default=25.0)
    parser.add_argument("--min-insider", type=float, default=55.0)
    parser.add_argument("--max-spec", type=float, default=70.0)
    parser.add_argument("--max-price", type=float, default=0.70)
    parser.add_argument("--max-concurrent", type=int, default=20)
    parser.add_argument("--max-market-exposure", type=float, default=200.0)
    parser.add_argument("--max-total-exposure", type=float, default=1000.0)
    parser.add_argument("--cb-losses", type=int, default=None)
    parser.add_argument("--cb-drawdown", type=float, default=None)
    parser.add_argument("--start-ts", type=int, default=None)
    parser.add_argument("--end-ts", type=int, default=None)
    parser.add_argument("--config", dest="config_file", default=None)
    parser.add_argument("--json", dest="json_output", action="store_true")
    parser.add_argument("--save-as", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--strategy-type", default="insider_copy_v1")
    return parser.parse_args(argv)


def _cli_main(argv: list[str] | None = None) -> None:
    args = _parse_cli_args(argv)

    if args.config_file:
        with open(args.config_file) as f:
            config = ReplayConfig.from_dict(json.load(f))
    else:
        config = ReplayConfig(
            strategy_type=args.strategy_type,
            initial_bankroll=args.bankroll,
            base_size_usdc=args.size,
            min_insider_score=args.min_insider,
            max_speculator_score=args.max_spec,
            max_entry_price=args.max_price,
            max_concurrent_positions=args.max_concurrent,
            max_exposure_per_market_usdc=args.max_market_exposure,
            max_total_exposure_usdc=args.max_total_exposure,
            circuit_breaker_max_consecutive_losses=args.cb_losses,
            circuit_breaker_max_drawdown_pct=args.cb_drawdown,
            start_ts=args.start_ts,
            end_ts=args.end_ts,
        )

    conn = db.get_connection(args.db) if args.db else db.get_connection()

    import time as _time
    start = _time.monotonic()
    result = run_replay(conn, config)
    duration_ms = int((_time.monotonic() - start) * 1000)

    if args.json_output:
        out = result.to_dict()
        out["duration_ms"] = duration_ms
        print(json.dumps(out, indent=2))
    else:
        s = result.summary
        total_signals = s["total_trades"] + s["total_skipped"]
        print(f"Replay complete in {duration_ms}ms")
        print(f"  Signals: {total_signals} total, {s['total_skipped']} skipped")
        print(f"  Trades:  {s['total_trades']} ({s['wins']}W / {s['losses']}L)")
        if s["win_rate"] is not None and s["total_trades"] > 0:
            print(f"  Win Rate: {s['win_rate']:.1%}")
        else:
            print("  Win Rate: N/A")
        print(f"  P&L:     ${s['total_pnl_usdc']:+.2f}")
        print(f"  Bankroll: ${config.initial_bankroll:.2f} -> ${s['final_bankroll']:.2f}")
        print(f"  Max DD:  {s['max_drawdown_pct']:.1%}")

    if args.save_as:
        from db import save_replay_config, save_replay_run
        try:
            cfg_id = save_replay_config(conn, name=args.save_as, config_json=config.to_dict())
        except Exception:
            cfg_id = None
        save_replay_run(
            conn, config_id=cfg_id, config_json=config.to_dict(),
            summary_json=result.summary, result_json=result.to_dict(),
            signal_count=result.summary.get("total_trades", 0) + result.summary.get("total_skipped", 0),
            trade_count=result.summary.get("total_trades", 0),
            duration_ms=duration_ms,
        )
        print(f"  Saved as: {args.save_as}")

    conn.close()


if __name__ == "__main__":
    _cli_main()
