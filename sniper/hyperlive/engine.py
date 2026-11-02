"""Hyperlive engine — the single-trade state machine. Always in exactly one state; one open
position at a time. Drives SCANNING→SELECTING→DESIGNING→ARMED→MANAGING→CLOSING→EVALUATING and
loops. `step(snapshots, ts)` advances one stage and returns the public state for the UI."""
from __future__ import annotations

from sniper.hyperlive import execution as EX
from sniper.hyperlive import strategist as ST
from sniper.hyperlive.calibration import Calibration
from sniper.hyperlive.models import MarketSnapshot, Position, TradeIdea


class Portfolio:
    """Shared guard across parallel horizon tracks: no two tracks hold the same asset at
    once (prevents fake diversification / hidden concentration)."""
    def __init__(self):
        self.positions = {}             # track -> (asset, side)

    def can_enter(self, asset, side, track):
        return all(a != asset for t, (a, s) in self.positions.items() if t != track)

    def register(self, track, asset, side):
        self.positions[track] = (asset, side)

    def release(self, track):
        self.positions.pop(track, None)


class HyperliveEngine:
    def __init__(self, calib: Calibration | None = None, on_event=None, *,
                 track: str = "all", allowed_setups: set | None = None, portfolio: Portfolio | None = None):
        self.calib = calib or Calibration()
        self.track = track
        self.allowed_setups = allowed_setups
        self.portfolio = portfolio
        self.state = "SCANNING"
        self.cycle_id = 0
        self.idea: TradeIdea | None = None
        self.ranked = []
        self.pos: Position | None = None
        self.last_unrealized_bps: float | None = None
        self.last_close = None          # dict: setup, regime, reason, realized_bps, won
        self.no_trade_reason = ""
        self.history = []               # list of closed-cycle dicts
        self._on_event = on_event       # optional callback(kind, payload) for persistence/UI

    def _emit(self, kind, **payload):
        if self._on_event:
            self._on_event(kind, payload)

    def _by_asset(self, snapshots, asset):
        for s in snapshots:
            if s.asset == asset:
                return s
        return None

    def step(self, snapshots: list[MarketSnapshot], ts_ms: int) -> dict:
        if self.state == "SCANNING":
            self.state = "SELECTING"

        elif self.state == "SELECTING":
            self.idea, self.ranked, self.no_trade_reason = ST.select(snapshots, self.calib, self.allowed_setups)
            if self.idea is None:
                self._emit("no_trade", cycle=self.cycle_id, reason=self.no_trade_reason)
                self.cycle_id += 1
                self.state = "SCANNING"
            else:
                self.state = "DESIGNING"

        elif self.state == "DESIGNING":
            self._emit("idea", cycle=self.cycle_id, idea=self.idea)
            self.state = "ARMED"

        elif self.state == "ARMED":
            s = self._by_asset(snapshots, self.idea.asset)
            if s is None or s.status != "ok":
                return self.public_state()          # wait for a fresh snapshot of the asset
            # entry-timing helper: cluster_approach waits until price is near the cluster
            if self.idea.entry_hint == "cluster_approach":
                dist = float(s.features.get("cluster_distance_bps") or 0.0)
                if dist > self.idea.target_bps:     # not close enough yet
                    return self.public_state()
            if self.portfolio is not None and not self.portfolio.can_enter(self.idea.asset, self.idea.side, self.track):
                self.no_trade_reason = f"portfolio guard: {self.idea.asset} held by another track"
                self._emit("no_trade", cycle=self.cycle_id, reason=self.no_trade_reason)
                self.idea = None
                self.cycle_id += 1
                self.state = "SCANNING"
                return self.public_state()
            self.pos = EX.open_position(self.idea, s)
            if self.portfolio is not None:
                self.portfolio.register(self.track, self.pos.asset, self.pos.side)
            self._emit("entry", cycle=self.cycle_id, position=self.pos)
            self.state = "MANAGING"

        elif self.state == "MANAGING":
            s = self._by_asset(snapshots, self.pos.asset)
            if s is None or s.status != "ok":
                return self.public_state()
            exit_now, reason, net = EX.evaluate(self.pos, s)
            self.last_unrealized_bps = net
            if exit_now:
                self._exit_reason = reason
                self._exit_net = net
                self.state = "CLOSING"

        elif self.state == "CLOSING":
            won = self._exit_net is not None and self._exit_net > 0
            self.last_close = {"asset": self.pos.asset, "setup": self.pos.setup,
                               "regime": self.idea.regime, "reason": self._exit_reason,
                               "realized_bps": round(self._exit_net or 0.0, 2), "won": won,
                               "thesis": self.idea.thesis}
            self._emit("exit", cycle=self.cycle_id, close=self.last_close)
            self.state = "EVALUATING"

        elif self.state == "EVALUATING":
            self.calib.update(self.pos.setup, self.idea.regime, won=self.last_close["won"],
                              realized_move_bps=self.last_close["realized_bps"])
            if self.portfolio is not None:
                self.portfolio.release(self.track)
            self.history.append({"cycle": self.cycle_id, **self.last_close})
            self._emit("evaluated", cycle=self.cycle_id, close=self.last_close,
                       calibration=self.calib.snapshot())
            self.pos = None
            self.idea = None
            self.cycle_id += 1
            self.state = "SCANNING"

        return self.public_state()

    def public_state(self) -> dict:
        return {
            "track": self.track,
            "state": self.state,
            "cycle_id": self.cycle_id,
            "no_trade_reason": self.no_trade_reason,
            "idea": _idea_dict(self.idea),
            "ranked": [_cand_dict(c) for c in self.ranked[:8]],
            "position": _pos_dict(self.pos),
            "unrealized_bps": self.last_unrealized_bps,
            "last_close": self.last_close,
            "history": self.history[-25:],
            "calibration": self.calib.snapshot(),
        }


def _idea_dict(i):
    return None if i is None else {
        "asset": i.asset, "setup": i.setup, "side": i.side, "horizon_ms": i.horizon_ms,
        "target_bps": i.target_bps, "stop_bps": i.stop_bps, "ev_bps": i.ev_bps,
        "confidence": i.confidence, "regime": i.regime, "thesis": i.thesis}


def _cand_dict(c):
    return {"asset": c.asset, "setup": c.setup, "side": c.side, "regime": c.regime,
            "ev_bps": c.ev_bps, "p_win": c.p_win, "target_bps": c.target_bps,
            "stop_bps": c.stop_bps, "cost_bps": c.cost_bps, "confidence": c.confidence}


def _pos_dict(p):
    return None if p is None else {"asset": p.asset, "side": p.side, "setup": p.setup,
                                   "entry_bid": p.entry_bid, "entry_ask": p.entry_ask,
                                   "target_bps": p.target_bps, "stop_bps": p.stop_bps,
                                   "horizon_ms": p.horizon_ms}
