"""HL Liquidation v1 strategy plugin.

Detects whale addresses approaching liquidation on Hyperliquid isolated-margin
positions and takes counter-positions (opposite side to the whale).

Data source: hl_whale_positions table (populated by hl_whale_poller.py).
Execution: paper-only via execution/hyperliquid_paper.py.

Three-tier threshold ladder:
  - observe (35%): log + watchlist, no action
  - entry  (25%): open counter-position if distance is worsening
  - panic  (15%): increase leverage on new entries

Exit conditions (checked in order):
  1. Take-profit: 2% move in our direction
  2. Stop-loss: 1% move against us
  3. Forced liquidation: mark crossed our liquidation price
  4. Time stop: 30 minutes since open
  5. Thesis invalidated: whale position gone or distance widened past entry + 5
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

from .base import (
    StrategyEntryDecision,
    StrategyExitDecision,
    StrategyManageState,
    StrategyPlugin,
    StrategyReport,
    StrategySignal,
)

logger = logging.getLogger(__name__)

# Default thresholds
OBSERVE_THRESHOLD = 35.0
ENTRY_THRESHOLD = 25.0
PANIC_THRESHOLD = 15.0
MIN_NOTIONAL_USD = 50_000
DEFAULT_BANKROLL = 10_000.0
DEFAULT_LEVERAGE = 1.5
PANIC_LEVERAGE = 2.0
TP_PCT = 2.0
SL_PCT = 1.0
TIME_STOP_SECONDS = 30 * 60  # 30 minutes


def _extract_whale_wallet(position: Dict[str, Any]) -> str:
    """Extract whale wallet from instance_tag (format: 'whale:0x...')."""
    tag = str(position.get("instance_tag") or "")
    if tag.startswith("whale:"):
        return tag[6:]
    return ""


def _parse_params(strategy: Dict[str, Any]) -> Dict[str, Any]:
    params = strategy.get("parameters") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    return params


class HlLiquidationV1Strategy(StrategyPlugin):
    plugin_id = "hl_liquidation_v1"
    display_name = "HL Liquidation Reaction"
    description = "Counter-trade whales approaching liquidation on Hyperliquid"
    strategy_family = "hl_liquidation"
    supported_execution_modes = ("paper",)
    required_feeds = ("hyperliquid", "hl_whale")
    required_tables = ("hl_whale_positions", "hl_positions", "hl_fills")
    default_loop_seconds = 60

    def discover(self, conn, strategy: Dict[str, Any]) -> List[StrategySignal]:
        params = _parse_params(strategy)
        entry_threshold = float(params.get("entry_threshold") or ENTRY_THRESHOLD)
        observe_threshold = float(params.get("observe_threshold") or OBSERVE_THRESHOLD)
        panic_threshold = float(params.get("panic_threshold") or PANIC_THRESHOLD)
        min_notional = float(params.get("min_notional_usd") or MIN_NOTIONAL_USD)

        rows = conn.execute(
            """SELECT * FROM hl_whale_positions
               WHERE margin_type = 'isolated'
                 AND distance_pct < ?
                 AND position_value_usd > ?
               ORDER BY distance_pct ASC""",
            (observe_threshold, min_notional),
        ).fetchall()

        signals = []
        for row in rows:
            r = dict(row)
            distance = float(r["distance_pct"])
            prev_distance = r.get("prev_distance_pct")

            # Must be below entry threshold to generate a signal
            if distance >= entry_threshold:
                logger.debug(
                    "hl_liq: observe %s %s dist=%.2f%% (above entry %.0f%%)",
                    r["wallet"][:10], r["asset"], distance, entry_threshold,
                )
                continue

            # Must be worsening (distance decreasing toward liquidation)
            if prev_distance is not None and distance >= float(prev_distance):
                logger.debug(
                    "hl_liq: skip %s %s — not worsening (%.2f%% >= prev %.2f%%)",
                    r["wallet"][:10], r["asset"], distance, float(prev_distance),
                )
                continue

            # Counter-trade: opposite side to the whale
            whale_side = r["side"]
            our_side = "long" if whale_side == "short" else "short"

            confidence = max(0.0, min(1.0, 1.0 - distance / entry_threshold))

            # Bucket labels for evaluation dashboards
            if distance < panic_threshold:
                distance_bucket = "panic_<15"
            elif distance < entry_threshold:
                distance_bucket = "entry_15-25"
            else:
                distance_bucket = "observe_25-35"

            whale_lev = float(r["leverage"])
            if whale_lev >= 20:
                threshold_bucket = "high_lev_20x+"
            elif whale_lev >= 10:
                threshold_bucket = "mid_lev_10-20x"
            else:
                threshold_bucket = "low_lev_<10x"

            signals.append(StrategySignal(
                wallet=r["wallet"],
                condition_id=r["asset"],
                outcome=our_side,
                title=f"HL Liq: {r['asset']} counter-{whale_side} (dist {distance:.1f}%)",
                current_price=float(r["mark_price"]),
                position_value=float(r["position_value_usd"]),
                analyzed_ts=int(time.time()),
                metadata={
                    "signal_confidence": round(confidence, 4),
                    "distance_pct": distance,
                    "prev_distance_pct": float(prev_distance) if prev_distance is not None else None,
                    "whale_side": whale_side,
                    "whale_leverage": whale_lev,
                    "whale_wallet": r["wallet"],
                    "margin_type": r["margin_type"],
                    "whale_liq_price": float(r["liquidation_price"]) if r["liquidation_price"] else None,
                    "distance_bucket": distance_bucket,
                    "threshold_bucket": threshold_bucket,
                },
            ))

        # Deduplicate: keep only the closest (best) signal per asset.
        # Multiple whales on the same asset may produce conflicting signals;
        # the closest whale has the strongest liquidation thesis.
        best_per_asset: Dict[str, StrategySignal] = {}
        for sig in signals:
            existing = best_per_asset.get(sig.condition_id)
            if existing is None or (sig.metadata or {}).get("distance_pct", 100) < (existing.metadata or {}).get("distance_pct", 100):
                best_per_asset[sig.condition_id] = sig

        return list(best_per_asset.values())

    def enter(
        self,
        conn,
        strategy: Dict[str, Any],
        signal: StrategySignal,
        open_positions: List[Dict[str, Any]],
    ) -> StrategyEntryDecision:
        params = _parse_params(strategy)
        panic_threshold = float(params.get("panic_threshold") or PANIC_THRESHOLD)
        bankroll = float(params.get("bankroll_usdc") or DEFAULT_BANKROLL)

        # Dedup: reject if we already have an open position on this asset
        for pos in open_positions:
            if pos.get("asset") == signal.condition_id:
                return StrategyEntryDecision(
                    False, f"already_open:{signal.condition_id}",
                )

        distance = float((signal.metadata or {}).get("distance_pct", 100))
        entry_threshold = float(params.get("entry_threshold") or ENTRY_THRESHOLD)
        confidence = max(0.0, min(1.0, 1.0 - distance / entry_threshold)) if entry_threshold > 0 else 0.5

        # Sizing: bankroll-relative
        margin = max(10.0, min(25.0, bankroll * 0.01 * (0.75 + confidence)))
        leverage = PANIC_LEVERAGE if distance < panic_threshold else DEFAULT_LEVERAGE
        size_contracts = margin * leverage / signal.current_price if signal.current_price > 0 else 0

        if size_contracts <= 0:
            return StrategyEntryDecision(False, "zero_size")

        return StrategyEntryDecision(
            should_enter=True,
            reason=f"liq_proximity dist={distance:.1f}% lev={leverage}x",
            size_usdc=margin,
            entry_price=signal.current_price,
            execution_mode="paper",
            metadata={
                "leverage": leverage,
                "size_contracts": size_contracts,
                "whale_wallet": (signal.metadata or {}).get("whale_wallet", ""),
                "distance_pct": distance,
            },
        )

    def manage(self, conn, strategy: Dict[str, Any], position: Dict[str, Any]) -> StrategyManageState:
        asset = position.get("asset", "")
        whale_wallet = _extract_whale_wallet(position)

        # Try hl_whale_positions first (freshest mark price from poller)
        row = conn.execute(
            "SELECT mark_price FROM hl_whale_positions WHERE asset = ? ORDER BY captured_at DESC LIMIT 1",
            (asset,),
        ).fetchone()

        if row:
            mark = float(row["mark_price"])
        else:
            # Fallback to hyperliquid_snapshots
            snap = conn.execute(
                "SELECT mark_price FROM hyperliquid_snapshots WHERE asset = ? ORDER BY captured_at DESC LIMIT 1",
                (asset,),
            ).fetchone()
            mark = float(snap["mark_price"]) if snap else None

        # Check if the specific whale's position still exists AND same side (thesis validation)
        whale_side = position.get("side", "")
        # Our side is opposite to whale's — infer whale's original side
        original_whale_side = "short" if whale_side == "long" else "long"

        whale_row = None
        if whale_wallet:
            whale_row = conn.execute(
                "SELECT side, distance_pct FROM hl_whale_positions WHERE asset = ? AND wallet = ? LIMIT 1",
                (asset, whale_wallet),
            ).fetchone()
        else:
            whale_row = conn.execute(
                "SELECT side, distance_pct FROM hl_whale_positions WHERE asset = ? LIMIT 1",
                (asset,),
            ).fetchone()

        whale_exists = whale_row is not None
        whale_flipped = whale_exists and whale_row["side"] != original_whale_side

        return StrategyManageState(
            current_price=mark,
            signal_open=whale_exists and not whale_flipped,
            metadata={
                "whale_still_open": whale_exists,
                "whale_flipped": whale_flipped,
                "whale_wallet": whale_wallet,
                "whale_current_side": whale_row["side"] if whale_row else None,
                "whale_distance_pct": float(whale_row["distance_pct"]) if whale_row else None,
            },
        )

    def exit(
        self,
        conn,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        params = _parse_params(strategy)
        entry_threshold = float(params.get("entry_threshold") or ENTRY_THRESHOLD)
        tp_pct = float(params.get("tp_pct") or TP_PCT)
        sl_pct = float(params.get("sl_pct") or SL_PCT)
        time_stop_sec = int(params.get("time_stop_seconds") or TIME_STOP_SECONDS)

        entry_price = float(position.get("entry_price") or 0.0)
        side = position.get("side", "long")
        mark = float(state.current_price or position.get("current_price") or entry_price)
        liq_price = float(position.get("liquidation_price") or 0.0)
        opened_ts = int(position.get("opened_ts") or 0)
        now = int(time.time())

        if entry_price <= 0:
            return StrategyExitDecision(False, "no_entry_price")

        # Compute price move in our direction
        direction = 1 if side == "long" else -1
        move_pct = ((mark - entry_price) / entry_price) * 100 * direction

        # 1. Take-profit
        if move_pct >= tp_pct:
            return StrategyExitDecision(True, "take_profit", exit_price=mark)

        # 2. Stop-loss
        if move_pct <= -sl_pct:
            return StrategyExitDecision(True, "stop_loss", exit_price=mark)

        # 3. Forced liquidation: our position hit its own liq price
        if liq_price > 0:
            if side == "long" and mark <= liq_price:
                return StrategyExitDecision(True, "forced_liquidation", exit_price=mark)
            if side == "short" and mark >= liq_price:
                return StrategyExitDecision(True, "forced_liquidation", exit_price=mark)

        # 4. Time stop
        if opened_ts > 0 and (now - opened_ts) >= time_stop_sec:
            return StrategyExitDecision(True, "time_stop", exit_price=mark)

        # 5. Thesis invalidation: whale position gone or flipped sides
        if not state.signal_open:
            if (state.metadata or {}).get("whale_flipped"):
                return StrategyExitDecision(True, "whale_flipped_side", exit_price=mark)
            return StrategyExitDecision(True, "whale_exited", exit_price=mark)

        # 5b. Thesis invalidation: whale distance widened past entry_threshold + 5
        asset = position.get("asset", "")
        whale_wallet = _extract_whale_wallet(position)
        if whale_wallet:
            whale_row = conn.execute(
                "SELECT distance_pct FROM hl_whale_positions WHERE asset = ? AND wallet = ? LIMIT 1",
                (asset, whale_wallet),
            ).fetchone()
        else:
            whale_row = conn.execute(
                "SELECT distance_pct FROM hl_whale_positions WHERE asset = ? LIMIT 1",
                (asset,),
            ).fetchone()
        if whale_row:
            whale_distance = float(whale_row["distance_pct"])
            if whale_distance > entry_threshold + 5:
                return StrategyExitDecision(
                    True, f"thesis_invalidated:distance_widened:{whale_distance:.1f}",
                    exit_price=mark,
                )

        return StrategyExitDecision(False, "hold")

    def report(self, conn, strategy: Dict[str, Any], positions: List[Dict[str, Any]]) -> StrategyReport:
        open_count = sum(1 for p in positions if p.get("state") == "open")
        closed_count = sum(1 for p in positions if p.get("state") == "closed")
        total_pnl = sum(float(p.get("realized_pnl") or 0.0) for p in positions if p.get("state") == "closed")
        return StrategyReport(summary={
            "open": open_count,
            "closed": closed_count,
            "realized_pnl": round(total_pnl, 2),
        })
