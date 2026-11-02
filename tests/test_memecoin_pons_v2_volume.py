"""Pons v2 curve trades reach the on-chain m5 volume the gate reads.

MRLN (4663, 2026-09-23 13:02:39.529Z) is the acceptance case. The gate summed $0.11 over one
Uniswap pool holding $2.51 and refused the coin against a $259 floor, while GMGN read $3,721 of
five-minute volume and $8,186 of liquidity on `pons_v2`. The tape held no row for the source
transaction at all, because Pons v2 is a launchpad curve this subscription did not ingest.

Two identifiers disagree on purpose and the fixtures pin both: the CURVE contract
(0x0aa05986…) is our pool id, and the TOKEN (0x49d6a5c9…) is what GMGN reports as its
`pool_address`. Attributing swaps to the token would credit a contract that emits none.
"""
import json
import pathlib
import sqlite3

import pytest

from sniper.services import memecoin_onchain_volume as ov
from sniper.services.memecoin_tick_stream import (
    PONS_V2_CURVE_BUY, PONS_V2_CURVE_SELL, PONS_V2_LAUNCHED, PONS_V2_LAUNCHER,
    decode_evm_log, decode_pons_v2_launched)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "pons"
CHAIN = "4663"
MINT = "0x49d6a5c9795ff2dc709040940798a22b378c1bcd"
CURVE = "0x0aa05986104b528d2166335471d64dc1ffb6a108"
NATIVE = "0x" + "00" * 20
SOURCE_TX = "0xd64a13bc3fb4c63e79c2efb999229bae66b40ada105d1995c5e1d06755e5e126"
AT = 1790168559                                     # the decision, 2026-09-23T13:02:39Z

# The two words the receipt carries, to 18 decimals.
QUOTE_IN_WEI = 128497230365367688                   # 0.12849723 of the chain's native unit
TOKENS_OUT_WEI = 27090336792522968334443510         # 27,090,336.79 MRLN


def _receipt_logs():
    return json.loads((FIXTURES / "mrln_source_buy_receipt.json").read_text())["logs"]


def _curve_buy_log():
    return next(lg for lg in _receipt_logs()
                if (lg.get("topics") or [None])[0] == PONS_V2_CURVE_BUY)


BLOCK_HASH = "0x2a10b9c0e0f1c2d3e4f5061728394a5b6c7d8e9f0a1b2c3d4e5f60718293a4b5"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE pool_ticks (id INTEGER PRIMARY KEY AUTOINCREMENT, chain_id TEXT, pool TEXT,
            kind TEXT, block_no INTEGER, tx_sig TEXT, log_index INTEGER, maker TEXT,
            amount0 TEXT, amount1 TEXT, observed_at TEXT, block_hash TEXT);
        CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT,
            quote_side INTEGER, resolved_at TEXT, token0_decimals INTEGER, token1_decimals INTEGER);
        CREATE TABLE block_times (chain_id TEXT, block_no INTEGER, block_ts INTEGER,
            fetched_at TEXT, block_hash TEXT, PRIMARY KEY (chain_id, block_no));
        CREATE TABLE onchain_pool_protocols (chain_id TEXT, pool TEXT, protocol TEXT, mint TEXT);
    """)
    # The curve as `register_pons_curve` writes it: native quote on side 0, the token on side 1.
    c.execute("INSERT INTO pool_meta VALUES (?,?,?,?,?,?,?,?)",
              (CHAIN, CURVE, NATIVE, MINT, 0, "2026-09-23T12:21:55+00:00", 18, 18))
    for mint in (NATIVE, MINT):
        c.execute("INSERT INTO onchain_pool_protocols VALUES (?,?,?,?)",
                  (CHAIN, CURVE, "pons_v2", mint))
    # The node puts ONE hash on the header and on every log of that block, so the fixture does too.
    c.execute("INSERT INTO block_times VALUES (?,?,?,?,?)",
              (CHAIN, 70533516, AT - 4, "", BLOCK_HASH))
    c.commit()
    return c


def _tick(conn, tx, amount0, amount1, *, block=70533516, index=43):
    conn.execute("INSERT INTO pool_ticks (chain_id, pool, kind, block_no, tx_sig, log_index, "
                 "amount0, amount1, observed_at, block_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (CHAIN, CURVE, "pons_v2_swap", block, tx, index, str(amount0), str(amount1), "",
                  BLOCK_HASH))
    conn.commit()


def _price(_chain, _token):
    return 2715.06          # the chain's native unit in USD, as the explorer reported that day


def test_the_curve_buy_decodes_to_the_amounts_the_receipt_carries():
    row = decode_evm_log(CHAIN, _curve_buy_log(), "2026-09-23T13:02:39+00:00")
    assert row["kind"] == "pons_v2_swap"
    assert row["pool"] == CURVE, "the CURVE is the pool id, never the token GMGN reports"
    assert row["tx_sig"] == SOURCE_TX
    # Positive flows into the pool: the buyer's native in, the coin out.
    assert int(row["amount0"]) == QUOTE_IN_WEI
    assert int(row["amount1"]) == -TOKENS_OUT_WEI


# A REAL CurveSell, because a synthesised one asserts the author's assumption back at them. Mine
# did exactly that: it swapped the topic on a BUY log, so it could not see that the two events put
# their INPUT first and therefore carry their words in OPPOSITE orders. Three minutes of live tape
# on 2026-09-23 14:01Z read a p50 sell quote of 5,576,921 native units against a buy p50 of 0.039.
SELL_TOKENS_IN_WEI = 4234177857122699116666232        # 4,234,177.86 of the coin
SELL_QUOTE_OUT_WEI = 26934223856543512                # 0.02693422 native


def _curve_sell_log():
    return json.loads((FIXTURES / "mrln_chain_curve_sell_log.json").read_text())


def test_a_real_curve_sell_puts_its_token_leg_first():
    """The live defect, pinned to the chain's own bytes."""
    row = decode_evm_log(CHAIN, _curve_sell_log(), "2026-09-23T14:03:23+00:00")
    assert row["kind"] == "pons_v2_swap"
    # The quote leaves the pool and the coin goes in — the mirror of a buy.
    assert int(row["amount0"]) == -SELL_QUOTE_OUT_WEI
    assert int(row["amount1"]) == SELL_TOKENS_IN_WEI


def test_a_sell_quote_leg_is_a_quote_sized_number():
    """The cheap invariant that would have caught it: a native quote leg is never millions.

    A swapped word order puts a token count here, and on the live tape that read 5.6 million.
    """
    row = decode_evm_log(CHAIN, _curve_sell_log(), "2026-09-23T14:03:23+00:00")
    assert abs(int(row["amount0"])) / 1e18 < 1e3
    assert abs(int(row["amount1"])) / 1e18 > 1e3, "and the token leg is the large one"


def test_the_launched_log_names_the_token_then_the_curve():
    """Slot layout, pinned against MRLN's own launch rather than assumed from the signature."""
    log = json.loads((FIXTURES / "mrln_launched_log.json").read_text())
    assert log["topics"][0] == PONS_V2_LAUNCHED
    assert log["address"].lower() == PONS_V2_LAUNCHER
    assert decode_pons_v2_launched(log) == (MINT, CURVE)


def test_a_launched_log_from_another_address_is_refused():
    """Only the singleton launcher may name a curve; anyone can emit a matching topic."""
    log = json.loads((FIXTURES / "mrln_launched_log.json").read_text())
    log["address"] = "0x" + "11" * 20
    assert decode_pons_v2_launched(log) is None


def test_pons_v2_is_a_covered_protocol():
    """Without this the curve is registered, ingested, and then skipped as a foreign venue."""
    assert "pons_v2" in ov.COVERED_PROTOCOLS


def test_the_source_buy_alone_leaves_zero_organic_volume():
    """Rule 2 still holds: the source's own trade is excluded by hash, so $0 organic is honest.

    What changes is the rest of this file — the window is now MEASURED rather than blind.
    """
    conn = _conn()
    _tick(conn, SOURCE_TX, QUOTE_IN_WEI, -TOKENS_OUT_WEI)
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, exclude_tx=SOURCE_TX, price_fn=_price)
    assert out["usd"] == 0.0
    assert out["swaps"] == 0
    assert out["excluded_tx_seen"] is True, "the tape now holds the source trade"
    assert out["reason"] == "ok"


def test_a_second_curve_trade_is_organic_volume_the_gate_can_read():
    conn = _conn()
    _tick(conn, SOURCE_TX, QUOTE_IN_WEI, -TOKENS_OUT_WEI)
    _tick(conn, "0xother", 10 * 10 ** 18, -1, index=44)
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, exclude_tx=SOURCE_TX, price_fn=_price)
    assert out["swaps"] == 1
    assert out["usd"] == pytest.approx(10 * 2715.06)
    assert [p["protocol"] for p in out["pools"]] == ["pons_v2"]


def test_without_the_curve_registered_the_window_is_blind_not_quiet():
    """The MRLN refusal, reproduced: no Pons pool known, and the source trade absent with it."""
    conn = _conn()
    conn.execute("DELETE FROM onchain_pool_protocols")
    conn.commit()
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, exclude_tx=SOURCE_TX, price_fn=_price)
    assert out["usd"] is None and out["reason"] == "no_known_pool"


# --- the writer side: a live frame becomes a tick, a launch becomes a registration -------------

def _writer_tape(tmp_path):
    from sniper.services import memecoin_volume_writer as w
    conn = w.connect(tmp_path / "pons.db")
    tape = w.VolumeTape(conn, CHAIN)
    tape.head({"number": hex(70533516), "timestamp": hex(AT - 4), "hash": "0xaa"}, AT - 4)
    return w, conn, tape


def test_a_launched_frame_registers_the_curve_against_its_mint(tmp_path):
    _w, conn, tape = _writer_tape(tmp_path)
    try:
        tape.log_pons(json.loads((FIXTURES / "mrln_launched_log.json").read_text()), AT)
        meta = conn.execute("SELECT token0, token1, quote_side FROM pool_meta "
                            "WHERE chain_id=? AND pool=?", (CHAIN, CURVE)).fetchone()
        assert (meta[0], meta[1], meta[2]) == (None, MINT, None)
        mints = {r[0] for r in conn.execute(
            "SELECT mint FROM onchain_pool_protocols WHERE chain_id=? AND pool=? AND protocol='pons_v2'",
            (CHAIN, CURVE))}
        assert mints == {MINT}, 'Launched does not encode the counter currency'
    finally:
        conn.close()


def test_a_curve_trade_is_written_even_before_its_launch_is_seen(tmp_path):
    """A trade on an unregistered curve is kept, exactly like a V3 tick for an unresolved pool.

    Dropping it would lose the first minutes of every new coin — which is all of the volume that
    matters on a launchpad.
    """
    _w, conn, tape = _writer_tape(tmp_path)
    try:
        tape.log_pons(_curve_buy_log(), AT)
        row = conn.execute("SELECT pool, kind, amount0, amount1 FROM pool_ticks "
                           "WHERE chain_id=? AND tx_sig=?", (CHAIN, SOURCE_TX)).fetchone()
        assert row["pool"] == CURVE and row["kind"] == "pons_v2_swap"
        assert int(row["amount0"]) == QUOTE_IN_WEI and int(row["amount1"]) == -TOKENS_OUT_WEI
        assert conn.execute("SELECT COUNT(*) FROM pool_meta WHERE pool=?", (CURVE,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_the_subscription_asks_for_the_topics_the_decoder_handles():
    """A SOURCE GUARD. The 4663 tape went a month with zero V4 coverage because the request's
    topic list and the decoder's vocabulary drifted apart; this is the same failure one venue later,
    and nothing else in the suite opens a socket to catch it."""
    import inspect

    from sniper.services import memecoin_volume_writer as w
    src = inspect.getsource(w.run) + inspect.getsource(w.subscriptions)   # run sends subscriptions()
    assert "PONS_V2_CURVE_BUY,PONS_V2_CURVE_SELL" in src.replace(" ", "")
    assert "PONS_V2_LAUNCHER" in src and "PONS_V2_LAUNCHED" in src
