"""Paper executor — executable fills only (buy ask / sell bid + fee). Active management to
target/stop/time. No keys, no real orders."""
from __future__ import annotations

from sniper.hyperlive.models import MarketSnapshot, Position, TradeIdea
from sniper.research.leadlag.event_reactor import executable_pnl_bps


def open_position(idea: TradeIdea, s: MarketSnapshot) -> Position:
    return Position(asset=idea.asset, side=idea.side, entry_bid=s.bid, entry_ask=s.ask,
                    entry_ts_ms=s.ts_ms, target_bps=idea.target_bps, stop_bps=idea.stop_bps,
                    horizon_ms=idea.horizon_ms, setup=idea.setup)


def unrealized_bps(pos: Position, s: MarketSnapshot) -> float | None:
    return executable_pnl_bps(pos.side, pos.entry_bid, pos.entry_ask, s.bid, s.ask)


def evaluate(pos: Position, s: MarketSnapshot) -> tuple[bool, str, float | None]:
    """Return (should_exit, reason, unrealized_bps_now)."""
    net = unrealized_bps(pos, s)
    if net is None:
        return False, "no_mark", None
    age = int(s.ts_ms) - int(pos.entry_ts_ms)
    if net >= pos.target_bps:
        return True, "take_profit", net
    if net <= -pos.stop_bps:
        return True, "stop_loss", net
    if age >= pos.horizon_ms:
        return True, "time_stop", net
    return False, "hold", net
