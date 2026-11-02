import pytest


def test_rules_are_stable_bounded_and_cashtag_only():
    from sniper.analysis.memecoin_x.api_rules import compile_stream_rules

    accounts = [
        {"x_user_id": "2", "handle": "Beta", "role": "tier2_source"},
        {"x_user_id": "1", "handle": "alpha", "role": "tier1_trader"},
    ]
    rules = compile_stream_rules(accounts, max_rule_length=80)
    assert [row["value"] for row in rules] == [
        "(from:alpha OR from:beta) has:cashtags"
    ]
    assert rules[0]["tag"].startswith("sniper-x-intel:")
    assert rules == compile_stream_rules(list(reversed(accounts)), max_rule_length=80)


def test_rule_compiler_splits_only_between_whole_terms():
    from sniper.analysis.memecoin_x.api_rules import compile_stream_rules

    accounts = [
        {"x_user_id": str(i), "handle": f"source_{i}", "role": "tier1_trader"}
        for i in range(1, 7)
    ]
    rules = compile_stream_rules(accounts, max_rule_length=58)
    assert len(rules) > 1
    assert all(len(row["value"]) <= 58 for row in rules)
    assert sum(row["value"].count("from:") for row in rules) == 6
    assert all(row["value"].endswith(" has:cashtags") for row in rules)


@pytest.mark.parametrize("accounts,match", [
    ([], "active curated roster"),
    ([{"x_user_id": "1", "handle": "alpha OR from:evil",
       "role": "tier1_trader"}], "unsafe X handle"),
    ([{"x_user_id": "1", "handle": "alpha", "role": "suggested"}],
     "unsupported roster role"),
])
def test_rule_compiler_rejects_empty_unsafe_or_unsupported_accounts(accounts, match):
    from sniper.analysis.memecoin_x.api_rules import compile_stream_rules

    with pytest.raises(ValueError, match=match):
        compile_stream_rules(accounts)
