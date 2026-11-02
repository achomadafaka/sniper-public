"""WO-4 — tape market-breadth (regime context). Deterministic checks on synthetic ticks."""
import sqlite3

from sniper.analysis.market_breadth import tape_market_breadth
from sniper.analysis.tick_features import ensure_feature_tables
from sniper.persistence import db_ticks as t


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c)
    ensure_feature_tables(c)          # pool_meta lives here
    return c


def _pool(c, pool, quote_side, net_sign):
    c.execute("INSERT INTO pool_meta(chain_id, pool, quote_side, resolved_at) "
              "VALUES('4663',?,?,?)", (pool, quote_side, "2026-07-25T00:00:00+00:00"))
    # one swap; positive amount1 = net buy for quote_side=1
    amt = 1000 * net_sign
    c.execute("INSERT INTO pool_ticks(chain_id, pool, kind, block_no, amount0, amount1, observed_at) "
              "VALUES('4663',?,?,1,?,?,?)",
              (pool, "v2_swap", str(500), str(amt), "2026-07-25T00:00:00+00:00"))


def test_breadth_off_when_most_pools_offered():
    c = _db()
    for i in range(6):
        _pool(c, f"p{i}", 1, net_sign=1 if i < 2 else -1)   # 2 of 6 positive
    c.commit()
    r = tape_market_breadth(c, "4663", window_s=300)
    assert r["active_pools"] == 6 and r["positive_pools"] == 2
    assert abs(r["breadth"] - 2 / 6) < 1e-9
    assert r["regime"] == "off"


def test_breadth_on_when_most_pools_bid():
    c = _db()
    for i in range(6):
        _pool(c, f"p{i}", 1, net_sign=1 if i < 4 else -1)   # 4 of 6 positive
    c.commit()
    r = tape_market_breadth(c, "4663", window_s=300)
    assert r["regime"] == "on" and r["positive_pools"] == 4


def test_breadth_unknown_when_too_thin():
    c = _db()
    _pool(c, "p0", 1, net_sign=1)
    _pool(c, "p1", 1, net_sign=1)
    c.commit()
    r = tape_market_breadth(c, "4663", window_s=300, min_active_pools=5)
    assert r["regime"] == "unknown" and r["breadth"] is None and r["active_pools"] == 2


def test_breadth_empty_chain_is_unknown():
    c = _db()
    r = tape_market_breadth(c, "4663")
    assert r["regime"] == "unknown" and r["active_pools"] == 0
