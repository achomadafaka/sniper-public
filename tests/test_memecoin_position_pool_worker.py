"""The resolver worker: read each funded position's entry receipt once, persist its pool.

Placement was ruled post-entry, deferred to the END of the loop iteration, plus a reconciler for
whatever it misses. Measured funded 4663 holds: p10 35s, median 190s, p90 870s — so a pure cron
loses the fastest positions entirely (a 30s reconciler is blind for 85% of a p10 position's life),
while a synchronous hook would put an RPC between a signal and an order on a loop that runs exit
sweeps at 1s against an endpoint that 403s under burst.

ONE function serves both call sites; only `limit` differs. Two implementations of the same sweep
would drift.

It must never raise into its caller: the hook runs inside the funded runner loop, and a resolver
failure must not take down the thing that sells.
"""
import sqlite3

from sniper.services.memecoin_position_pool import ensure_position_pool_table, get_position_pool
from sniper.services.memecoin_position_pool_worker import resolve_pending_position_pools

MINT = "0x" + "aa" * 20
POOL = "0x" + "b1" * 32
TX = "0x" + "de" * 32
SWAP_V4 = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
MGR = "0x8366a39cc670b4001a1121b8f6a443a643e40951"


def _mconn(positions):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, network_id TEXT, "
              "execution_mode TEXT, is_open INT, mint TEXT, opened_at TEXT)")
    c.execute("CREATE TABLE memecoin_fills (position_id INT, kind TEXT, tx_sig TEXT, created_at TEXT)")
    for pid, mint, tx in positions:
        c.execute("INSERT INTO memecoin_positions VALUES (?,?,?,?,?,?)",
                  (pid, "4663", "real", 1, mint, "2026-09-02T10:00:00+00:00"))
        if tx:
            c.execute("INSERT INTO memecoin_fills VALUES (?,?,?,?)",
                      (pid, "entry", tx, "2026-09-02T10:00:00+00:00"))
    c.commit()
    return c


def _tc():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_position_pool_table(c)
    return c


def _receipt(pool=POOL):
    return {"logs": [{"address": MGR, "topics": [SWAP_V4, pool]}]}


def _run(mconn, tc, **kw):
    kw.setdefault("receipt_fn", lambda tx: _receipt())
    kw.setdefault("key_fn", lambda pid: {"currency0": "0x" + "0b" * 20, "currency1": MINT})
    kw.setdefault("now_iso", "2026-09-02T10:00:00+00:00")
    return resolve_pending_position_pools(mconn, tc, "4663", **kw)


def test_it_resolves_and_persists_an_open_position():
    m, t = _mconn([(1, MINT, TX)]), _tc()
    rep = _run(m, t)
    assert rep["resolved"] == 1
    assert get_position_pool(t, 1, "4663") == POOL.lower()


def test_an_already_resolved_position_costs_no_receipt_fetch():
    """The mapping is immutable, so re-fetching is pure waste on a rate-limited endpoint."""
    m, t = _mconn([(1, MINT, TX)]), _tc()
    _run(m, t)
    calls = []
    rep = _run(m, t, receipt_fn=lambda tx: calls.append(tx) or _receipt())
    assert calls == [] and rep["resolved"] == 0 and rep["already"] == 1


def test_a_position_with_no_entry_tx_is_skipped_not_retried_forever():
    m, t = _mconn([(1, MINT, None)]), _tc()
    rep = _run(m, t)
    assert rep["no_tx"] == 1 and rep["resolved"] == 0


def test_an_unidentifiable_receipt_fails_closed_and_stores_nothing():
    """Two candidate pools both holding the mint. Storing a guess would be worse than storing none."""
    other = "0x" + "e4" * 32
    m, t = _mconn([(1, MINT, TX)]), _tc()
    rep = _run(m, t, receipt_fn=lambda tx: {"logs": [{"address": MGR, "topics": [SWAP_V4, POOL]},
                                                     {"address": MGR, "topics": [SWAP_V4, other]}]})
    assert rep["unidentified"] == 1
    assert get_position_pool(t, 1, "4663") is None


def test_a_raising_receipt_fetch_NEVER_propagates():
    """This runs inside the funded runner loop. A resolver failure must not take down the loop
    that sells."""
    def boom(tx):
        raise RuntimeError("rpc down")
    m, t = _mconn([(1, MINT, TX)]), _tc()
    rep = _run(m, t, receipt_fn=boom)
    assert rep["errors"] == 1 and rep["resolved"] == 0


def test_one_bad_position_does_not_stop_the_others():
    def flaky(tx):
        if tx.endswith("01"):
            raise RuntimeError("rpc down")
        return _receipt()
    m, t = _mconn([(1, MINT, TX[:-2] + "01"), (2, MINT, TX)]), _tc()
    rep = _run(m, t, receipt_fn=flaky)
    assert rep["errors"] == 1 and rep["resolved"] == 1
    assert get_position_pool(t, 2, "4663") == POOL.lower()


def test_the_limit_bounds_the_work():
    """The hook passes a small limit so it cannot stretch a loop iteration; the reconciler passes a
    larger one. Same function, different bound."""
    m, t = _mconn([(i, MINT, TX) for i in range(1, 6)]), _tc()
    rep = _run(m, t, limit=2)
    assert rep["resolved"] == 2


def test_only_OPEN_real_positions_on_this_chain_are_considered():
    m, t = _mconn([(1, MINT, TX)]), _tc()
    m.execute("UPDATE memecoin_positions SET is_open=0"); m.commit()
    assert _run(m, t)["resolved"] == 0
    m.execute("UPDATE memecoin_positions SET is_open=1, execution_mode='paper'"); m.commit()
    assert _run(m, t)["resolved"] == 0
