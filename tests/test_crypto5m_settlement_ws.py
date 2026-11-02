"""Phase 1B Deliverable #3 — harden the `market_resolved` WS settlement parser.

Closes the gap flagged in review: from_ws_resolved silently accepted a settlement for ANY market (no
condition/market identity check), so a wrong-market WS event could write a bogus winner. It also must be
field-order / unknown-field tolerant, and the parser stays `provisional` until a REAL captured fixture is
verified (the current fixture is hand-authored/synthetic).
"""
import json
import os

from sniper.algos.polymarket_crypto5m import settlement as S
from sniper.algos.polymarket_crypto5m.models import MarketSpec

FX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m")


def _spec(condition_id="0xBTCWINDOW", market_id="m1", up="UPTOKEN", down="DOWNTOKEN"):
    return MarketSpec(
        event_id="e", market_id=market_id, condition_id=condition_id, slug="btc-updown-5m-1781947800",
        question="q", raw_rule_text="rt", rule_text_hash="h", resolution_source="src", series_id="s",
        market_created_ts=None, window_start_ts=1781947500, window_end_ts=1781947800,
        up_token_id=up, down_token_id=down, outcomes=("Up", "Down"), price_to_beat=None,
        fees_enabled=False, fee_type=None, taker_base_fee=None, maker_base_fee=None,
        tick_size=None, min_order_size=None)


def test_rejects_condition_id_mismatch():
    spec = _spec(condition_id="0xBTCWINDOW")
    msg = {"condition_id": "0xOTHERMARKET", "winning_token_id": "UPTOKEN"}
    assert S.from_ws_resolved(msg, spec) is None        # wrong market -> no settlement, not silent accept


def test_rejects_market_id_mismatch():
    spec = _spec(market_id="m1")
    msg = {"marketId": "m999", "winning_token_id": "UPTOKEN"}
    assert S.from_ws_resolved(msg, spec) is None


def test_accepts_matching_condition_id():
    spec = _spec()
    s = S.from_ws_resolved({"conditionId": "0xBTCWINDOW", "winning_token_id": "UPTOKEN"}, spec)
    assert s is not None and s.winning_outcome == "Up" and s.winning_token_id == "UPTOKEN"


def test_absent_identity_falls_back_to_token_match():
    # token ids are globally unique, so a winning-token match still ties to our market when no
    # condition id is present (tolerant, provisional schema).
    spec = _spec()
    s = S.from_ws_resolved({"winningAsset": "DOWNTOKEN"}, spec)
    assert s is not None and s.winning_outcome == "Down"


def test_field_order_and_unknown_fields_tolerated():
    spec = _spec()
    msg = {"_extra": 1, "nested": {"a": 2}, "winningAsset": "DOWNTOKEN", "noise": "x",
           "conditionId": "0xBTCWINDOW", "status": "resolved"}
    s = S.from_ws_resolved(msg, spec)
    assert s is not None and s.winning_outcome == "Down"


def test_parse_documented_winning_asset_id_and_market():
    s = S.from_ws_resolved({"market": "0xBTCWINDOW", "winning_asset_id": "DOWNTOKEN"}, _spec())
    assert s is not None and s.winning_outcome == "Down"


def test_winning_outcome_field_fallback():
    s = S.from_ws_resolved({"conditionId": "0xBTCWINDOW", "winning_outcome": "Up"}, _spec())
    assert s is not None and s.winning_outcome == "Up" and s.winning_token_id == "UPTOKEN"


def test_market_field_mismatch_rejected():
    s = S.from_ws_resolved({"market": "0xOTHER", "winning_token_id": "UPTOKEN"}, _spec())
    assert s is None


def test_missing_winner_returns_none():
    assert S.from_ws_resolved({"conditionId": "0xBTCWINDOW"}, _spec()) is None


def test_unknown_winning_token_returns_none():
    # a winning token that is neither up nor down for THIS market -> no settlement
    assert S.from_ws_resolved({"conditionId": "0xBTCWINDOW", "winning_token_id": "ZZZ"}, _spec()) is None


def test_from_gamma_rejects_single_mismatched_market():
    # review fix: a single embedded market whose conditionId != spec must NOT yield a winner under the
    # spec's id (that was the wrong-label hole the multi-market fix left open).
    from dataclasses import replace
    spec = replace(_spec(condition_id="CID_A"),
                   raw_payload={"markets": [{"conditionId": "CID_B", "outcomePrices": ["1", "0"]}]})
    s = S.from_gamma(spec)
    assert s.winning_outcome is None and s.winning_token_id is None


def test_provisional_fixture_parses_and_parser_is_provisional():
    with open(os.path.join(FX, "market_resolved_provisional.json")) as f:
        msg = json.load(f)
    spec = _spec()
    s = S.from_ws_resolved(msg, spec)
    assert s is not None and s.winning_outcome == "Up"
    assert S.ws_parser_status() == "provisional"          # synthetic fixture -> not yet verified
