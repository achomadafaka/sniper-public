"""Gamma event parsing + multi-gate discovery for BTC 5m markets. Uses recorded fixtures only."""
from __future__ import annotations

import copy
import json
import os

from sniper.algos.polymarket_crypto5m import market_resolver as MR
from sniper.algos.polymarket_crypto5m.models import RuleReason

FX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m")


def _fx(name):
    with open(os.path.join(FX, name)) as f:
        return json.load(f)


def test_parse_resolved_up_event_is_rule_supported():
    spec, v = MR.parse_event(_fx("event_resolved_up.json"))
    assert v.reason is RuleReason.SUPPORTED and v.rule_supported is True
    assert spec is not None
    assert spec.series_id == "10684"
    assert spec.slug == "btc-updown-5m-1781947200"
    assert spec.window_start_ts == 1781947200
    assert spec.window_end_ts == 1781947500
    assert spec.outcomes == ("Up", "Down")
    assert spec.up_token_id.startswith("971626667236")
    assert spec.down_token_id.startswith("630350983450")
    assert abs(spec.price_to_beat - 63456.04789961816) < 1e-6   # gamma oracle (resolved)
    assert spec.tick_size == 0.001
    assert spec.fees_enabled is True and spec.taker_base_fee == 1000
    assert spec.condition_id.startswith("0x623ded7e")
    # market-state carried for capture_ready / persistence
    assert spec.closed is True and spec.accepting_orders is False


def test_parse_resolved_down_has_per_market_tick():
    spec, v = MR.parse_event(_fx("event_resolved_down.json"))
    assert v.rule_supported is True
    assert spec.tick_size == 0.01
    assert spec.window_start_ts == 1781946900


def test_parse_live_event_is_rule_supported_but_price_to_beat_none():
    spec, v = MR.parse_event(_fx("event_live.json"))
    assert v.rule_supported is True          # it IS the contract...
    assert spec.price_to_beat is None        # ...but no oracle value yet (set at resolution)


def test_capture_ready_rejects_closed_and_orderbook_disabled():
    ready, reason = MR.capture_ready(_fx("event_resolved_up.json"), now_ts=1781947400)
    assert ready is False and reason == "closed"
    ready2, reason2 = MR.capture_ready(_fx("event_live.json"), now_ts=1)
    assert ready2 is False and reason2 == "orderbook_disabled"


def test_capture_ready_true_for_live_tradeable_window():
    raw = copy.deepcopy(_fx("event_resolved_up.json"))
    raw["closed"] = False
    raw["markets"][0]["closed"] = False
    raw["markets"][0]["acceptingOrders"] = True
    raw["markets"][0]["enableOrderBook"] = True
    raw["markets"][0]["endDate"] = "2999-01-01T00:00:00Z"
    ready, reason = MR.capture_ready(raw, now_ts=1781947400)
    assert ready is True and reason == "ready"


def test_capture_ready_window_ended():
    raw = copy.deepcopy(_fx("event_resolved_up.json"))
    raw["closed"] = False
    raw["markets"][0]["closed"] = False
    raw["markets"][0]["acceptingOrders"] = True
    raw["markets"][0]["enableOrderBook"] = True
    ready, reason = MR.capture_ready(raw, now_ts=9_999_999_999)  # far after window_end
    assert ready is False and reason == "window_ended"


def test_wrong_series_yields_no_spec():
    raw = copy.deepcopy(_fx("event_resolved_up.json"))
    raw["series"] = [{"id": "99999", "slug": "not-btc"}]
    raw["seriesSlug"] = "not-btc"
    spec, v = MR.parse_event(raw)
    assert v.reason is RuleReason.WRONG_SERIES and spec is None


def test_missing_condition_id_yields_no_spec():
    raw = copy.deepcopy(_fx("event_resolved_up.json"))
    raw["markets"][0]["conditionId"] = ""
    spec, v = MR.parse_event(raw)
    assert v.reason is RuleReason.MISSING_CONDITION_ID and spec is None


def test_three_token_market_yields_no_spec():
    raw = copy.deepcopy(_fx("event_resolved_up.json"))
    toks = json.loads(raw["markets"][0]["clobTokenIds"])
    raw["markets"][0]["clobTokenIds"] = json.dumps(toks + ["99999999999999999999"])
    spec, v = MR.parse_event(raw)
    assert v.reason is RuleReason.MISSING_TOKEN_MAPPING and spec is None


def test_bad_source_yields_no_spec():
    raw = copy.deepcopy(_fx("event_resolved_up.json"))
    raw["markets"][0]["resolutionSource"] = "https://data.chain.link/streams/btc-usd-v2"
    spec, v = MR.parse_event(raw)
    assert v.reason is RuleReason.UNSUPPORTED_SOURCE and spec is None


def test_raw_payload_preserved_verbatim():
    raw = _fx("event_resolved_up.json")
    spec, _ = MR.parse_event(raw)
    assert spec.raw_payload == raw


def test_window_from_slug_and_hash():
    assert MR.window_from_slug("btc-updown-5m-1781947200") == 1781947200
    assert MR.window_from_slug("garbage") is None
    assert MR.rule_text_hash("abc") == MR.rule_text_hash("abc") != MR.rule_text_hash("abd")


def test_discover_aggregates_accepted_and_rejected():
    good = _fx("event_resolved_up.json")
    bad = copy.deepcopy(good)
    bad["markets"][0]["resolutionSource"] = "https://x/"
    bad["slug"] = "btc-updown-5m-1781940000"
    pages = [[good, bad], []]

    def fake_fetch(url):
        return pages.pop(0) if pages else []

    out = MR.discover(fake_fetch, page_size=2, max_pages=5)
    assert len(out["accepted"]) == 1
    assert out["accepted"][0].slug == "btc-updown-5m-1781947200"
    assert out["rejected"] == [("btc-updown-5m-1781940000", RuleReason.UNSUPPORTED_SOURCE.value)]
    assert out["counts"]["accepted"] == 1 and out["counts"]["rejected"] == 1
