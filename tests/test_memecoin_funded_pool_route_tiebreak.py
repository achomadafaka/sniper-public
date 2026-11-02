"""`resolve_pool_for_position` is the FUNDED position->pool resolver (2026-09-01).

The funded runner reaches it: `scripts/memecoin_runner_loop.py` imports `build_evm_hot_book` and
`run_hot_exit_lane` from memecoin_hot_supervisor, which calls this at hot_supervisor.py:762, and
the pull lane calls it again in memecoin_exit_materializer. Ambiguity there falls back to the
pull lane rather than resolving.

It carried the same defect as the paper twin `tape_mid_for_position`: `len(rows) == 1` else None,
so a mint with TWO usable pools is refused for having too many, not too few. Over the funded 4663
book that is the larger half of the gap — 29% ambiguous against 11% with no pool at all.

The position records which ROUTER filled it, so the pool on that protocol is a record of what we
traded rather than a guess.
"""
import sqlite3

from sniper.services.memecoin_exit_materializer import (
    resolve_pool_for_position, route_protocol)

MINT = "0x" + "aa" * 20
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
V4_POOL = "0x" + "b1" * 32
V3_POOL = "0x" + "c2" * 20
V2_POOL = "0x" + "d3" * 20


def _routers():
    from sniper.execution.evm_v4_quote import V4
    from sniper.settings import memecoin as cfg
    return (str(V4["4663"]["universal_router"]).lower(),
            str(cfg.MEMECOIN_EVM_V3_ROUTER["4663"]).lower(),
            str(cfg.MEMECOIN_EVM_V2_ROUTER["4663"]).lower())


def _tc(pools):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT,"
              " quote_side INT, resolved_at TEXT, token0_decimals INT, token1_decimals INT)")
    c.execute("CREATE TABLE pool_ticks (chain_id TEXT, pool TEXT, kind TEXT, block_no INT)")
    for pool, kind in pools:
        c.execute("INSERT INTO pool_meta VALUES ('4663',?,?,?,0,'2026-09-01T00:00:00+00:00',18,18)",
                  (pool.lower(), WETH, MINT))
        c.execute("INSERT INTO pool_ticks VALUES ('4663',?,?,1)", (pool.lower(), kind))
    c.commit()
    return c


def _r(tc, route=None):
    return resolve_pool_for_position(tc, "4663", MINT, route_target=route)


def test_the_unambiguous_case_is_unchanged_by_the_route():
    tc = _tc([(V3_POOL, "v3_swap")])
    assert _r(tc) == V3_POOL.lower()
    v4r, _, _ = _routers()
    assert _r(tc, v4r) == V3_POOL.lower(), \
        "one usable pool is the answer regardless of route; the tie-break breaks TIES"


def test_ambiguous_without_a_route_still_fails_closed():
    tc = _tc([(V4_POOL, "v3_swap"), (V3_POOL, "v3_swap")])
    assert _r(tc) is None
    assert _r(tc, "0x" + "99" * 20) is None, "unrecognised router -> no tie-break -> fail closed"


def test_the_route_selects_the_pool_on_its_own_protocol():
    v4r, v3r, _ = _routers()
    tc = _tc([(V4_POOL, "v3_swap"), (V3_POOL, "v3_swap")])
    assert _r(tc, v4r) == V4_POOL.lower()
    assert _r(tc, v3r) == V3_POOL.lower()
    # V4 pool inserted FIRST, so returning rows[0] would pass the v4 case by accident.
    tc2 = _tc([(V3_POOL, "v3_swap"), (V4_POOL, "v3_swap")])
    assert _r(tc2, v4r) == V4_POOL.lower(), "selected by row position, not by shape"


def test_v2_and_v3_are_separated_by_tick_kind():
    """Both are 42-char addresses, so shape cannot tell them apart."""
    _, v3r, v2r = _routers()
    tc = _tc([(V3_POOL, "v3_swap"), (V2_POOL, "v2_swap")])
    assert _r(tc, v3r) == V3_POOL.lower()
    assert _r(tc, v2r) == V2_POOL.lower()


def test_two_pools_on_the_same_protocol_stay_ambiguous():
    _, v3r, _ = _routers()
    tc = _tc([(V3_POOL, "v3_swap"), ("0x" + "e4" * 20, "v3_swap")])
    assert _r(tc, v3r) is None


def test_solana_is_untouched_by_the_evm_tie_break():
    """Solana resolves by coin-VAULT on token0 and returns before any of this."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT,"
              " quote_side INT, resolved_at TEXT, token0_decimals INT, token1_decimals INT)")
    c.execute("INSERT INTO pool_meta VALUES ('solana','VAULT','Mint4Sol',NULL,1,NULL,9,9)")
    c.commit()
    assert resolve_pool_for_position(c, "solana", "Mint4Sol") == "VAULT"


def test_route_protocol_is_chain_scoped():
    """A router address is only meaningful on its own chain."""
    _, v3r, _ = _routers()
    assert route_protocol("4663", v3r) == "v3"
    assert route_protocol("8453", v3r) is None
    assert route_protocol("4663", None) is None


# --- fill-anchored fallback (2026-09-02) --------------------------------------------------------
# Mint-keyed resolution answers for 1 of 10 recent funded 4663 entries. The persisted fill-anchored
# pool answers for 9. The fallback fires ONLY when mint-keyed returns None, so it is monotone by
# construction: it can only turn "no pool" into "the pool we actually traded", and it cannot change
# any case that resolves today. That is what makes a blind window before the worker runs carry no
# regression risk — during it, the resolver returns exactly what it returns now.

from sniper.services.memecoin_position_pool import (  # noqa: E402
    ensure_position_pool_table, record_position_pool)

FILL_POOL = "0x" + "7f" * 32


def _with_store(tc):
    ensure_position_pool_table(tc)
    return tc


def test_the_fallback_supplies_a_pool_mint_keyed_resolution_cannot_find():
    tc = _with_store(_tc([]))          # no pool_meta rows at all -> mint-keyed returns None
    record_position_pool(tc, 42, "4663", MINT, FILL_POOL, tx="0x" + "de" * 32, log_index=1,
                         now_iso="2026-09-02T10:00:00+00:00")
    assert resolve_pool_for_position(tc, "4663", MINT) is None, "no position id -> unchanged"
    assert resolve_pool_for_position(tc, "4663", MINT, position_id=42) == FILL_POOL.lower()


def test_the_fallback_NEVER_overrides_a_mint_keyed_answer():
    """Monotone by construction. If mint-keyed resolves, that answer stands."""
    tc = _with_store(_tc([(V3_POOL, "v3_swap")]))
    record_position_pool(tc, 42, "4663", MINT, FILL_POOL, tx="0x" + "de" * 32, log_index=1,
                         now_iso="2026-09-02T10:00:00+00:00")
    assert resolve_pool_for_position(tc, "4663", MINT, position_id=42) == V3_POOL.lower()


def test_an_ambiguous_mint_still_reaches_the_fallback():
    """Ambiguity is 35 of 61 live positions. Those must be rescued, not just the empty ones."""
    tc = _with_store(_tc([(V4_POOL, "v3_swap"), (V3_POOL, "v3_swap")]))
    assert resolve_pool_for_position(tc, "4663", MINT) is None      # ambiguous, no route
    record_position_pool(tc, 42, "4663", MINT, FILL_POOL, tx="0x" + "de" * 32, log_index=1,
                         now_iso="2026-09-02T10:00:00+00:00")
    assert resolve_pool_for_position(tc, "4663", MINT, position_id=42) == FILL_POOL.lower()


def test_no_stored_row_leaves_the_behaviour_exactly_as_before():
    tc = _with_store(_tc([]))
    assert resolve_pool_for_position(tc, "4663", MINT, position_id=999) is None


def test_solana_never_consults_the_evm_store():
    """Solana resolves by coin-VAULT and returns before any of this."""
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT,"
              " quote_side INT, resolved_at TEXT, token0_decimals INT, token1_decimals INT)")
    ensure_position_pool_table(c)
    record_position_pool(c, 42, "solana", MINT, FILL_POOL, tx="0x" + "de" * 32, log_index=0,
                         now_iso="2026-09-02T10:00:00+00:00")
    assert resolve_pool_for_position(c, "solana", MINT, position_id=42) is None
