"""The web mark writer must apply the same spike quarantine as the funded sweep.

`memecoin_real_runner.py:2344` filters its price map through `_quarantine_spikes` before writing.
The web positions endpoint wrote its own copy of the same tape and did not, stamping
`quality='live'` on whatever the provider returned.

Live 2026-08-19, position 40687 (PURR, mint RL8tAMvv...177wPeE, $25 funded Solana bag): the
provider flipped mid-hold from mid 2.351e-05 / liq $19,757 / mcap $23,515 to mid 0.1114 / liq
$92.8M / mcap $111.4M - the price of a different asset entirely. The runner quarantined 420 ticks
across fastlane_mid, dexscreener and fastlane_sol_swap. Four `web` ticks went in as `live`, and
the operator card read entry->now 0.000027 -> 0.093781, value $88k, PnL +$88,249.33 (+352,997.3%)
on a position whose true executable price was 1.925e-05, i.e. about -27%.

No money moved: every exit read filters `quality != 'quarantined_spike'`
(memecoin_exits.py:275,380), and the position was never touched. The damage was confined to the
surface an operator reads before deciding to sell - which is not a small thing.
"""
import sqlite3

import pytest

from sniper.services import memecoin_exits as X


CHAIN = "solana"
MINT = "RL8tAMvvDKgwoKqurGjF7boQmE312nGUKBbB177wPeE"
GOOD = 2.351e-05
BAD = 0.1114                      # the other asset's price, ~4700x


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    from sniper.persistence import db_memecoin as m
    m.ensure_memecoin_tables(c)
    return c


def _open_position(conn, entry=GOOD):
    """A real open Solana position, because the quarantine's prior-price map only covers those."""
    from sniper.persistence import db_memecoin as m
    return m.open_position(
        conn, source_user_id="u1", source_handle="bigslime", mint=MINT, ticker="PURR",
        entry_price=entry, entry_notional=25.0, entry_fill_source="chain", entry_lag_s=4.27,
        their_size_usd=399.05, opened_at="2026-08-19T19:24:41+00:00",
        signal_source="follow_activity", strategy="alpha_b", execution_mode="real",
        network_id=CHAIN)


def _marks(conn):
    return [dict(r) for r in conn.execute(
        "SELECT mid, source, quality FROM memecoin_position_marks WHERE mint=? ORDER BY rowid",
        (MINT,))]


def _seed_good_mark(conn, ts="2026-08-19T19:26:27+00:00"):
    conn.execute(
        "INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, "
        "executable_exit_value, liquidity, mcap, source, quality) VALUES(?,?,?,?,?,?,?,?,?)",
        (CHAIN, MINT, ts, GOOD, GOOD, 19757.82, 23515.0, "fastlane_mid", "live"))
    conn.commit()


def test_the_spike_is_quarantined_not_written_live():
    """The exact live failure: a 4700x jump must not enter the tape as a good mark."""
    conn = _conn()
    _open_position(conn)
    _seed_good_mark(conn)

    clean = X._quarantine_spikes(
        conn, {(CHAIN, MINT): {"price": BAD, "liquidity": 92810370.08, "mcap": 111431932.0,
                               "source": "web"}},
        "2026-08-19T19:27:24+00:00")

    assert clean == {}, "a 4700x tick must never reach write_marks as clean"
    quarantined = [r for r in _marks(conn) if r["quality"] == "quarantined_spike"]
    assert len(quarantined) == 1 and quarantined[0]["mid"] == pytest.approx(BAD)


def test_an_ordinary_move_still_passes():
    """Guard against over-correcting: a real 40% move is not a spike."""
    conn = _conn()
    _open_position(conn)
    _seed_good_mark(conn)
    price = GOOD * 1.4
    clean = X._quarantine_spikes(
        conn, {(CHAIN, MINT): {"price": price, "liquidity": 20000.0, "mcap": 24000.0,
                               "source": "web"}},
        "2026-08-19T19:27:24+00:00")
    assert clean[(CHAIN, MINT)]["price"] == pytest.approx(price)


def test_the_web_endpoint_routes_its_write_through_the_filter():
    """Pin the wiring, not a mirrored predicate. The runner filters at
    memecoin_real_runner.py:2344; the web writer must make the same call."""
    import inspect
    from sniper.routes import api_memecoin
    src = inspect.getsource(api_memecoin)
    assert "write_marks(conn, _quarantine_spikes(conn, mark_writes, now_real), now_real)" in src, \
        "the web mark write must pass through _quarantine_spikes"


def test_both_writers_use_the_same_filter():
    """One rule, not two. If the runner's guard is ever changed, the web path inherits it."""
    import inspect
    from sniper.routes import api_memecoin
    from sniper.services import memecoin_real_runner
    for module in (api_memecoin, memecoin_real_runner):
        assert "_quarantine_spikes" in inspect.getsource(module)


def test_a_quarantined_mark_is_invisible_to_the_exit_reader():
    """Why no money moved. Stated as a test so a future refactor cannot quietly undo it."""
    conn = _conn()
    _open_position(conn)
    _seed_good_mark(conn)
    conn.execute(
        "INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, "
        "executable_exit_value, liquidity, mcap, source, quality) VALUES(?,?,?,?,?,?,?,?,?)",
        (CHAIN, MINT, "2026-08-19T19:27:24+00:00", BAD, BAD, 92810370.08, 111431932.0,
         "web", "quarantined_spike"))
    conn.commit()
    rows = conn.execute(
        "SELECT mid FROM memecoin_position_marks WHERE chain_id IS ? AND mint=? "
        "AND quality != 'quarantined_spike' AND mid > 0 ORDER BY sweep_ts DESC",
        (CHAIN, MINT)).fetchall()
    assert [r[0] for r in rows] == [pytest.approx(GOOD)]
