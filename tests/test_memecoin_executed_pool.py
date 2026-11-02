"""The pool a fill actually touched, read from our own receipt.

Nothing recorded it before 2026-09-22. `quote_route_target` is the ROUTER we routed through, and
`evm_route_json.pool_id` is the pool the QUOTE chose -- present on 42% of funded EVM fills since
09-15 and absent on every relay and aggregator route. A quoted pool is not an executed one: BUDDY
(2026-09-06) hit a decoy pool while the real one also quoted, and its first exact exit mark four
seconds after the fill was -61%.

No network. Every receipt here is a fake shaped like a real one.
"""
import sqlite3

from sniper.services import memecoin_executed_pool as X
from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP

CHAIN = "4663"
MANAGER = V4_POOL_MANAGERS[CHAIN]
POOL_ID = "0x" + "ab" * 32
PAIR_A = "0x" + "11" * 20
PAIR_B = "0x" + "22" * 20


def _log(address, topic0, *rest):
    return {"address": address, "topics": [topic0, *rest], "data": "0x"}


def _receipt(*logs):
    return {"logs": list(logs)}


def test_a_v4_swap_names_the_pool_id_not_the_manager():
    """V4's emitter is the singleton PoolManager, so the pool is topic1. Recording the emitter
    would name the same address for every V4 fill on the chain."""
    r = _receipt(_log(MANAGER, V4_SWAP, POOL_ID, "0x" + "00" * 32))
    assert X.executed_pool_from_receipt(CHAIN, r) == (POOL_ID, X.SOURCE_V4, 1)


def test_a_v3_swap_names_the_emitting_pool():
    r = _receipt(_log(PAIR_A, X.V3_SWAP, "0x1", "0x2"))
    assert X.executed_pool_from_receipt(CHAIN, r) == (PAIR_A, X.SOURCE_V3, 1)


def test_a_v2_swap_names_the_emitting_pair():
    r = _receipt(_log(PAIR_A, X.V2_SWAP, "0x1", "0x2"))
    assert X.executed_pool_from_receipt(CHAIN, r) == (PAIR_A, X.SOURCE_V2, 1)


def test_the_LAST_hop_wins_on_a_multihop_route():
    """THE PROPERTY THIS FILE EXISTS FOR. A multi-hop path ends at the pool that delivered the coin
    we now hold; the earlier hops priced a bridge we no longer own."""
    r = _receipt(_log(PAIR_A, X.V2_SWAP, "0x1"),
                 _log(PAIR_B, X.V3_SWAP, "0x1", "0x2"))
    pool, source, seen = X.executed_pool_from_receipt(CHAIN, r)
    assert (pool, source) == (PAIR_B, X.SOURCE_V3)
    assert seen == 2, "the hop count must be visible, not inferred from the venue name"


def test_an_aggregator_path_reports_how_many_hops_it_took():
    r = _receipt(_log(PAIR_A, X.V2_SWAP, "0x1"), _log(PAIR_B, X.V2_SWAP, "0x1"),
                 _log(MANAGER, V4_SWAP, POOL_ID, "0x" + "00" * 32))
    assert X.executed_pool_from_receipt(CHAIN, r) == (POOL_ID, X.SOURCE_V4, 3)


def test_a_v4_swap_from_something_that_is_not_the_pool_manager_is_ignored():
    """Anyone can emit a log with that topic. Only the chain's real PoolManager names a V4 pool."""
    r = _receipt(_log("0x" + "99" * 20, V4_SWAP, POOL_ID, "0x" + "00" * 32))
    assert X.executed_pool_from_receipt(CHAIN, r) == (None, None, 0)


def test_a_receipt_with_no_swap_names_nothing():
    r = _receipt(_log(PAIR_A, "0x" + "de" * 32, "0x1"))
    assert X.executed_pool_from_receipt(CHAIN, r) == (None, None, 0)


def test_an_empty_or_broken_receipt_is_None_never_a_guess():
    for bad in ({}, {"logs": []}, None, {"logs": [{"topics": []}]}):
        assert X.executed_pool_from_receipt(CHAIN, bad) == (None, None, 0)


def test_an_unknown_chain_still_reads_v2_and_v3_but_never_v4():
    """V4 needs a known PoolManager to trust topic1; V2/V3 carry their identity in the emitter."""
    r = _receipt(_log(MANAGER, V4_SWAP, POOL_ID, "0x" + "00" * 32))
    assert X.executed_pool_from_receipt("999999", r) == (None, None, 0)
    r2 = _receipt(_log(PAIR_A, X.V2_SWAP, "0x1"))
    assert X.executed_pool_from_receipt("999999", r2) == (PAIR_A, X.SOURCE_V2, 1)


# --- solana, which is carried over rather than decoded ------------------------------------------

def _tape():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sol_position_pools(position_id INTEGER, mint TEXT, pool TEXT,"
                 " venue_kind TEXT, resolution_tx TEXT, resolved_at TEXT)")
    return conn


def test_solana_carries_the_tape_resolution_and_labels_it_as_such():
    conn = _tape()
    conn.execute("INSERT INTO sol_position_pools VALUES(7,'M','POOL7','cpmm','sig','t')")
    assert X.executed_pool_for_solana(conn, 7) == ("POOL7", X.SOURCE_SOL_TAPE)


def test_solana_with_no_resolution_names_nothing():
    assert X.executed_pool_for_solana(_tape(), 7) == (None, None)
    assert X.executed_pool_for_solana(None, 7) == (None, None)


# --- persistence --------------------------------------------------------------------------------

def _book():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memecoin_positions(id INTEGER PRIMARY KEY, executed_pool TEXT,"
                 " executed_pool_source TEXT)")
    conn.execute("INSERT INTO memecoin_positions(id) VALUES(1)")
    return conn


def test_the_stamp_is_write_once():
    """An exit through a different venue must not overwrite what the ENTRY did: the executed pool
    is a fact about one transaction."""
    from sniper.persistence import db_memecoin as m
    conn = _book()
    assert m.record_executed_pool(conn, 1, "POOL_ENTRY", X.SOURCE_V4) is True
    assert m.record_executed_pool(conn, 1, "POOL_EXIT", X.SOURCE_V2) is False
    row = conn.execute("SELECT executed_pool, executed_pool_source FROM memecoin_positions "
                       "WHERE id=1").fetchone()
    assert row == ("POOL_ENTRY", X.SOURCE_V4)


def test_a_missing_pool_or_source_writes_nothing():
    from sniper.persistence import db_memecoin as m
    conn = _book()
    for pool, source in ((None, X.SOURCE_V4), ("P", None), ("", "")):
        assert m.record_executed_pool(conn, 1, pool, source) is False
    assert conn.execute("SELECT executed_pool FROM memecoin_positions WHERE id=1").fetchone()[0] is None


# --- a SPLIT route names the pool that delivered most of the coin (Alpha 2026-10-17, 9177657c) ---
# The last-Swap rule is right for a chain of hops, where only the last pool sends our coin. On a
# split route several pools send it: 9177657c's Relay fill put 92% of its WETH into Abyss WETH/MONTY
# 0x8e5354f0 and recorded MONTY/SEA 0x55b10cfc, the last V3 Swap, which delivered 4%.

import json
from pathlib import Path

TRANSFER_TOPIC = X.TRANSFER_TOPIC

MINT = "0x" + "4d" * 20
SPLIT = json.loads((Path(__file__).resolve().parent / "fixtures" / "memecoin"
                    / "executed_pool_split_route_9177657c.json").read_text())


def _word(address):
    return "0x" + "0" * 24 + address[2:]


def _sent(token, sender, amount, to="0x" + "99" * 20):
    return {"address": token, "topics": [TRANSFER_TOPIC, _word(sender), _word(to)],
            "data": "0x%064x" % amount}


def test_the_recorded_split_fill_names_the_pool_that_delivered_92_percent():
    receipt = SPLIT["receipt"]
    assert X.executed_pool_from_receipt(CHAIN, receipt, mint=SPLIT["mint"]) == (
        "0x8e5354f0f7a3e02a4438af8605c3e9a8fab45004", X.SOURCE_V3, 7)
    # without the coin, today's answer is unchanged for every other caller
    assert X.executed_pool_from_receipt(CHAIN, receipt)[0] == "0x55b10cfc8ebb86a41e83e2018b0741ceb0ca7e42"


def test_the_largest_delivery_wins_over_log_order():
    r = _receipt(_sent(MINT, PAIR_B, 90), _log(PAIR_B, X.V3_SWAP, "0x1", "0x2"),
                 _sent(MINT, PAIR_A, 10), _log(PAIR_A, X.V3_SWAP, "0x1", "0x2"))
    assert X.executed_pool_from_receipt(CHAIN, r, mint=MINT.upper().replace("0X", "0x")) == (
        PAIR_B, X.SOURCE_V3, 2)


def test_a_tie_goes_to_the_later_swap():
    r = _receipt(_sent(MINT, PAIR_A, 50), _log(PAIR_A, X.V2_SWAP, "0x1"),
                 _sent(MINT, PAIR_B, 50), _log(PAIR_B, X.V3_SWAP, "0x1", "0x2"))
    assert X.executed_pool_from_receipt(CHAIN, r, mint=MINT)[0] == PAIR_B


def test_a_chain_of_hops_still_names_the_last_pool():
    """Only the last hop sends our coin; the bridge hop sends its own token."""
    bridge = "0x" + "b0" * 20
    r = _receipt(_sent(bridge, PAIR_A, 10 ** 30), _log(PAIR_A, X.V2_SWAP, "0x1"),
                 _sent(MINT, PAIR_B, 5), _log(PAIR_B, X.V3_SWAP, "0x1", "0x2"))
    assert X.executed_pool_from_receipt(CHAIN, r, mint=MINT)[0] == PAIR_B


def test_a_v4_leg_or_no_pool_sent_coin_keeps_the_last_swap_rule():
    """A V4 or Infinity pool pays out of its singleton manager, so its share is not readable from
    a transfer; any such swap, or no coin sent by a swapping pool, keeps today's rule."""
    with_v4 = _receipt(_sent(MINT, PAIR_A, 90), _log(PAIR_A, X.V3_SWAP, "0x1", "0x2"),
                       _sent(MINT, MANAGER, 10), _log(MANAGER, V4_SWAP, POOL_ID, "0x" + "00" * 32))
    assert X.executed_pool_from_receipt(CHAIN, with_v4, mint=MINT) == (POOL_ID, X.SOURCE_V4, 2)
    router_paid = _receipt(_log(PAIR_A, X.V3_SWAP, "0x1", "0x2"), _log(PAIR_B, X.V3_SWAP, "0x1", "0x2"),
                           _sent(MINT, "0x" + "ee" * 20, 100))
    assert X.executed_pool_from_receipt(CHAIN, router_paid, mint=MINT)[0] == PAIR_B


def test_a_malformed_transfer_amount_falls_back_and_strict_raises():
    """Gamma LOW (9c7f280d7): a malformed coin Transfer drops only the share; the last Swap stands,
    as it did before the coin was passed."""
    bad = _sent(MINT, PAIR_A, 1)
    bad["data"] = "0xzz"
    r = _receipt(bad, _log(PAIR_A, X.V3_SWAP, "0x1", "0x2"), _log(PAIR_B, X.V3_SWAP, "0x1", "0x2"))
    assert X.executed_pool_from_receipt(CHAIN, r, mint=MINT) == (PAIR_B, X.SOURCE_V3, 2)
    import pytest
    with pytest.raises(ValueError):
        X.executed_pool_from_receipt(CHAIN, r, mint=MINT, strict=True)


def test_a_pool_that_did_not_swap_is_not_counted():
    """Gamma 3c: a skim or LP burn in the same transaction is a Transfer from a pool, not a swap
    delivery. Only addresses that also emitted a Swap here count."""
    skimmer = "0x" + "5c" * 20
    r = _receipt(_sent(MINT, skimmer, 10 ** 24), _sent(MINT, PAIR_A, 30),
                 _log(PAIR_A, X.V3_SWAP, "0x1", "0x2"), _sent(MINT, PAIR_B, 20),
                 _log(PAIR_B, X.V3_SWAP, "0x1", "0x2"))
    assert X.executed_pool_from_receipt(CHAIN, r, mint=MINT)[0] == PAIR_A


def test_a_fee_on_transfer_leg_counts_toward_its_own_pool():
    """Gamma 3b: the tax leg is also a Transfer from the pool, so it adds to that pool's share."""
    r = _receipt(_sent(MINT, PAIR_A, 40), _sent(MINT, PAIR_A, 30, to="0x" + "7a" * 20),
                 _log(PAIR_A, X.V2_SWAP, "0x1"), _sent(MINT, PAIR_B, 60),
                 _log(PAIR_B, X.V3_SWAP, "0x1", "0x2"))
    assert X.executed_pool_from_receipt(CHAIN, r, mint=MINT)[0] == PAIR_A


def test_bsc_native_bridge_route_keeps_the_last_pool_and_its_kind():
    """Gamma 3a: memecoin_bsc_v2_live maps only pancake_v3/uniswap_v2 routes, our own single-path
    swaps. There only the final hop sends the coin, so the winner and its kind are today's."""
    wbnb_pair, v3_pool = "0x" + "a1" * 20, "0x" + "a2" * 20
    bridge = "0x" + "b1" * 20
    r = _receipt(_sent(bridge, wbnb_pair, 10 ** 20), _log(wbnb_pair, X.V2_SWAP, "0x1"),
                 _sent(MINT, v3_pool, 7), _log(v3_pool, X.PANCAKE_V3_SWAP, "0x1", "0x2"))
    assert X.executed_pool_from_receipt("56", r, mint=MINT) == (v3_pool, X.SOURCE_V3, 2)
    split = _receipt(_sent(MINT, wbnb_pair, 90), _log(wbnb_pair, X.V2_SWAP, "0x1"),
                     _sent(MINT, v3_pool, 10), _log(v3_pool, X.PANCAKE_V3_SWAP, "0x1", "0x2"))
    assert X.executed_pool_from_receipt("56", split, mint=MINT) == (wbnb_pair, X.SOURCE_V2, 2)
