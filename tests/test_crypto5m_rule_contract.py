"""Rule-contract validator for Polymarket BTC 5m Up/Down markets (rule_supported gate only)."""
from __future__ import annotations

from sniper.algos.polymarket_crypto5m import rule_contract as RC
from sniper.algos.polymarket_crypto5m.models import RuleReason

GOOD_RULE = (
    'This market will resolve to "Up" if the Bitcoin price at the end of the time range '
    "specified in the title is greater than or equal to the price at the beginning of that range."
)
CHAINLINK = "https://data.chain.link/streams/btc-usd"


def _kw(**over):
    base = dict(
        series_id="10684",
        condition_id="0x623ded7e5a8f7da555",
        outcomes=("Up", "Down"),
        resolution_source=CHAINLINK,
        rule_text=GOOD_RULE,
        window_start_ts=1781947200,
        window_end_ts=1781947500,
        up_token_id="49680609523783328116971995432191336192400238301050539368950536816873685431784",
        down_token_id="10894856328753282420824365719433032640933448831236044284823731949545132310000",
        token_count=2,
    )
    base.update(over)
    return base


def test_fully_valid_market_is_rule_supported():
    v = RC.validate_rule(**_kw())
    assert v.reason is RuleReason.SUPPORTED
    assert v.rule_supported is True
    assert v.rule_contract_version == RC.RULE_CONTRACT_VERSION


def test_price_to_beat_is_not_a_rule_property():
    # priceToBeat absence must NOT affect rule_supported — it is a separate quality lane.
    assert "price_to_beat" not in RC.validate_rule.__code__.co_varnames


def test_wrong_series_rejected():
    v = RC.validate_rule(**_kw(series_id="99999"))
    assert v.reason is RuleReason.WRONG_SERIES
    assert v.rule_supported is False


def test_missing_condition_id_rejected():
    for bad in (None, "", "   "):
        v = RC.validate_rule(**_kw(condition_id=bad))
        assert v.reason is RuleReason.MISSING_CONDITION_ID
        assert v.rule_supported is False


def test_exact_source_allowlist_rejects_lookalike_suffix():
    # startswith would wrongly accept a versioned/foreign suffix — require an exact normalized match.
    assert RC.validate_rule(**_kw(resolution_source=CHAINLINK + "-v2")).reason is RuleReason.UNSUPPORTED_SOURCE
    assert RC.validate_rule(**_kw(resolution_source="https://data.chain.link/streams/eth-usd")).reason is RuleReason.UNSUPPORTED_SOURCE
    assert RC.validate_rule(**_kw(resolution_source="https://www.binance.com/")).reason is RuleReason.UNSUPPORTED_SOURCE


def test_exact_source_allows_trailing_slash_and_query():
    assert RC.validate_rule(**_kw(resolution_source=CHAINLINK + "/")).reason is RuleReason.SUPPORTED
    assert RC.validate_rule(**_kw(resolution_source=CHAINLINK + "?x=1")).reason is RuleReason.SUPPORTED


def test_operator_flip_in_rule_text_rejected():
    flipped = GOOD_RULE.replace("greater than or equal to", "greater than")
    assert RC.validate_rule(**_kw(rule_text=flipped)).reason is RuleReason.UNSUPPORTED_RULE


def test_malformed_window_when_not_300s_or_missing():
    assert RC.validate_rule(**_kw(window_end_ts=1781947200 + 600)).reason is RuleReason.MALFORMED_WINDOW
    assert RC.validate_rule(**_kw(window_start_ts=None)).reason is RuleReason.MALFORMED_WINDOW


def test_unexpected_outcome_shape_rejected():
    for bad in (("Yes", "No"), ("Up",), ("Up", "Down", "Maybe"), ()):
        assert RC.validate_rule(**_kw(outcomes=bad)).reason is RuleReason.UNEXPECTED_OUTCOME_SHAPE


def test_three_token_market_rejected():
    # a market that ships 3 token ids is malformed — do NOT silently take the first two.
    assert RC.validate_rule(**_kw(token_count=3)).reason is RuleReason.MISSING_TOKEN_MAPPING


def test_missing_or_identical_token_mapping_rejected():
    assert RC.validate_rule(**_kw(up_token_id=None)).reason is RuleReason.MISSING_TOKEN_MAPPING
    assert RC.validate_rule(**_kw(down_token_id="")).reason is RuleReason.MISSING_TOKEN_MAPPING
    assert RC.validate_rule(**_kw(up_token_id="x", down_token_id="x", token_count=2).copy()).reason is RuleReason.MISSING_TOKEN_MAPPING


def test_outcome_for_end_equal_start_maps_to_up():
    assert RC.outcome_for(start_price=100.0, end_price=100.0) == "Up"
    assert RC.outcome_for(start_price=100.0, end_price=100.01) == "Up"
    assert RC.outcome_for(start_price=100.0, end_price=99.99) == "Down"


def test_validation_order_series_then_condition_then_source():
    # multiple failures at once -> series reported first (cheapest identity gate)
    v = RC.validate_rule(**_kw(series_id="0", condition_id="", resolution_source="x", rule_text="garbage"))
    assert v.reason is RuleReason.WRONG_SERIES
    # with series fixed, condition id is next
    v2 = RC.validate_rule(**_kw(condition_id="", resolution_source="x"))
    assert v2.reason is RuleReason.MISSING_CONDITION_ID
