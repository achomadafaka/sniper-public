"""Simple parallel paper policy lanes for live Hyperliquid research."""
from __future__ import annotations

from dataclasses import dataclass

from sniper.algos.hl_microstructure.models import FeatureSnapshot, PolicyDecision


@dataclass(frozen=True)
class PolicyConfig:
    max_spread_bps: float = 12.0
    min_trade_count_10s: int = 3
    flow_threshold: float = 0.35
    book_imbalance_threshold: float = 0.25
    reversal_return_15s: float = 0.0015


def _f(snapshot: FeatureSnapshot, key: str, default=None):
    v = snapshot.features.get(key)
    return default if v is None else v


def _spread_ok(snapshot: FeatureSnapshot, cfg: PolicyConfig) -> PolicyDecision | None:
    spread = snapshot.features.get("spread_bps")
    if spread is None:
        return PolicyDecision("", "skip", None, "book_missing", 0.0)
    if float(spread) > cfg.max_spread_bps:
        return PolicyDecision("", "skip", None, "spread_too_wide", 0.0)
    return None


def evaluate_policies(snapshot: FeatureSnapshot, cfg: PolicyConfig | None = None) -> list[PolicyDecision]:
    cfg = cfg or PolicyConfig()
    decisions = [PolicyDecision("baseline_no_trade", "skip", None, "control_lane", 0.0)]

    if snapshot.status != "ok":
        for pid in ("flow_momentum", "book_pressure", "vol_spike_reversal"):
            decisions.append(PolicyDecision(pid, "skip", None, snapshot.status, 0.0))
        return decisions

    spread_gate = _spread_ok(snapshot, cfg)
    if spread_gate is not None:
        for pid in ("flow_momentum", "book_pressure", "vol_spike_reversal"):
            decisions.append(PolicyDecision(pid, "skip", None, spread_gate.reason, 0.0))
        return decisions

    flow = float(_f(snapshot, "flow_imbalance_10s", 0.0))
    ret5 = _f(snapshot, "return_5s")
    ret15 = _f(snapshot, "return_15s")
    count = int(_f(snapshot, "trade_count_10s", 0))
    imbalance = _f(snapshot, "book_imbalance_5")

    if count >= cfg.min_trade_count_10s and ret5 is not None and flow >= cfg.flow_threshold and ret5 >= 0:
        decisions.append(PolicyDecision("flow_momentum", "enter", "long", "flow_confirmed", min(1.0, flow)))
    elif count >= cfg.min_trade_count_10s and ret5 is not None and flow <= -cfg.flow_threshold and ret5 <= 0:
        decisions.append(PolicyDecision("flow_momentum", "enter", "short", "flow_confirmed", min(1.0, abs(flow))))
    else:
        decisions.append(PolicyDecision("flow_momentum", "skip", None, "no_flow_edge", 0.0))

    if imbalance is not None and imbalance >= cfg.book_imbalance_threshold and flow >= 0.10:
        decisions.append(PolicyDecision("book_pressure", "enter", "long", "book_and_flow_aligned", min(1.0, imbalance)))
    elif imbalance is not None and imbalance <= -cfg.book_imbalance_threshold and flow <= -0.10:
        decisions.append(PolicyDecision("book_pressure", "enter", "short", "book_and_flow_aligned", min(1.0, abs(imbalance))))
    else:
        decisions.append(PolicyDecision("book_pressure", "skip", None, "no_book_pressure", 0.0))

    if ret15 is not None and ret15 >= cfg.reversal_return_15s and flow < -0.15:
        decisions.append(PolicyDecision("vol_spike_reversal", "enter", "short", "flow_faded_up_spike", min(1.0, abs(flow))))
    elif ret15 is not None and ret15 <= -cfg.reversal_return_15s and flow > 0.15:
        decisions.append(PolicyDecision("vol_spike_reversal", "enter", "long", "flow_faded_down_spike", min(1.0, abs(flow))))
    else:
        decisions.append(PolicyDecision("vol_spike_reversal", "skip", None, "no_reversal_edge", 0.0))

    return decisions
