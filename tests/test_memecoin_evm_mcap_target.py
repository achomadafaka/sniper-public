"""The operator's mcap take-profit must read a CURRENT market cap, not a stale snapshot.

CRCL (funded position 49849): operator target 670,000, peak derived mcap while open 4,188,152 --
crossed 6.25x -- yet only 10 of its 13,257 marks carried an mcap at all and the highest stored
value was 185,667. The resolver read the newest stored mark BEFORE deriving from the live price,
and a non-zero stale value short-circuited the derivation. The target could never be crossed and
the position was closed by hand.
"""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_real_runner import _resolve_override_and_mcap
from sniper.services import memecoin_exits as X


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _pos(**over):
    p = {"id": 49849, "mint": "0xcrcl", "network_id": "4663",
         "entry_price": 0.0005492778764423071, "entry_mcap": 532807.518428}
    p.update(over)
    return p


def test_a_live_price_beats_a_stale_stored_mcap():
    conn = _conn()
    conn.execute(
        "INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, mcap, source)"
        " VALUES('4663','0xcrcl','2026-09-04T02:00:00+00:00', 0.0006, 185667.03, 'geckoterminal')")
    # display_mid is the price the sweep just used; entry basis makes the derivation exact.
    peak_mid = 0.004317617397231255
    _o, mcap = _resolve_override_and_mcap(
        conn, _pos(), {}, peak_mid, {49849: None}, _Cfg())
    assert mcap is not None
    assert mcap > 4_000_000, mcap          # derived, not the 185,667 snapshot
    assert mcap >= 670_000                 # the operator's target is now crossable


def test_a_provider_market_cap_still_wins_outright():
    conn = _conn()
    _o, mcap = _resolve_override_and_mcap(
        conn, _pos(), {"market_cap": 63512.0}, 0.004, {49849: None}, _Cfg())
    assert mcap == 63512.0


def test_a_stale_mark_is_still_used_when_nothing_else_exists():
    # Fail-safe: a position with no entry basis keeps the snapshot rather than going blind.
    conn = _conn()
    conn.execute(
        "INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, mcap, source)"
        " VALUES('4663','0xcrcl','2026-09-04T02:00:00+00:00', 0.0006, 185667.03, 'geckoterminal')")
    _o, mcap = _resolve_override_and_mcap(
        conn, _pos(entry_mcap=0.0, entry_price=0.0), {}, 0.004, {49849: None}, _Cfg())
    assert mcap == 185667.03


def test_a_dexscreener_market_cap_reaches_the_marks_table():
    """DexScreener's key is `market_cap`; the EVM batch's is `mcap`. Reading only one stored NULL
    for every row the other produced."""
    conn = _conn()
    X.write_marks(conn, {("4663", "0xcrcl"): {
        "price": 0.004, "market_cap": 4_188_152.0, "source": "dexscreener"}},
        "2026-09-04T18:00:00+00:00")
    row = conn.execute("SELECT mcap, source FROM memecoin_position_marks").fetchone()
    assert row is not None and row["mcap"] == 4_188_152.0


class _Cfg:
    MEMECOIN_AUTO_TEMPLATES = "off"
