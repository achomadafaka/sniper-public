"""The kill-rule-2 baseline counter.

Registered by the promotion prereg §9 so the read cannot silently have no input. Every failure
here is a wrong NUMBER rather than an error, and the numbers feed a stop rule.
"""
import importlib.util
import pathlib

import pytest

SCRIPT = (pathlib.Path(__file__).resolve().parents[1] / "scripts" / "research"
          / "paper_quote_baseline.py")
_spec = importlib.util.spec_from_file_location("paper_quote_baseline", SCRIPT)
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)


def test_the_rule_is_a_SHARE_and_carries_its_denominator():
    """"Stop if rate_limited exceeds 10% of quote attempts" needs attempts. A label counter alone
    is a numerator, and a failure-only count is the shape that hid 40% host downtime for weeks."""
    s = B.summarize({"opened": 90, "skipped_quote_rate_limited@api.jup.ag": 10})
    assert s["attempts"] == 100
    assert s["rate_limited"] == 10
    assert s["rate_limited_share"] == pytest.approx(0.10)


def test_an_unattributed_skip_is_not_counted_as_a_reason():
    """`skipped_no_executable_quote` carries no reason and no host. Folding it into any reason
    bucket would invent a cause."""
    s = B.summarize({"opened": 5, B.UNATTRIBUTED: 95})
    assert s["unattributed"] == 95
    assert s["by_reason"] == {} and s["by_host"] == {}
    assert s["rate_limited"] == 0


def test_low_attribution_makes_the_rule_unreadable_rather_than_zero():
    """THE MEASURED CASE, 2026-08-29: 1 of 299 skips attributable. Publishing a confident 0.3%
    over a 99% unattributed population would be the worst available outcome — the share is a lower
    bound on an unknown, not a measurement."""
    s = B.summarize({"opened": 700, B.UNATTRIBUTED: 298,
                     "skipped_quote_no_route@api.jup.ag": 1})
    assert s["attributable_share"] < B.MIN_ATTRIBUTABLE
    assert s["rate_limited_share"] == 0.0, "zero here means unmeasured, not measured-as-zero"


def test_full_attribution_reads_normally():
    s = B.summarize({"opened": 80, "skipped_quote_rate_limited@api.jup.ag": 15,
                     "skipped_quote_no_route@lite-api.jup.ag": 5})
    assert s["attributable_share"] == 1.0
    assert s["by_reason"] == {"rate_limited": 15, "no_route": 5}
    assert s["by_host"] == {"api.jup.ag": 15, "lite-api.jup.ag": 5}


def test_a_label_with_no_host_still_parses():
    """The EVM path attaches neither reason nor host, and a half-formed label must not crash the
    counter or silently vanish."""
    s = B.summarize({"opened": 9, "skipped_quote_provider_error@": 1})
    assert s["by_reason"] == {"provider_error": 1}
    assert s["by_host"] == {"(no host)": 1}


def test_the_rule_is_scoped_to_the_chain_jupiter_quotes():
    """Kill rule 2 is about Jupiter rate-limiting and Jupiter quotes Solana. An EVM attempt cannot
    be rate-limited by Jupiter, so counting it dilutes the share of the thing the rule is about.

    Measured: 295 of 298 unattributed skips were EVM, and chains 56, 8453 and 1 had 203 attempts
    with ZERO opens. Over all chains the lane read 29.3% unpriceable; on Solana alone it is 0.8%.
    """
    assert B.JUPITER_CHAIN == "solana"


def test_no_skips_is_not_a_zero_share():
    s = B.summarize({"opened": 50})
    assert s["skips"] == 0
    assert s["attributable_share"] is None, "no skips means nothing to attribute, not 0% attributed"


def test_empty_input_does_not_divide_by_zero():
    s = B.summarize({})
    assert s["attempts"] == 0 and s["rate_limited_share"] is None
