"""Pre-entry flow OBSERVATION (2026-08-25).

This runs on the funded entry path and decides nothing. Its whole contract is that it can never
hurt an entry: a missing tape, a missing pool, a broken DB, or a raising connection must all return
None quietly. Those are the tests that matter."""
import sqlite3

from sniper.services.memecoin_solana_tape import observe_pre_entry_flow

NOW = "2026-08-25T12:00:00+00:00"


def _tape_with(mint="M", pool="P", swaps=True):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE sol_pool_vaults (pool TEXT, mint TEXT, coin_vault TEXT, "
              "quote_vault TEXT, quote_mint TEXT, resolved_at TEXT)")
    c.execute("CREATE TABLE pool_ticks (id INTEGER PRIMARY KEY, chain_id TEXT, pool TEXT, "
              "kind TEXT, amount0 TEXT, amount1 TEXT, maker TEXT, observed_at TEXT)")
    c.execute("INSERT INTO sol_pool_vaults VALUES (?,?,'','','', '2026-08-25T11:00:00+00:00')",
              (pool, mint))
    c.execute("INSERT INTO pool_ticks (chain_id,pool,kind,amount0,amount1,maker,observed_at) "
              "VALUES ('solana',?, 'sol_sync','0','5000','x','2026-08-25T11:58:00+00:00')", (pool,))
    if swaps:
        for i, m in enumerate(("a", "b", "c")):
            c.execute("INSERT INTO pool_ticks (chain_id,pool,kind,amount0,amount1,maker,observed_at)"
                      " VALUES ('solana',?, 'sol_swap','0','-100',?, ?)",
                      (pool, m, "2026-08-25T11:59:0%d+00:00" % i))
    return c


def test_returns_the_observation_for_a_resolved_pool():
    c = _tape_with()
    got = observe_pre_entry_flow("M", now_iso=NOW, connect=lambda: c)
    assert got["swaps_n"] == 3 and got["makers_n"] == 3
    assert got["turnover"] == 300 / 5000


def test_unresolved_pool_is_none_not_zero():
    c = _tape_with(mint="OTHER")
    assert observe_pre_entry_flow("M", now_iso=NOW, connect=lambda: c) is None


def test_resolved_pool_with_no_swap_in_window_is_none():
    c = _tape_with(swaps=False)
    assert observe_pre_entry_flow("M", now_iso=NOW, connect=lambda: c) is None


def test_window_excludes_older_swaps():
    c = _tape_with()
    # a 1-second window cannot reach swaps from a minute earlier
    assert observe_pre_entry_flow("M", window_s=1, now_iso=NOW, connect=lambda: c) is None


def test_a_raising_connection_never_propagates():
    def boom():
        raise RuntimeError("tape is on fire")
    assert observe_pre_entry_flow("M", now_iso=NOW, connect=boom) is None


def test_a_broken_schema_never_propagates():
    c = sqlite3.connect(":memory:")           # no tables at all
    assert observe_pre_entry_flow("M", now_iso=NOW, connect=lambda: c) is None


def test_missing_tape_file_never_propagates():
    assert observe_pre_entry_flow("M", now_iso=NOW, tape_path="/nonexistent/nope.db") is None


def test_clip_share_is_reported_when_a_clip_is_given():
    c = _tape_with()
    got = observe_pre_entry_flow("M", now_iso=NOW, clip_quote=30, connect=lambda: c)
    assert got["clip_share"] == 30 / 300


def test_every_outcome_is_counted_so_a_dead_observer_is_visible():
    """The reason this exists: the hook shipped live, recorded zero for its whole life, and
    nothing said so. A swallowed exception with no counter is indistinguishable from a quiet
    market."""
    from sniper.services import memecoin_solana_tape as T
    before = T.observe_counts_snapshot()

    observe_pre_entry_flow("M", now_iso=NOW, connect=lambda: _tape_with())          # written
    observe_pre_entry_flow("M", now_iso=NOW, connect=lambda: _tape_with(mint="X"))  # no_pool
    observe_pre_entry_flow("M", now_iso=NOW, connect=lambda: _tape_with(swaps=False))  # no_swaps

    def boom():
        raise RuntimeError("x")
    observe_pre_entry_flow("M", now_iso=NOW, connect=boom)                          # error

    after = T.observe_counts_snapshot()
    assert after["calls"] - before["calls"] == 4
    assert after["written"] - before["written"] == 1
    assert after["no_pool"] - before["no_pool"] == 1
    assert after["no_swaps"] - before["no_swaps"] == 1
    assert after["error"] - before["error"] == 1
