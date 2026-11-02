"""Phase 1B review fix (Codex #3/#4): market selection must follow the deterministic BTC candidate, NOT
``markets[0]``. A multi-market Gamma event must not let settlement read the wrong market's winner (which
would corrupt the training-dataset label), nor let ``capture_ready`` gate on the wrong market. The
existing fixtures are all single-market, which is why green tests missed this.
"""
import json
import os

from sniper.algos.polymarket_crypto5m.market_resolver import parse_event, capture_ready
from sniper.algos.polymarket_crypto5m.settlement import from_gamma

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m")


def _load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


def test_from_gamma_picks_btc_market_not_index_zero():
    ev = _load("event_resolved_up.json")          # single BTC market, Up wins (outcomePrices ["1","0"])
    btc = (ev.get("markets") or [])[0]
    # Decoy non-BTC market injected at index 0 with the OPPOSITE outcomePrices.
    decoy = {"conditionId": "0xDECOY", "outcomes": "[\"Yes\", \"No\"]",
             "clobTokenIds": "[\"a\", \"b\"]", "outcomePrices": "[\"0\", \"1\"]",
             "umaResolutionStatus": "resolved"}
    multi = {**ev, "markets": [decoy, btc]}
    spec, verdict = parse_event(multi)
    assert verdict.rule_supported
    assert spec.condition_id == btc.get("conditionId")
    settle = from_gamma(spec)
    # Must read the BTC market (Up), NOT the decoy at index 0 (which would say Down).
    assert settle.winning_outcome == "Up"
    assert settle.winning_token_id == spec.up_token_id


def test_capture_ready_selects_btc_candidate_over_index_zero():
    btc = {"conditionId": "0xBTC", "outcomes": "[\"Up\", \"Down\"]",
           "clobTokenIds": "[\"UP\", \"DOWN\"]", "active": True, "closed": False,
           "enableOrderBook": True, "acceptingOrders": True, "endDate": "2031-01-01T00:00:00Z"}
    decoy_closed = {"conditionId": "0xW", "outcomes": "[\"Yes\", \"No\"]",
                    "clobTokenIds": "[\"a\", \"b\"]", "active": False, "closed": True,
                    "enableOrderBook": False, "acceptingOrders": False}
    ev = {"active": True, "closed": False, "enableOrderBook": True,
          "endDate": "2031-01-01T00:00:00Z", "markets": [decoy_closed, btc]}
    ready, reason = capture_ready(ev, now_ts=100)
    assert ready is True and reason == "ready"


def test_capture_ready_ambiguous_multi_btc_candidate_blocks():
    btc1 = {"conditionId": "0xA", "outcomes": "[\"Up\", \"Down\"]", "clobTokenIds": "[\"u1\", \"d1\"]",
            "active": True, "closed": False, "enableOrderBook": True, "acceptingOrders": True,
            "endDate": "2031-01-01T00:00:00Z"}
    btc2 = {**btc1, "conditionId": "0xB", "clobTokenIds": "[\"u2\", \"d2\"]"}
    ev = {"active": True, "closed": False, "enableOrderBook": True,
          "endDate": "2031-01-01T00:00:00Z", "markets": [btc1, btc2]}
    ready, reason = capture_ready(ev, now_ts=100)
    assert ready is False and reason == "ambiguous_market"


def test_single_market_event_unchanged():
    # Regression guard: single-market events keep working exactly as before.
    ev = _load("event_resolved_up.json")
    spec, verdict = parse_event(ev)
    assert verdict.rule_supported
    settle = from_gamma(spec)
    assert settle.winning_outcome == "Up"
    ready, reason = capture_ready({**ev, "active": True, "closed": False, "enableOrderBook": True},
                                  now_ts=0)
    assert reason in ("ready", "window_ended", "not_accepting_orders", "orderbook_disabled",
                      "not_active", "closed")  # deterministic, market-state dependent
