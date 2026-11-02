"""V4 pool_meta resolution from the Initialize event (2026-08-30).

`pool_meta` maps mint -> pool so a liquidity witness can ask "when did THIS pool last trade".
It is populated by calling token0()/token1() on a pool CONTRACT — and a V4 pool is NOT a
contract, it is a key inside the PoolManager singleton. So the existing resolver structurally
cannot produce a V4 row, and measured 2026-08-30 it had ZERO: 60,880 rows for chain 4663, every
one a 42-char V2/V3 address.

The consequence was that the V4 swap rows the tick-stream topic fix now ingests were unreachable
by mint. Our funded 4663 positions are V4, so a witness could answer for every pool on the chain
EXCEPT the ones we hold.

The mapping exists only in the PoolManager's `Initialize` event, which carries the pool id and
both currencies as INDEXED topics. Verified on chain: 4 topics, a 66-char pool id in topics[1],
currency0/currency1 in topics[2]/[3], and fee/tickSpacing/hooks in the data.
"""
import sqlite3

import pytest

import sniper.services.memecoin_evm_v4_events as V4E

MGR = V4E.V4_POOL_MANAGERS["4663"]
POOL = "0x" + "ab" * 32
C0 = "0x" + "00" * 20
C1 = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"


def _init_log(pool=POOL, c0=C0, c1=C1, address=MGR):
    return {
        "address": address,
        "topics": [V4E.V4_INITIALIZE, pool,
                   "0x" + "00" * 12 + c0[2:], "0x" + "00" * 12 + c1[2:]],
        "data": "0x" + "".join([format(200000, "064x"), format(200, "064x"),
                                format(0, "064x"), format(0, "064x"), format(0, "064x")]),
        "blockNumber": "0x1", "logIndex": "0x0",
        "transactionHash": "0xdead", "blockHash": "0xbeef",
    }


def test_the_initialize_topic_matches_the_deployed_event():
    """Derived from the signature, not pasted: the 8-field form is what chain 4663 emits."""
    from eth_utils import keccak
    expected = "0x" + keccak(
        text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex()
    assert V4E.V4_INITIALIZE == expected


def test_decode_initialize_returns_the_pool_to_currency_mapping():
    row = V4E.decode_v4_initialize("4663", _init_log(), "2026-08-30T18:00:00+00:00")
    assert row is not None
    assert row["pool"] == POOL.lower()
    assert len(row["pool"]) == 66, "a V4 pool id is 32 bytes; a 42-char value is a V2/V3 address"
    assert row["token0"] == C0.lower()
    assert row["token1"] == C1.lower()
    assert row["chain_id"] == "4663"


def test_a_log_from_another_address_is_refused():
    """Only OUR configured PoolManager. A foreign singleton would map pools we cannot trade."""
    assert V4E.decode_v4_initialize(
        "4663", _init_log(address="0x" + "99" * 20), "2026-08-30T18:00:00+00:00") is None


def test_a_wrong_length_pool_id_is_refused():
    bad = _init_log()
    bad["topics"][1] = "0x" + "ab" * 20        # 42-char, a V2/V3 address shape
    assert V4E.decode_v4_initialize("4663", bad, "2026-08-30T18:00:00+00:00") is None


def test_a_non_initialize_log_is_refused():
    other = _init_log()
    other["topics"][0] = V4E.V4_SWAP
    assert V4E.decode_v4_initialize("4663", other, "2026-08-30T18:00:00+00:00") is None


def test_an_unconfigured_chain_is_refused():
    assert V4E.decode_v4_initialize("9999", _init_log(), "2026-08-30T18:00:00+00:00") is None


# --- the write path ---------------------------------------------------------------------------

def _ticks_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT,
                 quote_side INTEGER, resolved_at TEXT, token0_decimals INTEGER,
                 token1_decimals INTEGER, PRIMARY KEY (chain_id, pool))""")
    return c


def test_upsert_writes_a_v4_row_and_is_idempotent():
    from sniper.services.memecoin_tick_stream import upsert_v4_pool_meta
    c = _ticks_db()
    row = V4E.decode_v4_initialize("4663", _init_log(), "2026-08-30T18:00:00+00:00")
    assert upsert_v4_pool_meta(c, [row]) == 1
    assert upsert_v4_pool_meta(c, [row]) in (0, 1), "re-ingesting the same log must not error"
    got = c.execute("SELECT * FROM pool_meta WHERE chain_id='4663'").fetchall()
    assert len(got) == 1
    assert got[0]["pool"] == POOL.lower()
    assert len(got[0]["pool"]) == 66


def test_upsert_makes_the_mint_resolvable_which_is_the_whole_point():
    """The witness asks: given a mint, which pools do I watch? That must return the V4 pool."""
    from sniper.services.memecoin_tick_stream import upsert_v4_pool_meta
    c = _ticks_db()
    upsert_v4_pool_meta(c, [V4E.decode_v4_initialize("4663", _init_log(), "2026-08-30T18:00:00+00:00")])
    found = c.execute(
        "SELECT pool FROM pool_meta WHERE chain_id=? AND (token0=? OR token1=?)",
        ("4663", C1.lower(), C1.lower())).fetchall()
    assert [r["pool"] for r in found] == [POOL.lower()]


def test_upsert_tolerates_an_empty_batch():
    from sniper.services.memecoin_tick_stream import upsert_v4_pool_meta
    assert upsert_v4_pool_meta(_ticks_db(), []) == 0


def test_initialize_is_in_the_ingest_topic_list():
    """It rides the SAME getLogs the tick stream already makes — no extra call, and Initialize is
    rare (4 logs per 300 blocks measured), so the volume cost is negligible."""
    import sniper.services.memecoin_tick_stream as TS
    assert V4E.V4_INITIALIZE.lower() in {t.lower() for t in TS.INGEST_TOPICS}


# --- BACKFILL: the forward path only sees pools born after it deploys --------------------------
# A pool that already initialized never emits Initialize again, so without a backfill the index
# is blind on every pre-existing position — which is every position we currently hold. Same
# failure as the un-indexed version, just later.
#
# The pool id is an INDEXED topic, so this is a targeted lookup per pool rather than a block
# sweep: bounded by the size of OUR universe, not by chain history.

def test_backfill_writes_mappings_for_the_pools_we_ask_about():
    from sniper.services.memecoin_tick_stream import backfill_v4_pool_meta
    c = _ticks_db()
    keys = {POOL: {"currency0": C0, "currency1": C1}}
    r = backfill_v4_pool_meta(c, "4663", [POOL], rpc_url="http://rpc",
                              key_fn=lambda ch, pid, rpc: keys.get(pid),
                              now_iso="2026-08-30T18:00:00+00:00")
    assert r["written"] == 1 and r["resolved"] == 1 and r["unresolved"] == 0
    assert c.execute("SELECT pool FROM pool_meta").fetchone()["pool"] == POOL.lower()


def test_backfill_REPORTS_what_it_could_not_resolve():
    """The deliverable is 'complete for these pools', never an implied completeness. A pool the
    lookup could not answer for must be COUNTED, so the witness treats it as unknown rather than
    as a pool with no trades."""
    from sniper.services.memecoin_tick_stream import backfill_v4_pool_meta
    c = _ticks_db()
    other = "0x" + "cd" * 32
    r = backfill_v4_pool_meta(c, "4663", [POOL, other], rpc_url="http://rpc",
                              key_fn=lambda ch, pid, rpc: ({"currency0": C0, "currency1": C1}
                                                           if pid == POOL else None),
                              now_iso="2026-08-30T18:00:00+00:00")
    assert r["resolved"] == 1 and r["unresolved"] == 1
    assert other in r["unresolved_pools"]


def test_backfill_is_idempotent_against_the_forward_path():
    """A backfill re-ingesting a log the forward path already wrote must be a NO-OP, not an
    error — the mapping is immutable."""
    from sniper.services.memecoin_tick_stream import backfill_v4_pool_meta, upsert_v4_pool_meta
    c = _ticks_db()
    upsert_v4_pool_meta(c, [V4E.decode_v4_initialize("4663", _init_log(),
                                                     "2026-08-30T18:00:00+00:00")])
    r = backfill_v4_pool_meta(c, "4663", [POOL], rpc_url="http://rpc",
                              key_fn=lambda ch, pid, rpc: {"currency0": C0, "currency1": C1},
                              now_iso="2026-08-30T19:00:00+00:00")
    assert c.execute("SELECT COUNT(*) n FROM pool_meta").fetchone()["n"] == 1
    assert c.execute("SELECT resolved_at FROM pool_meta").fetchone()[0].startswith("2026-08-30T18"), \
        "an immutable mapping must not be restamped by a re-run"


def test_backfill_refuses_a_non_v4_pool_id():
    """A 42-char id is a V2/V3 address. Same guard as the forward path."""
    from sniper.services.memecoin_tick_stream import backfill_v4_pool_meta
    c = _ticks_db()
    r = backfill_v4_pool_meta(c, "4663", ["0x" + "ab" * 20], rpc_url="http://rpc",
                              key_fn=lambda ch, pid, rpc: {"currency0": C0, "currency1": C1},
                              now_iso="2026-08-30T18:00:00+00:00")
    assert r["written"] == 0 and r["skipped_shape"] == 1


def test_backfill_tolerates_a_raising_lookup_and_counts_it():
    from sniper.services.memecoin_tick_stream import backfill_v4_pool_meta
    def boom(ch, pid, rpc): raise RuntimeError("node refused")
    r = backfill_v4_pool_meta(_ticks_db(), "4663", [POOL], rpc_url="http://rpc",
                              key_fn=boom, now_iso="2026-08-30T18:00:00+00:00")
    assert r["written"] == 0 and r["unresolved"] == 1


def test_backfill_default_timestamp_is_callable():
    """Every other test passes now_iso, so the DEFAULT path was never exercised — and it was a
    NameError. A test that always supplies the argument proves the logic, not the wiring."""
    from sniper.services.memecoin_tick_stream import backfill_v4_pool_meta
    c = _ticks_db()
    r = backfill_v4_pool_meta(c, "4663", [POOL], rpc_url="http://rpc",
                              key_fn=lambda ch, pid, rpc: {"currency0": C0, "currency1": C1})
    assert r["written"] == 1
    assert c.execute("SELECT resolved_at FROM pool_meta").fetchone()[0].startswith("20")


# --- USABILITY: a row the canonical resolver filters out is not coverage ------------------------
# memecoin_exits.py:1143 requires quote_side IS NOT NULL AND both decimals IS NOT NULL. Measured
# 2026-08-30: 59,869 of 60,881 42-char rows were usable (98.3%) and 0 of 214 V4 rows were (0%).
# Writing token0/token1 alone produces rows the ONE consumer discards, so row count reads as
# coverage while the witness stays blind. 140 of those 214 have token0 = the native sentinel,
# which is not in the quote-token set, and tick_features treats a NULL quote_side as "done" and
# never revisits it.

NATIVE = "0x" + "00" * 20
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"


def _bf(c, pools, key_fn, **kw):
    from sniper.services.memecoin_tick_stream import backfill_v4_pool_meta
    kw.setdefault("quote_tokens", {WETH})
    kw.setdefault("dec_fn", lambda tok: 18)
    kw.setdefault("now_iso", "2026-08-30T18:00:00+00:00")
    return backfill_v4_pool_meta(c, "4663", pools, rpc_url="http://rpc", key_fn=key_fn, **kw)


def _usable(c):
    return c.execute("SELECT COUNT(*) FROM pool_meta WHERE quote_side IS NOT NULL "
                     "AND token0_decimals IS NOT NULL AND token1_decimals IS NOT NULL").fetchone()[0]


def test_a_native_v4_pool_resolves_quote_side_and_is_USABLE():
    """The native sentinel IS the quote asset on a V4 native pool, and it sorts first, so it is
    always currency0. It has no decimals() to call — native is 18 by definition."""
    c = _ticks_db()
    r = _bf(c, [POOL], lambda ch, pid, rpc: {"currency0": NATIVE, "currency1": C1})
    assert r["written"] == 1
    row = c.execute("SELECT quote_side, token0_decimals, token1_decimals FROM pool_meta").fetchone()
    assert row["quote_side"] == 0
    assert row["token0_decimals"] == 18, "native has no contract; 18 by definition, never a call"
    assert _usable(c) == 1


def test_a_weth_quoted_v4_pool_takes_the_side_weth_is_on():
    c = _ticks_db()
    _bf(c, [POOL], lambda ch, pid, rpc: {"currency0": C1, "currency1": WETH})
    assert c.execute("SELECT quote_side FROM pool_meta").fetchone()[0] == 1


def test_a_pool_with_no_known_quote_token_stays_unusable_rather_than_guessed():
    """Orientation decides which side is price. A guess is worse than an unresolved row."""
    c = _ticks_db()
    _bf(c, [POOL], lambda ch, pid, rpc: {"currency0": C1, "currency1": "0x" + "11" * 20})
    assert c.execute("SELECT quote_side FROM pool_meta").fetchone()[0] is None
    assert _usable(c) == 0


def test_a_failed_decimals_call_leaves_the_row_unusable_not_defaulted_to_18():
    """Defaulting decimals is a silent scale error: every price off by 10^n."""
    c = _ticks_db()
    _bf(c, [POOL], lambda ch, pid, rpc: {"currency0": NATIVE, "currency1": C1},
        dec_fn=lambda tok: None if tok == C1.lower() else 18)
    assert c.execute("SELECT token1_decimals FROM pool_meta").fetchone()[0] is None
    assert _usable(c) == 0


def test_an_existing_mapping_row_gets_ENRICHED_not_skipped():
    """The 214 rows the forward path already wrote have token0/token1 and nothing else. INSERT OR
    IGNORE leaves them unusable for ever, so the backfill must UPDATE the missing fields."""
    from sniper.services.memecoin_tick_stream import upsert_v4_pool_meta
    c = _ticks_db()
    upsert_v4_pool_meta(c, [{"chain_id": "4663", "pool": POOL.lower(), "token0": NATIVE,
                             "token1": C1.lower(), "resolved_at": "2026-08-30T10:00:00+00:00"}])
    assert _usable(c) == 0
    r = _bf(c, [POOL], lambda ch, pid, rpc: {"currency0": NATIVE, "currency1": C1})
    assert r["enriched"] == 1
    assert _usable(c) == 1
    assert c.execute("SELECT resolved_at FROM pool_meta").fetchone()[0].startswith("2026-08-30T10"), \
        "enrichment adds missing fields; it must not restamp the immutable mapping"


def test_enrichment_never_overwrites_a_resolved_field():
    c = _ticks_db()
    c.execute("INSERT INTO pool_meta (chain_id,pool,token0,token1,quote_side,token0_decimals,"
              "token1_decimals,resolved_at) VALUES ('4663',?,?,?,1,6,9,'2026-08-30T10:00:00+00:00')",
              (POOL.lower(), NATIVE, C1.lower()))
    c.commit()
    _bf(c, [POOL], lambda ch, pid, rpc: {"currency0": NATIVE, "currency1": C1})
    row = c.execute("SELECT quote_side, token0_decimals, token1_decimals FROM pool_meta").fetchone()
    assert (row["quote_side"], row["token0_decimals"], row["token1_decimals"]) == (1, 6, 9)
