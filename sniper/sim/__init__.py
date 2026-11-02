"""Simulation platform — canonical schema and venue adapters."""
from sniper.sim.schema import RawEvent, EventType
from sniper.sim.ids import make_market_id, parse_market_id
from sniper.sim.venue import VenueAdapter
from sniper.sim.universe import UniverseFilter, MarketCandidate
from sniper.sim.serialize import event_to_jsonl, jsonl_to_event
from sniper.sim.market_type import classify, group

__all__ = [
    "RawEvent",
    "EventType",
    "make_market_id",
    "parse_market_id",
    "VenueAdapter",
    "UniverseFilter",
    "MarketCandidate",
    "event_to_jsonl",
    "jsonl_to_event",
    "classify",
    "group",
]
