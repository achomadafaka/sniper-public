"""The multihop's PAIR lookup seeks one composite index (Alpha GO 2026-10-17).

`_stored_pair_pools(chain, a, b)` filters both currencies, but only one-sided expression indexes
existed, so SQLite walked the busier side: ETH/USDG on 4663 read 48,155 USDG rows to return 357,
49.7 ms p50 on the live ledger, inside `v4_multihop:bridge_options` on the entry quote path. The
composite (network_id, pool_manager, currency0, currency1) index measured 41.6 -> 0.22 ms on a
965k-row copy. Assert the PLAN, so a drifted expression cannot quietly bring the walk back.
"""
import json
import sqlite3

from sniper.execution import evm_v4_multihop as mh
from sniper.execution.evm_v4_quote import V4
from sniper.persistence import db_memecoin as dbm

CHAIN = "4663"
ETH = "0x0000000000000000000000000000000000000000"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"


def _key(c0, c1, fee=3000):
    return {"currency0": c0, "currency1": c1, "fee": fee, "tick_spacing": 60, "hooks": ETH}


def _spy(monkeypatch, conn):
    plans = []

    class _Spy:
        def execute(self, sql, params=()):
            plans.extend(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
            return conn.execute(sql, params)

        def close(self):
            pass
    monkeypatch.setattr("sniper.persistence.db_core.get_readonly_connection", lambda *a, **k: _Spy())
    return plans


def _ledger():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbm.ensure_memecoin_tables(conn)
    pm = V4[CHAIN]["pool_manager"]
    rows = [("0x%064x" % i, _key(ETH, USDG.upper().replace("0X", "0x"))) for i in range(3)]
    rows += [("0x%064x" % (100 + i), _key("0x" + "%040x" % (i + 1), USDG)) for i in range(50)]
    conn.executemany(
        "INSERT INTO memecoin_evm_v4_pool_keys (network_id, pool_id, pool_manager, key_json, verified_at)"
        " VALUES (?,?,?,?,?)", [(CHAIN, pid, pm, json.dumps(k), "2026-10-17") for pid, k in rows])
    conn.commit()
    return conn


def test_the_pair_lookup_seeks_the_composite_index_on_all_four_terms(monkeypatch):
    conn = _ledger()
    plans = _spy(monkeypatch, conn)
    got = mh._stored_pair_pools(CHAIN, USDG, ETH)              # either order, mixed case
    assert sorted(pid for pid, _key in got) == ["0x%064x" % i for i in range(3)]
    assert plans and not any("SCAN" in p for p in plans), plans
    assert any("idx_mc_v4_key_pair" in p and p.count("<expr>=?") == 2 for p in plans), plans


def test_ensure_is_idempotent_and_the_mint_lookup_keeps_its_two_indexes(monkeypatch):
    """The coin lookup's UNION still seeks the one-sided indexes it was written for."""
    conn = _ledger()
    dbm.ensure_memecoin_tables(conn)                          # a second ensure is a no-op
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memecoin_evm_v4_pool_keys'")}
    assert {"idx_mc_v4_key_currency0", "idx_mc_v4_key_currency1", "idx_mc_v4_key_pair"} <= names
    from sniper.execution import evm_v4_quote as V
    plans = _spy(monkeypatch, conn)
    V._stored_pools_for_mint(4663, USDG)
    assert plans and not any("SCAN" in p for p in plans), plans
    assert sum(("idx_mc_v4_key_currency" in p or "idx_mc_v4_key_pair" in p) and "<expr>=?" in p
               for p in plans) == 2, plans                   # one seek per UNION side
