"""A route we priced and declined must not be reported as a missing route.

Hock 0x43330ef8 (Tekkerrss, chain 56, mcap $36.9k), 2026-09-09T06:03:03Z, aborted with

    no quote (no_route: no route on v3/v2/pons_v2 (v4/v4_multihop off))

That sentence is false. The coin's PancakeSwap V2 WBNB pair 0x816cdda0 held 18.115 WBNB — about
$13,586 — and quoting it on demand returned 220,615 tokens for 0.02 WBNB with
`liquidityAvailable: True`. The v2 rung had found it and turned it down:
`round_trip_over_budget:-16.188742` against the 10% budget (0.4 x TP 25).

The gate was right. The sentence sent the reader hunting for BSC liquidity that was there all
along — V2 factory, V3 pools at all four Pancake fee tiers, token bytecode — before the trace
showed the rung's own reason.

`classify_quote_miss` ordered its verdicts hit -> blind -> no_route, so a rung that ANSWERED and
declined fell into the same bucket as a rung that found nothing. Those are opposite facts about a
coin: one says the market is not there, the other says it is there and too expensive.

Only reasons meaning "priced, then declined" are promoted. `no_bridge_candidates` genuinely means
that rung found nothing and stays part of `no_route`.
"""
import pytest

from sniper.execution.evm_real_seams import classify_quote_miss, _PRICED_AND_DECLINED

HOCK = [
    {"rung": "v3", "hit": False},
    {"rung": "v2", "hit": False, "reason": "round_trip_over_budget:-16.188742148673462"},
    {"rung": "pons_v2", "hit": False},
    {"rung": "v4", "hit": False, "skipped": True},
    {"rung": "v4_multihop", "hit": False, "skipped": True},
]


def test_the_hock_ladder_reports_the_cost_refusal_not_a_missing_route():
    code, detail = classify_quote_miss(HOCK)
    assert code == "route_over_budget"
    assert "v2" in detail and "round_trip_over_budget" in detail
    assert "no route" not in detail


def test_a_genuinely_empty_ladder_still_says_no_route():
    code, detail = classify_quote_miss([{"rung": "v3", "hit": False}, {"rung": "v2", "hit": False}])
    assert code == "no_route" and detail == "no route on v3/v2"


def test_no_bridge_candidates_is_not_a_declined_route():
    """That rung found nothing. Promoting it would relabel a real miss as a cost decision."""
    code, _ = classify_quote_miss(
        [{"rung": "v4_multihop", "hit": False, "reason": "no_bridge_candidates"}])
    assert code == "no_route"


def test_a_blind_rung_still_outranks_a_declined_one():
    """Blindness is the more serious fact: it says we could not SEE, which is not a verdict on the
    coin at all. It must keep priority (4663 HOOKR, 2026-08-07)."""
    code, detail = classify_quote_miss([
        {"rung": "v4", "hit": False, "blind_reason": "rate_limit"},
        {"rung": "v2", "hit": False, "reason": "round_trip_over_budget:-16.1"},
    ])
    assert code == "provider_rate_limit" and "could not read" in detail


def test_a_hit_still_short_circuits():
    assert classify_quote_miss([
        {"rung": "v2", "hit": True},
        {"rung": "v3", "hit": False, "reason": "round_trip_over_budget:-99"},
    ]) == (None, None)


def test_a_skipped_rung_cannot_be_promoted():
    """A rung that never ran cannot have priced anything."""
    code, _ = classify_quote_miss([
        {"rung": "v2", "hit": False, "skipped": True,
         "reason": "round_trip_over_budget:-16.1"},
        {"rung": "v3", "hit": False},
    ])
    assert code == "no_route"


def test_the_empty_ladder_is_still_unattributed():
    assert classify_quote_miss([])[0] == "quote_unattributed"


@pytest.mark.parametrize("reason", ["round_trip_over_budget", "round_trip_over_budget:-0.0"])
def test_the_prefix_matches_with_and_without_a_number(reason):
    code, _ = classify_quote_miss([{"rung": "v2", "hit": False, "reason": reason}])
    assert code == "route_over_budget"


def test_the_promoted_set_is_explicit_and_small():
    """A blanket 'any reason is a decline' would relabel every rung-specific miss. Keep this list
    short and add to it only with a case in hand."""
    assert _PRICED_AND_DECLINED == ("round_trip_over_budget",)


def test_a_blind_page_still_names_the_rung_that_decided():
    """Aircoin (4663, 2026-09-12): v2 was blind while pons_v2 priced the curve and declined it on
    cost. The code stays blind; the sentence must also carry the decision."""
    code, detail = classify_quote_miss([
        {"rung": "v3", "hit": False},
        {"rung": "v2", "hit": False, "blind_reason": "rpc_transport", "reason": "read error: x"},
        {"rung": "pons_v2", "hit": False, "reason": "round_trip_over_budget:-13.398364"},
        {"rung": "v4_multihop", "hit": False, "reason": "no_bridge_candidates"},
    ])
    assert code == "provider_rpc_transport"
    # The blind rung's own text is printed too since 2026-09-29 (pos 66990: a Kyber 400 read as
    # bare rpc_transport); the decision sentence is unchanged.
    assert detail == ("v2 could not read: rpc_transport [read error: x]; pons_v2 priced this route "
                      "and declined it: round_trip_over_budget:-13.398364")


def test_taigan_reports_both_priced_routes_without_changing_retry_class():
    rungs = [
        {"rung": "v2", "hit": False, "reason": "round_trip_over_budget:-99.90617953502598"},
        {"rung": "mixed_v3_v2", "hit": False, "reason": "round_trip_over_budget:-17.88930"},
    ]
    code, detail = classify_quote_miss(rungs)
    assert code == "route_over_budget"
    assert all(row["reason"] in detail for row in rungs)
    code, detail = classify_quote_miss([
        {"rung": "v3", "hit": False, "blind_reason": "rpc_transport"}, *rungs])
    assert code == "provider_rpc_transport"
    assert all(row["reason"] in detail for row in rungs)
