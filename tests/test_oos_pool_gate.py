"""WO-8A — the OOS pool gate that decides whether the 5m-signal confirmation may run."""
import sqlite3

from sniper.analysis.tick_features import ensure_feature_tables
from sniper.persistence import db_ticks as t
import scripts.research.oos_pool_gate as G


def _db():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c); ensure_feature_tables(c)
    return c


def _pool(c, pool, first_iso, n_blocks):
    from datetime import datetime, timedelta
    base = datetime.fromisoformat(first_iso)
    for b in range(n_blocks):
        c.execute("INSERT INTO pool_ticks(chain_id, pool, kind, block_no, amount0, amount1, "
                  "observed_at) VALUES('4663',?, 'v2_swap', ?, '1','1', ?)",
                  (pool, 1000 + b, (base + timedelta(seconds=b)).isoformat()))


def test_gate_counts_only_dense_postcutoff_pools(tmp_path):
    path = str(tmp_path / "t.db")
    c = sqlite3.connect(path); t.ensure_tick_tables(c); ensure_feature_tables(c)
    cutoff = "2026-07-24T20:06:10+00:00"
    _pool(c, "pre", "2026-07-24T19:00:00+00:00", 5)      # before cutoff -> not OOS
    _pool(c, "thin_after", "2026-07-24T21:00:00+00:00", 2)   # after but too thin
    _pool(c, "dense_after", "2026-07-24T22:00:00+00:00", 5)  # after + clears floor
    c.commit(); c.close()
    r = G.count_oos_pools(path, cutoff=cutoff, min_blocks=3)
    assert r["raw_oos_pools"] == 2                        # thin_after + dense_after
    assert r["qualifying_oos_pools"] == 1                 # only dense_after clears the floor


def test_gate_stops_below_threshold(tmp_path):
    path = str(tmp_path / "t2.db")
    c = sqlite3.connect(path); t.ensure_tick_tables(c); ensure_feature_tables(c)
    _pool(c, "dense_after", "2026-07-24T22:00:00+00:00", 5)
    c.commit(); c.close()
    r = G.count_oos_pools(path, cutoff="2026-07-24T20:06:10+00:00", min_blocks=3)
    assert r["qualifying_oos_pools"] == 1
    assert "STOP" in G.render(r)                          # 1 < 100 -> STOP
