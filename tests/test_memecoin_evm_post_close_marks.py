"""EVM post-close marks: the minutes after a stop, off the on-chain swap tape.

Codex recheck N13 second half. Solana keeps printing marks after a close because the swap-mark
lane feeds off its vault book; a 4663/8453 position gets nothing, because EVM marks come from the
exact balance-based exit preview and the balance is gone. The tape has the pool's post-close path,
so this lane reads it -- observation only, never a decision.

THE SERIES IS A RATIO ANCHORED ON OUR OWN EXIT, NOT A PRICE DERIVED FROM DECIMALS. Of the 179
pools bound to funded positions on 2026-09-23, both token decimals resolve for SIX; without them
`sqrt_price_to_quote_per_token` yields an atomic ratio, which must never be written into `mid`.
The unknown 10**(base-quote) factor cancels in `ratio_t / ratio_anchor`, so multiplying by the
realized exit price gives a real USD series on every pool with a known quote side (163 of 179).
"""
import sqlite3

import pytest

from sniper.services import memecoin_evm_post_close_marks as P
from sniper.persistence import db_memecoin as m

CHAIN = "4663"
MINT = "0x" + "cc" * 20
POOL = "0x" + "aa" * 32
SIG = "0x" + "df" * 32
CLOSED_AT = "2026-09-23T20:46:44.209108+00:00"


def _ledger():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint=MINT, ticker="T",
                          entry_price=4.776e-05, entry_notional=50.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-09-23T20:42:40+00:00",
                          network_id=CHAIN)
    c.execute("UPDATE memecoin_positions SET execution_mode='real', is_open=0, closed_at=?,"
              " close_sig=? WHERE id=?", (CLOSED_AT, SIG, pid))
    c.execute("INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, tx_sig,"
              " network_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
              (pid, "bracket_exit", 4.768444810376199e-05, 1046819.677839431, 49.917, SIG,
               CHAIN, CLOSED_AT))
    c.commit()
    return c, pid


_STORE_SEQ = [0]


def _stores(tmp_path, *, ticks, bind=True, quote_side=0, pool_meta=True, canonical=False):
    # A fresh pair per call: a test that builds two tapes must not re-CREATE the first one's tables.
    _STORE_SEQ[0] += 1
    root = tmp_path / ("store-%d" % _STORE_SEQ[0])
    root.mkdir()
    bind_path = str(root / "ticks.db")
    tape_path = str(root / "volume.db")
    b = sqlite3.connect(bind_path)
    b.execute("CREATE TABLE evm_position_pools (position_id INTEGER, chain_id TEXT, mint TEXT,"
              " pool TEXT, venue_kind TEXT, resolution_tx TEXT, resolution_log_index INTEGER,"
              " resolved_at TEXT)")
    if bind:
        b.execute("INSERT INTO evm_position_pools(position_id, chain_id, mint, pool) VALUES(?,?,?,?)",
                  (1, CHAIN, MINT, POOL))
    b.commit(); b.close()
    v = sqlite3.connect(tape_path)
    v.execute("CREATE TABLE pool_ticks (id INTEGER PRIMARY KEY, chain_id TEXT, pool TEXT,"
              " kind TEXT, block_no INTEGER, tx_sig TEXT, log_index INTEGER, maker TEXT,"
              " amount0 TEXT, amount1 TEXT, observed_at TEXT, tick INTEGER, tick_lower INTEGER,"
              " tick_upper INTEGER, liquidity TEXT, sqrt_price_x96 TEXT, block_hash TEXT)")
    v.execute("CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT,"
              " quote_side INTEGER, resolved_at TEXT, token0_decimals INTEGER,"
              " token1_decimals INTEGER, resolve_state TEXT)")
    if canonical:
        v.execute("CREATE TABLE block_times (chain_id TEXT, block_no INTEGER, block_ts TEXT,"
                  " fetched_at TEXT, block_hash TEXT, PRIMARY KEY(chain_id,block_no))")
    if pool_meta:
        v.execute("INSERT INTO pool_meta(chain_id, pool, token0, token1, quote_side,"
                  " token0_decimals, token1_decimals) VALUES(?,?,?,?,?,?,?)",
                  (CHAIN, POOL, "0x" + "11" * 20, MINT, quote_side, 18, None))
    for i, (sig, at, sqrt_p) in enumerate(ticks, start=1):
        v.execute("INSERT INTO pool_ticks(id, chain_id, pool, kind, tx_sig, observed_at,"
                  " sqrt_price_x96) VALUES(?,?,?,?,?,?,?)",
                  (i, CHAIN, POOL, "v3_swap", sig, at, str(sqrt_p)))
        if canonical:
            v.execute("UPDATE pool_ticks SET block_no=?,log_index=?,block_hash=? WHERE id=?",
                      (100+i, i, f"hash{100+i}", i))
            v.execute("INSERT INTO block_times VALUES(?,?,?,?,?)",
                      (CHAIN, 100+i, at, at, f"hash{100+i}"))
    v.commit(); v.close()
    return tape_path, bind_path


def _marks(tmp_path, conn, **kw):
    ticks = kw.pop("ticks", [
        (SIG, CLOSED_AT, 584378033656737499161238777404864),                       # our own exit
        ("0x" + "11" * 32, "2026-09-23T20:46:49.374000+00:00", 584378033656737499161238777404864 // 2),
    ])
    tape_path, bind_path = _stores(tmp_path, ticks=ticks, canonical=True, **kw)
    return P.evm_post_close_marks(conn, now_iso="2026-09-23T20:47:00+00:00",
                                  tape_path=tape_path, bind_path=bind_path)


def test_the_series_is_anchored_on_our_own_exit_transaction(tmp_path):
    conn, _pid = _ledger()
    marks = _marks(tmp_path, conn)
    assert list(marks) == [(CHAIN, MINT)]
    entry = marks[(CHAIN, MINT)]
    # sqrt halved -> ratio quartered on quote_side=0 (the reciprocal side), so the mark is 4x the
    # exit price; the point is that it is OUR exit price times a pure ratio, no decimals involved.
    assert entry["price"] == pytest.approx(4.768444810376199e-05 * 4.0, rel=1e-6)
    assert entry["observed_at"] == "2026-09-23T20:46:49.374000+00:00"
    assert entry["source"] == P.SOURCE
    assert "post_swap_anchor" in P.SOURCE, "the anchor bias must be visible in the label itself"


def test_no_tick_from_our_exit_falls_back_to_the_first_tick_after_the_close(tmp_path):
    conn, _pid = _ledger()
    marks = _marks(tmp_path, conn, ticks=[
        ("0x" + "22" * 32, "2026-09-23T20:46:45.000000+00:00", 10 ** 30),
        ("0x" + "33" * 32, "2026-09-23T20:46:50.000000+00:00", 10 ** 30),
    ])
    # anchor and latest are the same price, so the series starts exactly at the realized exit
    assert marks[(CHAIN, MINT)]["price"] == pytest.approx(4.768444810376199e-05, rel=1e-9)


def test_an_unbound_position_produces_nothing(tmp_path):
    conn, _pid = _ledger()
    assert _marks(tmp_path, conn, bind=False) == {}


def test_an_unknown_quote_side_produces_nothing(tmp_path):
    conn, _pid = _ledger()
    assert _marks(tmp_path, conn, quote_side=None) == {}
    assert _marks(tmp_path, conn, pool_meta=False) == {}


def test_a_pool_with_no_tick_in_the_window_produces_nothing_not_a_zero(tmp_path):
    conn, _pid = _ledger()
    marks = _marks(tmp_path, conn, ticks=[
        ("0x" + "44" * 32, "2026-09-23T20:40:00.000000+00:00", 10 ** 30),          # before the close
    ])
    assert marks == {}, "a mark of 0.0 would read as a measured free coin"


def test_a_post_close_entry_carries_no_market_cap(tmp_path):
    """INVARIANT, not a regression: the post-close entry must never carry a market cap.

    The funded sweep's mcap fallback (memecoin_real_runner.py, `SELECT mcap FROM
    memecoin_position_marks WHERE chain_id=? AND mint=? AND mcap>0 ...`) keys on the COIN, not the
    position, so a re-entered coin would read its former bag's post-close mcap there the day one of
    these rows carried one. Today `write_marks` stores `mcap or market_cap`, both absent here, so the
    row is NULL and `mcap>0` can never select it. This pins that: adding either key fails here
    before it can reach a funded decision.
    """
    conn, _pid = _ledger()
    marks = _marks(tmp_path, conn)
    assert marks, "the fixture must produce an entry, or this invariant checks nothing"
    for entry in marks.values():
        assert not {"mcap", "market_cap"} & set(entry), entry
