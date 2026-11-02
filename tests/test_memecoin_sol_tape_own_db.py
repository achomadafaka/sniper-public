"""Own-DB split (2026-07-24): the Solana pull tape must never be a second writer on the funded
tick tape (`data/memecoin_ticks.db`) — that two-writer WAL lock incident got the exit-state worker
disabled. `connect_sol_tape()` opens a wholly separate file; the Solana writer and the materializer's
Solana reads both route through it, while the EVM tickstream / materializer keep using the shared
ticks DB untouched.
"""
import inspect
import sqlite3

from sniper.persistence import db_ticks as t
from sniper.services import memecoin_solana_amounts as sa
from sniper.services import memecoin_solana_tape as st
from sniper.services.memecoin_exit_materializer import materialize_open_positions

COIN = "CoinMint111111111111111111111111111111111111"
POOL_OWNER = "PoolAuth111111111111111111111111111111111111"
COIN_VAULT = "CoinVault11111111111111111111111111111111111"
WSOL_VAULT = "WsolVault11111111111111111111111111111111111"


def _tx(entries_pre, entries_post, payer="Payer111111111111111111111111111111111111111"):
    addrs, seen = [payer], {payer}
    for (_m, _o, a, _amt) in entries_pre + entries_post:
        if a not in seen:
            addrs.append(a); seen.add(a)
    def bals(entries):
        return [{"mint": m, "owner": o, "accountIndex": addrs.index(a),
                 "uiTokenAmount": {"amount": str(amt)}} for (m, o, a, amt) in entries]
    return {"transaction": {"message": {"accountKeys": [{"pubkey": a} for a in addrs]}},
            "meta": {"preTokenBalances": bals(entries_pre), "postTokenBalances": bals(entries_post)}}


def _ticks_db():
    """The SHARED ticks DB (EVM tickstream's territory). `pool_meta` is included because production
    gets it from a separate cron (`tick_feature_job.py`, not this task's concern) — present but with
    no Solana rows, matching `test_exit_materializer.py`'s fixture shape."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c)
    t.ensure_block_times(c)
    from sniper.analysis.tick_features import ensure_feature_tables
    ensure_feature_tables(c)
    return c


def _sol_tape_db():
    """The Solana tape's OWN DB, built the way `connect_sol_tape()` builds a real one (same
    ensure_* calls), just on an in-memory file so tests never touch disk."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c)
    t.ensure_block_times(c)
    from sniper.analysis.tick_features import ensure_feature_tables
    ensure_feature_tables(c)
    return c


def _ledger_one_solana_pos(mint):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE memecoin_positions (
        id INTEGER PRIMARY KEY, is_open INTEGER, execution_mode TEXT, network_id TEXT, mint TEXT,
        quote_route_target TEXT, entry_price REAL, executable_peak_price REAL,
        funded_contract_version INTEGER, opened_at TEXT)""")
    c.execute("INSERT INTO memecoin_positions VALUES "
              "(9,1,'real','solana',?, 'ROUTER-IGNORED',1.0,1.2,NULL,"
              "'2026-07-24T00:00:00+00:00')",
              (mint,))
    c.commit()
    return c


def test_connect_sol_tape_is_a_distinct_file_with_the_full_schema(tmp_path, monkeypatch):
    path = tmp_path / "sol_tape.db"
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_DB", str(path))
    assert t.sol_tape_db_path() == str(path)
    c = t.connect_sol_tape()
    try:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"pool_ticks", "block_times", "pool_meta"} <= tables
    finally:
        c.close()
    assert path.exists()                                   # its OWN file, not the ticks db's


def test_solana_write_pass_lands_in_the_sol_db_and_never_in_the_ticks_db():
    """The core regression: solana_tape_pass must write pool_ticks/block_times/sol_sig_cursor/
    pool_meta ONLY into the sol-tape connection, never into the shared ticks connection."""
    ticks_tc = _ticks_db()
    sol_tc = _sol_tape_db()
    mconn = _ledger_one_solana_pos(COIN)

    resolve_tx = _tx([(COIN, POOL_OWNER, COIN_VAULT, 1000), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 500)],
                      [(COIN, POOL_OWNER, COIN_VAULT, 1000), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 500)],
                      payer="3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH")
    swap_tx = _tx([(COIN, POOL_OWNER, COIN_VAULT, 1000), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 500)],
                  [(COIN, POOL_OWNER, COIN_VAULT, 900), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 560)])
    swap_tx["blockTime"] = 1_700_000_000

    # own entry fill (fastlane needs a memecoin_fills row to resolve the pool the first time)
    mconn.execute("CREATE TABLE memecoin_fills (position_id INTEGER, kind TEXT, tx_sig TEXT, "
                  "id INTEGER PRIMARY KEY)")
    mconn.execute("INSERT INTO memecoin_fills VALUES (9, 'entry', 'entrysig', 1)")
    mconn.commit()

    def get_tx_fn(sig):
        return resolve_tx if sig == "entrysig" else swap_tx

    def get_sigs_fn(_vault, params):
        return [] if params.get("until") == "sig1" else [{"signature": "sig1", "slot": 42}]

    st.solana_tape_pass(mconn, sol_tc, get_sigs_fn, get_tx_fn, "2026-07-24T00:00:00+00:00", tx_budget=10)

    # written into the SOL tape DB
    assert sol_tc.execute("SELECT COUNT(*) FROM pool_ticks").fetchone()[0] > 0
    assert sol_tc.execute("SELECT COUNT(*) FROM sol_sig_cursor").fetchone()[0] == 1
    assert sol_tc.execute("SELECT COUNT(*) FROM pool_meta WHERE chain_id='solana'").fetchone()[0] == 1

    # NOTHING landed in the shared ticks DB — the funded EVM tickstream's territory, untouched
    assert ticks_tc.execute("SELECT COUNT(*) FROM pool_ticks").fetchone()[0] == 0
    assert ticks_tc.execute("SELECT COUNT(*) FROM pool_meta").fetchone()[0] == 0


def test_materializer_reads_solana_state_from_the_sol_db_end_to_end():
    """Reuses the vault-keyed tape fixtures: seed a real Solana state via `tape_pass_for_vault` into
    a SEPARATE sol_tc, then confirm `materialize_open_positions(..., sol_tc=sol_tc)` produces a
    TRUSTED (not source_untrusted) snapshot by reading the pool from sol_tc, not from tc."""
    ticks_tc = _ticks_db()
    sol_tc = _sol_tape_db()
    mconn = _ledger_one_solana_pos(COIN)

    sol_tc.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, quote_side, resolved_at) "
                   "VALUES ('solana', ?, ?, ?, 1, ?)", (COIN_VAULT, COIN, sa.WSOL, "2026-01-01T00:00:00Z"))
    sol_tc.commit()

    ok_tx = _tx([(COIN, POOL_OWNER, COIN_VAULT, 1000), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 500)],
                [(COIN, POOL_OWNER, COIN_VAULT, 900), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 560)])
    ok_tx["blockTime"] = 1_700_000_000
    sigs = [{"signature": "sig1", "slot": 10}]
    st.tape_pass_for_vault(sol_tc, COIN, COIN_VAULT, lambda a, p: sigs if not p.get("until") else [],
                           lambda s: ok_tx, "2026-07-24T00:00:00+00:00", tx_budget=10)

    # sanity: nothing about this coin exists in the bare shared ticks DB
    assert ticks_tc.execute("SELECT COUNT(*) FROM pool_meta").fetchone()[0] == 0

    n = materialize_open_positions(mconn, ticks_tc, 1_700_000_100, sol_tc=sol_tc)
    assert n == 1
    from sniper.persistence import db_exit_state as xs
    snap = xs.latest_state(ticks_tc, 9)
    assert snap["state"] != "source_untrusted"          # resolved via sol_tc, not silently untrusted

    # without sol_tc (old call shape) the SAME bare tc still yields untrusted — proves the routing,
    # not a coincidental pass. A distinct now_ts avoids the (position_id, computed_at) dedup key.
    n2 = materialize_open_positions(mconn, ticks_tc, 1_700_000_110)
    assert xs.latest_state(ticks_tc, 9)["state"] == "source_untrusted"
    assert n2 == 1


def test_sol_tape_db_is_in_the_wal_checkpoint_list():
    from scripts import wal_checkpoint as w
    src = inspect.getsource(w.main)
    assert "sol_tape_db_path" in src   # resolver, not a literal path (P2: state-root/env aware)
