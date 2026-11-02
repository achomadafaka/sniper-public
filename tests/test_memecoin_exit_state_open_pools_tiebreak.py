"""`_open_pools` decides which pools the exit-state worker writes state for at all.

It calls the SAME funded resolver as the hot lane and the pull materializer, so it inherits the
same `len(rows) == 1 else None` refusal on an ambiguous mint. That matters more here than at the
other call sites: the materializer can now PRICE an ambiguous mint once the route tie-break is
applied, but if this set still resolves it to None the pool never enters `out`, no exit state is
written, and the pricing improvement has nothing to act on.

The Solana branch must keep using `position_id` and must NEVER pass `quote_route_target` — there
it holds the Jupiter route target, not a pool. That is the exact join mistake 4663 already taught
us, and a naive one-line fix here reintroduces it.
"""
import sqlite3

import scripts.memecoin_exit_state_worker as W

MINT = "0x" + "aa" * 20
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
V3_POOL = "0x" + "c2" * 20
V2_POOL = "0x" + "d3" * 20


def _routers():
    from sniper.settings import memecoin as cfg
    return (str(cfg.MEMECOIN_EVM_V3_ROUTER["4663"]).lower(),
            str(cfg.MEMECOIN_EVM_V2_ROUTER["4663"]).lower())


def _tc(pools):
    """A tick DB where MINT has one row per (pool, kind) supplied."""
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


class _Positions:
    """Stands in for `open_real_positions(mconn)`; the worker only iterates it."""

    def __init__(self, rows):
        self.rows = rows

    def __call__(self, _mconn):
        return self.rows


def _run(monkeypatch, tc, rows):
    monkeypatch.setattr(W, "open_real_positions", _Positions(rows))
    monkeypatch.setattr(W, "in_scope", lambda _chain: True)
    return W._open_pools(object(), tc)


def test_ambiguous_evm_mint_resolves_via_its_own_route(monkeypatch):
    """Two usable pools; the position's router names which protocol it actually traded."""
    tc = _tc([(V3_POOL, "v3_swap"), (V2_POOL, "v2_swap")])
    v3r, v2r = _routers()

    got = _run(monkeypatch, tc, [{"network_id": "4663", "mint": MINT, "id": 1,
                                  "quote_route_target": v3r}])
    assert got == {("4663", V3_POOL.lower())}, \
        "the V3-routed position must resolve to the V3 pool, not fail closed for having two"

    got = _run(monkeypatch, tc, [{"network_id": "4663", "mint": MINT, "id": 1,
                                  "quote_route_target": v2r}])
    assert got == {("4663", V2_POOL.lower())}, \
        "the same ambiguous mint resolves DIFFERENTLY by route — proves the route is consulted"


def test_ambiguous_mint_without_a_route_still_fails_closed(monkeypatch):
    """No route evidence means the old refusal stands. The tie-break must not invent an answer."""
    tc = _tc([(V3_POOL, "v3_swap"), (V2_POOL, "v2_swap")])
    assert _run(monkeypatch, tc, [{"network_id": "4663", "mint": MINT, "id": 1,
                                   "quote_route_target": None}]) == set()


def test_solana_never_passes_the_route_target(monkeypatch):
    """On Solana `quote_route_target` is the Jupiter route, not a pool. Passing it would be the
    4663 join mistake. Assert the resolver is called with position_id and no route_target."""
    seen = {}

    def _spy(_tc, chain, mint, position_id=None, route_target=None):
        seen.update(chain=chain, mint=mint, position_id=position_id, route_target=route_target)
        return None

    import sniper.services.memecoin_exit_materializer as M
    monkeypatch.setattr(M, "resolve_pool_for_position", _spy)
    _run(monkeypatch, _tc([]), [{"network_id": "solana", "mint": MINT, "id": 7,
                                 "quote_route_target": "JUPITERrouteTARGETnotAPool"}])
    assert seen["route_target"] is None, "Solana must never tie-break on the Jupiter route target"
    assert seen["position_id"] == 7, "Solana resolves by position_id"
