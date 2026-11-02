import sqlite3

import pytest
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct
from sniper.services import memecoin_runner as run


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); m.add_wallet(c, "u1", "alice"); return c


class _Ev:
    def __init__(self, side):
        self.side = side; self.source_user_id = "u1"; self.source_handle = "alice"
        self.mint = "MINT"; self.ticker = "PEPE"; self.their_price = 0.005
        self.their_market_cap = 1e6; self.their_size_usd = 3000.0; self.is_first_buy = True
        self.fomo_event_id = f"e-{side}"; self.created_at = "t"; self.received_at = "t"
        self.lag_s = 0.1; self.signal_source = "global_feed"


def test_buy_opens_one_position_per_arm(monkeypatch):
    c = _conn()
    feed = {"responseObject": {"feed": [
        {"id": "b1", "type": "large_buy", "userId": "u1", "tokenAddress": "MINT",
         "createdAt": "2026-07-07T10:00:00Z",
         "body": {"userHandle": "alice", "ticker": "PEPE", "price": 0.005,
                  "marketCap": "1000000", "currentSizeUsd": 3000.0, "isFirstBuy": True}}]}}

    def http(method, url, headers=None, body=None):
        return 200, feed, "Sun, 07 Jul 2026 10:00:01 GMT"

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"
        def refresh_token(self, http=None): return "t"

    from sniper.settings.memecoin import DEFAULT_MEMECOIN_ARMS
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ARMS", DEFAULT_MEMECOIN_ARMS)
    run.run_once(c, _Auth(), 100.0, http=http, fill_fn=lambda *a, **k: (0.005, "jupiter"))
    strats = {r[0] for r in c.execute("SELECT strategy FROM memecoin_positions WHERE is_open=1")}
    assert strats == {
        "control", "momentum_a", "alpha_b", "scalp_c", "scalp_c2", "trail_d", "peak_lock_e",
        "combo_f",
    }


def test_momentum_a_ignores_trader_sell_but_control_and_b_mirror():
    c = _conn()
    deep = lambda mint: {"price": 0.006, "liquidity": 1e15}   # deep liq -> ~0 slippage
    for s in ("control", "momentum_a", "alpha_b"):
        ct.on_copy_event(c, _Ev("buy"), 100.0, "t0",
                         fill_fn=lambda *a, **k: (0.005, "jupiter"), strategy=s)
    for s in ("control", "momentum_a", "alpha_b"):
        ct.on_copy_event(c, _Ev("sell"), 100.0, "t1", stats_fn=deep, strategy=s)
    open_now = {r[0] for r in c.execute("SELECT strategy FROM memecoin_positions WHERE is_open=1")}
    assert open_now == {"momentum_a"}


def test_mirror_exit_quotes_the_whole_remaining_bag():
    # Fill model v2: the mirror exit routes the position's FULL remaining qty through the
    # executable quote seam (impact lives in the route, not a modeled slippage curve), and the
    # returned unit price books as-is. entry $100 @ 0.001 -> qty 100000.
    c = _conn()
    ct.copy_event_to_arms(c, _Ev("buy"), 100.0, "t0", arms=("control",),
                          fill_fn=lambda *a, **k: (0.001, "jupiter"))
    quoted = []

    def exit_quote(mint, net, qty):
        quoted.append(qty)
        return 0.0098, "route_quote"

    ct.copy_event_to_arms(c, _Ev("sell"), 100.0, "t1", arms=("control",),
                          fill_fn=lambda *a, **k: (None, None), exit_quote_fn=exit_quote)
    assert quoted == [100000.0]           # whole-bag quote, not the entry bet
    px = c.execute("SELECT price FROM memecoin_fills WHERE kind='mirror_exit'").fetchone()[0]
    assert abs(px - 0.0098) < 1e-9        # the route price books unmodified


def test_entry_fill_shared_across_arms_one_quote_one_price():
    # REGRESSION (review Critical #1): a buy must resolve ONE fill for all arms — the exit is the
    # only variable. A per-arm fill would call fill_fn 3x and give different entry prices.
    c = _conn()
    calls = {"n": 0}

    def counting_fill(mint, size, side, tp):
        calls["n"] += 1
        return (0.005, "jupiter")

    ct.copy_event_to_arms(c, _Ev("buy"), 100.0, "t0",
                          arms=("control", "momentum_a", "alpha_b"), fill_fn=counting_fill)
    assert calls["n"] == 1                       # ONE quote for the event, not one per arm
    entries = {r[0] for r in c.execute("SELECT entry_price FROM memecoin_positions")}
    assert entries == {0.005}                    # all 3 arms opened at the identical entry


def test_momentum_a_skipped_on_slow_evm_chain_opens_on_solana():
    """momentum_a (15-min fast-ride) needs a fast price. UPDATED 2026-07-24: 4663 now OPENS because
    the tick tape prices it far faster than DexScreener (the pre-registered tape-vs-DexScreener
    cross-check PASSED: n=1768, median 1.78%, p90 12.62%). Chains with NO tape are still skipped."""
    c = _conn()
    evm = _Ev("buy"); evm.network_id = "4663"; evm.mint = "EVMMINT"; evm.fomo_event_id = "e-evm"
    out = ct.copy_event_to_arms(c, evm, 100.0, "t0", arms=("control", "momentum_a", "alpha_b"),
                                fill_fn=lambda *a, **k: (0.005, "jupiter"))
    assert out["momentum_a"] == "opened"          # 4663 is tape-priced now
    opened = {r[0] for r in c.execute(
        "SELECT strategy FROM memecoin_positions WHERE is_open=1 AND mint='EVMMINT'")}
    assert {"momentum_a", "control", "alpha_b"} <= opened

    # a chain with no tape is still refused
    noTape = _Ev("buy"); noTape.network_id = "8453"; noTape.mint = "BASEMINT"; noTape.fomo_event_id = "e-base"
    out2 = ct.copy_event_to_arms(c, noTape, 100.0, "t0b", arms=("control", "momentum_a", "alpha_b"),
                                 fill_fn=lambda *a, **k: (0.005, "jupiter"))
    assert out2["momentum_a"] == "skipped_slow_chain"

    sol = _Ev("buy"); sol.network_id = "solana"; sol.mint = "SOLMINT"; sol.fomo_event_id = "e-sol"
    ct.copy_event_to_arms(c, sol, 100.0, "t1", arms=("control", "momentum_a", "alpha_b"),
                          fill_fn=lambda *a, **k: (0.005, "jupiter"))
    sol_open = {r[0] for r in c.execute(
        "SELECT strategy FROM memecoin_positions WHERE is_open=1 AND mint='SOLMINT'")}
    assert "momentum_a" in sol_open   # Solana fast feed -> momentum_a runs normally


def test_chase_skip_shared_across_arms():
    # a chase-skip on the shared fill skips ALL arms (not just one)
    c = _conn()
    ev = _Ev("buy"); ev.their_price = 0.001      # our fill 0.005 is >15% over -> chase
    out = ct.copy_event_to_arms(c, ev, 100.0, "t0", arms=("control", "momentum_a", "alpha_b"),
                                fill_fn=lambda *a, **k: (0.005, "jupiter"))
    assert set(out.values()) == {"skipped_chase_over_funded_cap"}
    assert c.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0


# --- mirror bad-tick guard (2026-07-23, Jimothy): a wrong-denominated mid (>=10x the position's
#     own prior knowledge) must never book a mirror fill; +$6.0M of phantom paper PnL and a
#     poisoned arming stat came from exactly this hole (sweep quarantine never sees mirrors). ---

def test_mirror_close_quarantines_spike_mid(monkeypatch):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_copy_trader as ct

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="M", ticker="T",
                          entry_price=0.001, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0,
                          opened_at="2026-07-23T10:00:00+00:00", strategy="control")
    m.insert_fill(c, position_id=pid, kind="entry", price=0.001, qty=100000.0, notional=100.0,
                  fill_source="j", fraction=1.0, created_at="2026-07-23T10:00:00+00:00")
    m.update_position_last_quote(c, pid, 0.0012, "2026-07-23T10:05:00+00:00")

    class _Sell:
        source_user_id = "u1"; mint = "M"; network_id = "solana"; their_price = 0.0011

    # the route quote returns a 4,000x glitch price -> quarantined, booked as a censored
    # NOQUOTE close at entry (never the glitch, never a substitute mid/their_price)
    r = ct._mirror_close_arm(c, _Sell(), "2026-07-23T10:06:00+00:00", "control",
                             quote_fn=lambda mint, net, qty: (4.0, "route_quote"))
    assert r == "exit"
    pos = m.get_position(c, pid)
    assert pos["is_open"] == 0
    assert pos["exit_reason"] == "mirror_noquote"
    # UPDATED 2026-08-29: a quarantined spike leaves no usable quote, so nothing was observed.
    assert pos["realized_pnl"] is None
    fill = c.execute("SELECT price, fill_source FROM memecoin_fills WHERE position_id=? "
                     "AND kind='mirror_exit'", (pid,)).fetchone()
    assert fill["fill_source"] == "noquote" and fill["price"] < 0.01


def test_mirror_close_legit_10x_winner_books_normally(monkeypatch):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_copy_trader as ct

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="M", ticker="T",
                          entry_price=0.001, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0,
                          opened_at="2026-07-23T10:00:00+00:00", strategy="control")
    m.insert_fill(c, position_id=pid, kind="entry", price=0.001, qty=100000.0, notional=100.0,
                  fill_source="j", fraction=1.0, created_at="2026-07-23T10:00:00+00:00")
    # the position's own tape SAW the run: peak tracked at 0.009 -> a 0.008 mirror is plausible
    m.update_position_peak(c, pid, 0.009, "2026-07-23T10:05:00+00:00", 0)
    m.update_position_last_quote(c, pid, 0.0085, "2026-07-23T10:05:30+00:00")

    class _Sell:
        source_user_id = "u1"; mint = "M"; network_id = "solana"; their_price = 0.008

    r = ct._mirror_close_arm(c, _Sell(), "2026-07-23T10:06:00+00:00", "control",
                             quote_fn=lambda mint, net, qty: (0.008, "route_quote"))
    assert r == "exit"
    pos = m.get_position(c, pid)
    assert pos["realized_pnl"] > 500.0            # ~8x winner books as a winner


# --- the write lock (2026-09-05) ---------------------------------------------------------------
# "paper fan-out failed after funded hook" fired 23 times on 2026-09-05; 5 ended in
# `database is locked`, codes 5 and one 517 (SQLITE_BUSY_SNAPSHOT). Funded entries were unaffected
# because the funded hook runs first, but the paper arms lost those positions.
#
# `SAVEPOINT paper_entry` with no transaction open begins a DEFERRED one, and `open_position`
# SELECTs before it INSERTs, which pins a read snapshot. SQLite does NOT call the busy handler when
# a read transaction upgrades to a write, so a concurrent writer gives SQLITE_BUSY immediately and
# the 60s busy_timeout never applies.

def _file_ledger(tmp_path):
    path = tmp_path / "ledger.db"
    setup = sqlite3.connect(str(path))
    setup.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(setup)
    m.add_wallet(setup, "u1", "alice")
    setup.commit()
    setup.close()
    return str(path)


def _open(path, cross_thread=False):
    c = sqlite3.connect(path, timeout=5.0, check_same_thread=not cross_thread and True or False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c


def test_a_held_write_lock_costs_the_arm_a_wait_and_not_its_position(tmp_path):
    """A rival writer holding the lock must delay the entry, never lose it.

    THIS TEST PASSES ON THE UNFIXED CODE TOO, and that is worth saying rather than leaving as an
    implied guarantee: a rival that holds the lock from BEFORE the savepoint does not reproduce the
    bug, because the deferred read is taken while the rival is still uncommitted. The 517 needs the
    rival to commit BETWEEN the analytics SELECT and the INSERT, and forcing that ordering needs a
    trace hook -- which then deadlocks the FIXED version, because it blocks at BEGIN IMMEDIATE and
    never reaches the SELECT the hook fires on. So this is a smoke test that the fix does not break
    concurrent operation; `test_the_write_lock_is_taken_before_the_savepoint` is the one that fails
    on the unfixed code and is therefore the real regression guard.

    `SAVEPOINT paper_entry` on a connection with no transaction begins a DEFERRED one, and
    `open_position` SELECTs before it INSERTs, which pins a read snapshot. SQLite does NOT call the
    busy handler when a read transaction upgrades to a write, so a rival gives SQLITE_BUSY (5) or,
    if it committed since the snapshot, SQLITE_BUSY_SNAPSHOT (517) -- immediately, with the 60s
    `busy_timeout` never applying. Measured in isolation: 517 in 0.000s. On 2026-09-05 that cost 5
    of 23 "paper fan-out failed" events their positions.

    `BEGIN IMMEDIATE` takes the write lock first and DOES wait, so the rival's 0.3s hold becomes a
    0.3s delay rather than a lost arm.
    """
    import threading
    path = _file_ledger(tmp_path)
    a = _open(path)
    a.execute("PRAGMA journal_mode=WAL")
    rival = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
    rival.execute("PRAGMA busy_timeout=5000")
    rival.execute("BEGIN IMMEDIATE")
    rival.execute("INSERT INTO memecoin_wallets (source_user_id, handle) VALUES (?,?)",
                  ("u-rival", "rival"))
    threading.Timer(0.3, rival.commit).start()

    try:
        out = ct.copy_event_to_arms(a, _Ev("buy"), 100.0, "t0", arms=("control",),
                                    fill_fn=lambda *x, **k: (0.005, "jupiter"))
    finally:
        try:
            rival.close()
        except Exception:
            pass

    assert out.get("control") == "opened", out
    assert a.execute("SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1").fetchone()[0] == 1
    a.close()


def test_a_failed_entry_does_not_carry_the_write_lock_out_of_the_function(tmp_path):
    """`_open_arm` continues into network calls; a held write lock blocks every other writer.

    Guards the two rollback paths added with BEGIN IMMEDIATE. It does not fail on the unfixed code
    (which never took the lock in the first place), so it is a forward guard rather than a
    reproduction: it is what breaks if someone later removes the `began` rollbacks.
    """
    path = _file_ledger(tmp_path)
    a = _open(path)
    a.execute("PRAGMA journal_mode=WAL")

    def _boom(*_a, **_k):
        raise RuntimeError("entry blew up after the savepoint")
    original = m.insert_fill
    m.insert_fill = _boom
    try:
        with pytest.raises(RuntimeError):
            ct.copy_event_to_arms(a, _Ev("buy"), 100.0, "t0", arms=("control",),
                                  fill_fn=lambda *x, **k: (0.005, "jupiter"))
    finally:
        m.insert_fill = original

    assert not a.in_transaction, "the connection must not still hold the write lock"
    a.close()


def test_the_write_lock_is_taken_before_the_savepoint(tmp_path):
    """BEGIN IMMEDIATE waits on the busy handler; a DEFERRED upgrade does not."""
    path = _file_ledger(tmp_path)
    a = _open(path)
    seen = []
    a.set_trace_callback(lambda sql: seen.append(" ".join(str(sql).split()).upper()))

    ct.copy_event_to_arms(a, _Ev("buy"), 100.0, "t0", arms=("control",),
                          fill_fn=lambda *x, **k: (0.005, "jupiter"))
    a.set_trace_callback(None)
    a.close()

    begins = [i for i, s in enumerate(seen) if s.startswith("BEGIN IMMEDIATE")]
    saves = [i for i, s in enumerate(seen) if s.startswith("SAVEPOINT PAPER_ENTRY")]
    assert begins and saves, seen[:20]
    assert begins[0] < saves[0], "the write lock must be held before the savepoint opens"
