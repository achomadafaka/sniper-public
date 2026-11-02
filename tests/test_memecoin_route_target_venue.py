"""`quote_route_target` must not name the venue wrong.

2026-08-24: the column recorded `jupiter` for exactly the quotes that did NOT come from Jupiter.
`_quote_with_fallback` asks Raydium directly when Jupiter's own priceImpactPct > 5% (the PEECOIN
fix, 2026-08-14). `_ray_quote` normalizes that answer into the Jupiter shape the executor reads,
and the normalized dict has no `routePlan` — so the ammKey loop found nothing and fell through to
a hardcoded `"jupiter"`. `_venue` was in the object all along and is already read at three other
call sites in solana_real_seams.

Note the column is the ROUTER/SETTLER, never pool identity — `pool_meta` is the canonical
position->pool resolver. Naming the venue correctly does not make this a pool key.
"""
from sniper.execution.entry_attribution import _route_target


def _jup(*amm_keys):
    return {"routePlan": [{"swapInfo": {"ammKey": k}} for k in amm_keys]}


def test_jupiter_legs_still_win():
    assert _route_target(_jup("AAA", "BBB"), "solana") == "AAA,BBB"


def test_a_raydium_quote_is_named_raydium_not_jupiter():
    """The regression. This shape has no routePlan and previously recorded 'jupiter'."""
    q = {"_venue": "raydium", "outAmount": "1000"}
    assert _route_target(q, "solana") == "raydium"


def test_legs_take_precedence_over_the_venue_key():
    """If we CAN read the pools, the pools are the more specific answer."""
    q = dict(_jup("AAA"), _venue="raydium")
    assert _route_target(q, "solana") == "AAA"


def test_no_legs_and_no_venue_still_falls_back():
    """Unknown stays 'jupiter' rather than empty — an empty string in this column would read as
    'not recorded' and is worse than a wrong-but-legible default."""
    assert _route_target({"outAmount": "1"}, "solana") == "jupiter"


def test_an_empty_route_plan_is_not_a_route():
    assert _route_target({"routePlan": [], "_venue": "raydium"}, "solana") == "raydium"


def test_legs_without_amm_keys_fall_through_to_the_venue():
    """A routePlan whose legs carry no swapInfo.ammKey is unreadable, not a route."""
    q = {"routePlan": [{"swapInfo": {}}, {}], "_venue": "raydium"}
    assert _route_target(q, "solana") == "raydium"


def test_evm_is_untouched():
    q = {"transaction": {"to": "0xAbCd"}}
    assert _route_target(q, "8453") == "0xabcd"


def test_evm_missing_target_is_untouched():
    assert _route_target({}, "8453") == "0x"
