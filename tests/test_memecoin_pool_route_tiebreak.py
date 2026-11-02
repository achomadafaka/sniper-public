"""The pool resolver refuses when a mint has more than one usable pool (2026-08-30).

`tape_mid_for_position` fails closed on `len(metas) != 1`. Measured against all 70 funded 4663
mints: 42 resolve, 20 are AMBIGUOUS (>1 usable pool), 8 unresolvable. So 40% of the funded book
has no usable pool resolution, and the ambiguous half is the larger one.

That makes index completeness a LIABILITY. Modelled before landing the V4 backfill: making every
V4 row usable moves funded resolution 42 -> 41, because 4 mints that resolve to exactly one V3
pool today gain a second pool and start failing closed. More data, more refusals.

The evidence to choose is already stored. `memecoin_positions.quote_route_target` records the
ROUTER the position's own fill used — V2 router, V3 router, or the V4 Universal Router — so the
right pool for THIS position is the one on the protocol it actually traded. That is a record, not
a heuristic. Most-ticks or deepest-liquidity would silently pick a pool we never traded, which is
worse than refusing.

Still fails closed when the route is absent, unrecognised, or when two pools sit on the SAME
protocol. A remaining ambiguity is an honest one.
"""
import sqlite3
from datetime import datetime

import pytest

from sniper.services.memecoin_exits import tape_mid_for_position

MINT = "0x" + "aa" * 20
BLOCK_ISO = "2026-08-30T18:00:00+00:00"
NOW_TS = datetime.fromisoformat(BLOCK_ISO).timestamp() + 5.0   # inside the 90s freshness cap
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
V4_POOL = "0x" + "b1" * 32          # 66 chars -> V4, a pool id
V3_POOL = "0x" + "c2" * 20          # 42 chars -> V2/V3, a pool address
V2_POOL = "0x" + "d3" * 20


def _routers():
    from sniper.execution.evm_v4_quote import V4
    from sniper.settings import memecoin as cfg
    return (str(V4["4663"]["universal_router"]).lower(),
            str(cfg.MEMECOIN_EVM_V3_ROUTER["4663"]).lower(),
            str(cfg.MEMECOIN_EVM_V2_ROUTER["4663"]).lower())


def _tc(pools, ticks=True):
    """pools: [(pool, kind, mult)] — `mult` makes each pool price differently, so the returned
    number IDENTIFIES which pool was chosen. Asserting only "not None" let two mutations survive:
    a V4 branch returning metas[:1], and a kind filter hardcoded to v3_swap. Both picked the
    wrong pool while still returning a price."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT,"
              " quote_side INT, resolved_at TEXT, token0_decimals INT, token1_decimals INT)")
    c.execute("CREATE TABLE pool_ticks (chain_id TEXT, pool TEXT, kind TEXT, block_no INT,"
              " amount0 TEXT, amount1 TEXT, observed_at TEXT)")
    # Mirrors production: block_ts is TEXT ISO, and there is a fetched_at column.
    c.execute("CREATE TABLE block_times (chain_id TEXT, block_no INTEGER, block_ts TEXT,"
              " fetched_at TEXT)")
    for i, (pool, kind, mult) in enumerate(pools):
        # quote_side=0 -> WETH is token0, the MINT is token1 (the base side the resolver matches)
        c.execute("INSERT INTO pool_meta VALUES ('4663',?,?,?,0,'2026-08-30T00:00:00+00:00',18,18)",
                  (pool.lower(), WETH, MINT))
        if ticks:
            for b in (100 + 10 * i, 101 + 10 * i):
                c.execute("INSERT INTO pool_ticks VALUES ('4663',?,?,?,?,?,?)",
                          (pool.lower(), kind, b, str(mult * 10 ** 18), str(-(10 ** 18)),
                           BLOCK_ISO))
                c.execute("INSERT INTO block_times VALUES ('4663',?,?,?)", (b, BLOCK_ISO, BLOCK_ISO))
    c.commit()
    return c


def _px_of(mult):
    """The exact USD price a pool with this multiplier must produce."""
    return float(mult) * 2000.0


def _pos(route):
    return {"network_id": "4663", "mint": MINT, "quote_route_target": route}


def _price(tc, pos):
    return tape_mid_for_position(tc, pos, quote_usd=2000.0, now_ts=NOW_TS)[0]


def test_one_usable_pool_still_resolves_without_any_route():
    """The unambiguous path must not start depending on the route."""
    tc = _tc([(V3_POOL, "v3_swap", 3)])
    assert _price(tc, _pos(None)) == pytest.approx(_px_of(3))


def test_two_pools_on_DIFFERENT_protocols_resolve_to_the_one_the_fill_used():
    v4r, v3r, _ = _routers()
    tc = _tc([(V4_POOL, "v3_swap", 7), (V3_POOL, "v3_swap", 3)])
    assert _price(tc, _pos(None)) is None, "no route -> still ambiguous -> fail closed"
    # The V4 pool is FIRST, so a tie-break that returned metas[:1] would pass the v4 case by
    # accident. Both cases assert the exact pool.
    assert _price(tc, _pos(v4r)) == pytest.approx(_px_of(7))
    assert _price(tc, _pos(v3r)) == pytest.approx(_px_of(3))


def test_THE_TIEBREAK_ACTUALLY_READS_THE_ROUTE():
    """Mutation guard. A tie-break that ignored the route and returned the first row would pass a
    test where the wanted pool happens to be first. Here the V4 pool is inserted FIRST, and the
    route names the V3 one — so picking row[0] resolves the pool the position never traded.

    Proven by the tape: each pool carries a DIFFERENT price, so the returned number identifies
    which pool was chosen, not merely that one was.
    """
    _, v3r, _ = _routers()
    tc = _tc([(V4_POOL, "v3_swap", 7), (V3_POOL, "v3_swap", 3)])
    got = _price(tc, _pos(v3r))
    assert got != pytest.approx(_px_of(7)), "returned the V4 pool -> the route was ignored"
    assert got == pytest.approx(_px_of(3))


def test_two_pools_on_the_SAME_protocol_stay_ambiguous():
    """An honest remaining ambiguity. Do not invent a second tie-break to squeeze these out."""
    _, v3r, _ = _routers()
    other = "0x" + "e4" * 20
    tc = _tc([(V3_POOL, "v3_swap", 3), (other, "v3_swap", 5)])
    assert _price(tc, _pos(v3r)) is None


def test_an_unrecognised_route_target_fails_closed():
    tc = _tc([(V4_POOL, "v3_swap", 7), (V3_POOL, "v3_swap", 3)])
    assert _price(tc, _pos("0x" + "99" * 20)) is None


def test_v2_and_v3_are_separated_by_tick_kind_since_both_are_42_char():
    """Shape alone only isolates V4. A V2 pool address and a V3 pool address are both 42 chars, so
    the protocol comes from what kind of swap the pool actually emits."""
    _, v3r, v2r = _routers()
    tc = _tc([(V3_POOL, "v3_swap", 3), (V2_POOL, "v2_swap", 9)])
    assert _price(tc, _pos(v3r)) == pytest.approx(_px_of(3))
    assert _price(tc, _pos(v2r)) == pytest.approx(_px_of(9)), \
        "a kind filter hardcoded to v3_swap returns the V3 pool for a V2 route"


def test_zero_usable_pools_is_still_unresolvable_with_a_route():
    """A tie-break breaks ties; it cannot conjure a pool."""
    tc = _tc([])
    _, v3r, _ = _routers()
    assert _price(tc, _pos(v3r)) is None


def test_the_V4_branch_selects_BY_SHAPE_not_by_position():
    """Mutation guard for the V4 branch specifically. In the test above the V4 pool happens to be
    first, so `return metas[:1]` passes it by accident. Here the V4 pool is SECOND."""
    v4r, _, _ = _routers()
    tc = _tc([(V3_POOL, "v3_swap", 3), (V4_POOL, "v3_swap", 7)])
    assert _price(tc, _pos(v4r)) == pytest.approx(_px_of(7))


def test_a_single_pool_resolves_even_when_the_route_names_another_protocol():
    """The tie-break breaks TIES. It must not become a consistency check on the unambiguous path:
    one usable pool is the answer regardless of what the route says, and firing the filter at
    len==1 would turn a working resolution into a refusal."""
    v4r, _, _ = _routers()
    tc = _tc([(V3_POOL, "v3_swap", 3)])
    assert _price(tc, _pos(v4r)) == pytest.approx(_px_of(3))
