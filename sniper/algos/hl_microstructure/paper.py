"""Visible-book paper execution for Hyperliquid microstructure policies."""
from __future__ import annotations

import json
from dataclasses import dataclass

from sniper.algos.hl_microstructure.book import BookState
from sniper.algos.hl_microstructure.models import FeatureSnapshot, PaperResult, PolicyDecision
from sniper.algos.hl_microstructure import persistence as P


@dataclass(frozen=True)
class PaperConfig:
    notional_usdc: float = 25.0
    leverage: float = 1.0
    fee_rate: float = 0.00045
    take_profit_pct: float = 0.002
    stop_loss_pct: float = 0.0015
    max_hold_ms: int = 180_000
    min_signal_decay_hold_ms: int = 30_000


class PaperEngine:
    def __init__(self, conn, config: PaperConfig | None = None):
        self.conn = conn
        self.config = config or PaperConfig()

    def _record_skip(self, decision: PolicyDecision, snapshot: FeatureSnapshot, snapshot_id: int,
                     reason: str) -> PaperResult:
        P.record_decision(self.conn, decision, snapshot, feature_snapshot_id=snapshot_id,
                          status="skipped", reason=reason, action="skip", fee_rate=self.config.fee_rate)
        return PaperResult("skipped", reason)

    def _exit_reason(self, pos, snapshot: FeatureSnapshot, decision: PolicyDecision) -> str | None:
        mid = snapshot.features.get("mid")
        if mid is None:
            return None
        side = pos["side"]
        sign = 1 if side == "long" else -1
        move = sign * ((float(mid) - float(pos["entry_price"])) / float(pos["entry_price"]))
        age = int(snapshot.decision_ts_ms) - int(pos["opened_ts_ms"])
        plan = _json_dict(pos["exit_plan_json"] if "exit_plan_json" in pos.keys() else None)
        take_profit_pct = _float(plan.get("take_profit_pct"), self.config.take_profit_pct)
        stop_loss_pct = _float(plan.get("stop_loss_pct"), self.config.stop_loss_pct)
        max_hold_ms = int(_float(plan.get("time_stop_ms"), self.config.max_hold_ms))
        min_signal_decay_hold_ms = int(_float(
            plan.get("min_signal_decay_hold_ms"),
            self.config.min_signal_decay_hold_ms,
        ))
        # Risk exits keep priority; decay hysteresis only handles weak/absent signal.
        if move >= take_profit_pct:
            return "take_profit"
        if move <= -stop_loss_pct:
            return "stop_loss"
        if age >= max_hold_ms:
            return "time_stop"
        if str(pos["policy_id"]).startswith("fusion") and decision.action == "skip":
            if decision.reason in ("needs_two_source_families", "weak_fusion_score", "not_enough_flow"):
                if age < min_signal_decay_hold_ms:
                    return "signal_decay_grace"
                return "signal_decay"
            if decision.reason in ("book_missing", "spread_too_wide"):
                return "data_stop"
        # A flip is active opposite evidence, not mere decay: it intentionally bypasses
        # the decay grace window and exits immediately even inside min_signal_decay_hold_ms.
        if decision.action == "enter" and decision.side and decision.side != side:
            return "signal_flip"
        return None

    def _close(self, pos, decision: PolicyDecision, snapshot: FeatureSnapshot, snapshot_id: int,
               book: BookState, reason: str) -> PaperResult:
        exit_side = "short" if pos["side"] == "long" else "long"
        fill = book.taker_fill_quantity(exit_side, quantity=float(pos["quantity"]))
        if not fill.fillable or fill.avg_price is None or fill.quantity is None:
            return self._record_skip(decision, snapshot, snapshot_id, fill.reason)
        exit_fee = fill.notional_usdc * self.config.fee_rate
        sign = 1 if pos["side"] == "long" else -1
        gross = sign * (fill.avg_price - float(pos["entry_price"])) * float(pos["quantity"])
        realized = gross - float(pos["entry_fee_usdc"]) - exit_fee
        P.close_position(self.conn, position_id=int(pos["id"]), exit_fee_usdc=exit_fee,
                         realized_pnl=realized, closed_ts_ms=snapshot.decision_ts_ms, reason=reason)
        P.record_fill(self.conn, position_id=int(pos["id"]), policy_id=decision.policy_id,
                      asset=snapshot.asset, fill_type="exit", side=exit_side, price=fill.avg_price,
                      quantity=fill.quantity, notional_usdc=fill.notional_usdc, fee_usdc=exit_fee,
                      exchange_ts_ms=book.exchange_ts_ms, decision_ts_ms=snapshot.decision_ts_ms)
        P.record_decision(self.conn, decision, snapshot, feature_snapshot_id=snapshot_id,
                          status="exited", reason=reason, position_id=int(pos["id"]),
                          fee_rate=self.config.fee_rate, action="exit", side=exit_side)
        return PaperResult("exited", reason, int(pos["id"]), fill.avg_price, realized)

    def on_decision(self, decision: PolicyDecision, snapshot: FeatureSnapshot,
                    book: BookState | None) -> PaperResult:
        snapshot_id = P.record_feature_snapshot(self.conn, snapshot)
        if book is None:
            return self._record_skip(decision, snapshot, snapshot_id, "book_missing")

        pos = P.open_position_for(self.conn, policy_id=decision.policy_id, asset=snapshot.asset)
        if pos is not None:
            reason = self._exit_reason(pos, snapshot, decision)
            if reason:
                if reason == "signal_decay_grace":
                    P.record_decision(self.conn, decision, snapshot, feature_snapshot_id=snapshot_id,
                                      status="held", reason=reason, position_id=int(pos["id"]),
                                      fee_rate=self.config.fee_rate, action="hold", side=pos["side"])
                    return PaperResult("held", reason, int(pos["id"]))
                return self._close(pos, decision, snapshot, snapshot_id, book, reason)
            P.record_decision(self.conn, decision, snapshot, feature_snapshot_id=snapshot_id,
                              status="held", reason="position_open", position_id=int(pos["id"]),
                              fee_rate=self.config.fee_rate, action="hold", side=pos["side"])
            return PaperResult("held", "position_open", int(pos["id"]))

        if decision.action != "enter" or decision.side not in ("long", "short"):
            return self._record_skip(decision, snapshot, snapshot_id, decision.reason)

        fill = book.taker_fill(decision.side, notional_usdc=self.config.notional_usdc)
        if not fill.fillable or fill.avg_price is None or fill.quantity is None:
            return self._record_skip(decision, snapshot, snapshot_id, fill.reason)

        fee = fill.notional_usdc * self.config.fee_rate
        meta = decision.metadata or {}
        pos_id = P.open_position(self.conn, policy_id=decision.policy_id, asset=snapshot.asset,
                                 side=decision.side, entry_price=fill.avg_price, quantity=fill.quantity,
                                 notional_usdc=fill.notional_usdc, leverage=self.config.leverage,
                                 fee_usdc=fee, opened_ts_ms=snapshot.decision_ts_ms,
                                 entry_thesis=str(meta.get("thesis") or ""),
                                 source_votes=meta.get("source_votes") if isinstance(meta.get("source_votes"), dict) else {},
                                 exit_plan=meta.get("exit_plan") if isinstance(meta.get("exit_plan"), dict) else {})
        P.record_fill(self.conn, position_id=pos_id, policy_id=decision.policy_id, asset=snapshot.asset,
                      fill_type="entry", side=decision.side, price=fill.avg_price,
                      quantity=fill.quantity, notional_usdc=fill.notional_usdc, fee_usdc=fee,
                      exchange_ts_ms=book.exchange_ts_ms, decision_ts_ms=snapshot.decision_ts_ms)
        P.record_decision(self.conn, decision, snapshot, feature_snapshot_id=snapshot_id,
                          status="entered", reason=decision.reason, position_id=pos_id,
                          fee_rate=self.config.fee_rate)
        return PaperResult("entered", decision.reason, pos_id, fill.avg_price)


def _json_dict(raw) -> dict:
    if not raw:
        return {}
    try:
        out = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return out if isinstance(out, dict) else {}


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
