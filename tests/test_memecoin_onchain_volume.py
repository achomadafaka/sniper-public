"""On-chain m5 volume for 4663, read from our own swap tape instead of a provider.

PERSON (2026-09-16 11:57:28.521Z) is the acceptance case: 7 swaps landed in the 5 minutes before
the decision, DexScreener had indexed ONE of them ($2,394.60), and the gate refused against a
$2,677 floor. The chain truth, excluding the trader's own buy, was about $3,480 — a pass.

UNKNOWN IS NEVER ZERO: a mint with no known pool, or a feed that has gone quiet, returns None so
the gate falls back to the provider. A confident $0 would refuse a live coin.
"""
import sqlite3

import pytest

from sniper.services import memecoin_onchain_volume as ov

CHAIN = "4663"
MINT = "0x70d3e2fc00000000000000000000000000000000"
QUOTE = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"          # WETH on 4663
POOL = "0xc093bcee00000000000000000000000000000000"
AT = 1789560000                                                # decision block time


def _hash(block):
    """The canonical hash for a height in these fixtures; an orphan carries a different one."""
    return "0xcanon%d" % block


def _conn(swaps=(), *, pool_rows=None, blocks=None, v4_pools=(MINT,), v23_pools=(),
          orphans=(), unverified_blocks=()):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE pool_ticks (id INTEGER PRIMARY KEY AUTOINCREMENT, chain_id TEXT, pool TEXT,
            kind TEXT, block_no INTEGER, tx_sig TEXT, log_index INTEGER, maker TEXT,
            amount0 TEXT, amount1 TEXT, observed_at TEXT, block_hash TEXT);
        CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT,
            quote_side INTEGER, resolved_at TEXT, token0_decimals INTEGER, token1_decimals INTEGER);
        -- PRIMARY KEY as in production: one row per height, REPLACEd on a reorg, which is what
        -- makes its hash the canonical one.
        CREATE TABLE block_times (chain_id TEXT, block_no INTEGER, block_ts INTEGER,
            fetched_at TEXT, block_hash TEXT, PRIMARY KEY (chain_id, block_no));
        CREATE TABLE onchain_pool_protocols (chain_id TEXT, pool TEXT, protocol TEXT, mint TEXT);
    """)
    for pool in v4_pools:
        c.execute("INSERT INTO onchain_pool_protocols VALUES (?,?,?,?)", (CHAIN, POOL, "v4", pool))
    for pool in v23_pools:
        c.execute("INSERT INTO onchain_pool_protocols VALUES (?,?,?,?)",
                  (CHAIN, "0xv23pool", "v3", pool))
    c.execute("INSERT INTO pool_meta VALUES (?,?,?,?,?,?,?,?)",
              (CHAIN, POOL, QUOTE, MINT, 0, "2026-09-16T00:00:00+00:00", 18, 18))
    for block, ts in ([(100, AT - 10)] if blocks is None else blocks):
        c.execute("INSERT INTO block_times VALUES (?,?,?,?,?)",
                  (CHAIN, block, ts, "",
                   None if block in unverified_blocks else _hash(block)))
    rows = [(block, tx, amount, _hash(block)) for block, tx, amount in swaps]
    rows += [(block, tx, amount, "0xreplaced%d" % block) for block, tx, amount in orphans]
    for i, (block, tx, quote_amount, block_hash) in enumerate(rows):
        c.execute("INSERT INTO pool_ticks (chain_id, pool, kind, block_no, tx_sig, log_index, "
                  "amount0, amount1, observed_at, block_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (CHAIN, POOL, "v4_swap", block, tx, i, str(int(quote_amount * 10 ** 18)),
                   str(-10 ** 18), "", block_hash))
    c.commit()
    return c


def _price(_chain, _token):
    return 1.0          # 1 quote unit = $1, so a quote amount IS the USD amount


def test_person_would_have_passed_on_chain_truth():
    """7 swaps, the trader's own excluded: about $3,480 against a $2,677 floor."""
    swaps = [(100, "0x83500da0", 1988.0)] + [(100, "0xother%d" % i, 580.0) for i in range(6)]
    out = ov.onchain_volume_m5(_conn(swaps, blocks=[(100, AT - 5)]), CHAIN, MINT, AT,
                               exclude_tx="0x83500da0", price_fn=_price)
    assert out["usd"] == pytest.approx(3480.0)
    assert out["swaps"] == 6
    assert out["coverage"] == "complete"
    assert out["usd"] > 2677.49


def test_the_source_transaction_is_excluded_by_hash_not_by_subtracting_a_size():
    swaps = [(100, "0xsource", 1000.0), (100, "0xother", 250.0)]
    out = ov.onchain_volume_m5(_conn(swaps), CHAIN, MINT, AT, exclude_tx="0xSOURCE",
                               price_fn=_price)
    assert out["usd"] == pytest.approx(250.0) and out["swaps"] == 1


@pytest.mark.parametrize("missing_meta", [False, True])
def test_one_measure_reads_pool_metadata_once_but_prices_every_swap(missing_meta):
    conn = _with_unpriced_pool(_conn([
        (100, "0xsource", 1000.), (100, "0xa", 2.), (100, "0xb", 3.), (100, "0xzero", 0.)
    ]), [(100, "0xunknown1"), (100, "0xunknown2")])
    if missing_meta:
        conn.execute("DELETE FROM pool_meta WHERE pool=?", (UNPRICED,))
    reads, prices = [], []
    conn.set_trace_callback(lambda sql: reads.append(sql) if (
        sql.startswith("SELECT") and "FROM pool_meta" in sql) else None)

    def fresh_price(chain, token):
        prices.append((chain, token))
        return float(len(prices))

    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, exclude_tx="0xSOURCE", price_fn=fresh_price)
    assert out["usd"] == pytest.approx(8.) and out["swaps"] == 3
    assert out["excluded_tx_seen"] is True and out["coverage"] == "partial"
    assert out["unpriced_pools"] == [{"pool": UNPRICED, "protocol": "v4", "swaps": 2}]
    assert prices == [(CHAIN, QUOTE)] * 3, "a price must remain fresh for each priced swap"
    assert len(reads) == 1, "metadata is already available in the measurement's pool snapshot"

    # A later measurement must observe new metadata, including a previously absent pool.
    conn.execute("UPDATE pool_meta SET quote_side=1 WHERE pool=?", (POOL,))
    if missing_meta:
        conn.execute("INSERT INTO pool_meta VALUES (?,?,?,?,?,?,?,?)",
                     (CHAIN, UNPRICED, QUOTE, MINT, 0, "", 6, 18))
    else:
        conn.execute("UPDATE pool_meta SET quote_side=0, token0_decimals=6 WHERE pool=?", (UNPRICED,))
    reads.clear()
    prices.clear()

    def price_one(chain, token):
        prices.append((chain, token))
        return 1.

    after = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, exclude_tx="0xsource", price_fn=price_one)
    assert after["usd"] == pytest.approx(13.) and after["swaps"] == 5
    assert after["coverage"] == "complete" and after["unpriced_pools"] == []
    assert len(prices) == 5 and prices[:3] == [(CHAIN, MINT)] * 3
    assert len(reads) == 1, "the next measurement must obtain its own metadata snapshot"
    conn.close()


def test_direct_quote_usd_calls_keep_reading_current_metadata():
    conn = _conn([(100, "0xa", 2.)])
    row = conn.execute("SELECT * FROM pool_ticks").fetchone()
    reads = []
    conn.set_trace_callback(lambda sql: reads.append(sql) if (
        sql.startswith("SELECT") and "FROM pool_meta" in sql) else None)
    assert ov._quote_usd(conn, CHAIN, row, _price) == 2.
    conn.execute("UPDATE pool_meta SET quote_side=1 WHERE pool=?", (POOL,))
    assert ov._quote_usd(conn, CHAIN, row, _price) == 1.
    assert len(reads) == 2
    conn.close()


def test_swaps_outside_the_five_minute_window_do_not_count():
    swaps = [(100, "0xin", 100.0), (99, "0xold", 900.0)]
    conn = _conn(swaps, blocks=[(100, AT - 5), (99, AT - 400)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)
    assert out["usd"] == pytest.approx(100.0) and out["swaps"] == 1


def test_a_quiet_but_live_feed_reports_zero_not_unknown():
    """No swaps, but the chain is delivering blocks: $0 is then a real measurement."""
    conn = _conn([], blocks=[(100, AT - 3)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)
    assert out["usd"] == 0.0 and out["swaps"] == 0
    assert out["newest_block_age_s"] == pytest.approx(3.0)


def test_a_stale_feed_is_unknown_never_zero():
    """The newest block we ingested is minutes old: we cannot say the coin was quiet."""
    conn = _conn([], blocks=[(100, AT - 600)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price, max_feed_age_s=120)
    assert out["usd"] is None and out["reason"] == "feed_stale"
    assert out["newest_block_age_s"] == pytest.approx(600.0)


def test_a_mint_with_no_known_pool_is_unknown():
    conn = _conn([], v4_pools=())
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)
    assert out["usd"] is None and out["reason"] == "no_known_pool"


def test_a_mint_that_also_trades_on_v2_reports_partial_coverage():
    """A number that omits one of the coin's pools is not the coin's volume, and says so. V3 is
    covered since 2026-09-22 (the tape subscribes to its Swap topic); V2 (about 1% of 4663 swap
    count) is not, so a V2 pool is what makes a read partial now."""
    swaps = [(100, "0xa", 500.0)]
    conn = _conn(swaps, v23_pools=(MINT,))
    conn.execute("UPDATE onchain_pool_protocols SET protocol='v2' WHERE protocol='v3'")
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)
    assert out["usd"] == pytest.approx(500.0)
    assert out["coverage"] == "partial"
    assert out["uncovered_protocols"] == ["v2"]
    assert "v3" in ov.COVERED_PROTOCOLS


def test_the_pools_summed_are_named_with_their_protocol():
    swaps = [(100, "0xa", 500.0), (100, "0xb", 250.0)]
    out = ov.onchain_volume_m5(_conn(swaps), CHAIN, MINT, AT, price_fn=_price)
    assert out["pools"] == [{"pool": POOL, "protocol": "v4", "swaps": 2, "usd": pytest.approx(750.0),
                             "buys": 2, "sells": 0, "txns": 2}]  # side counts still count logs


def test_an_unpriceable_quote_is_unknown_not_zero():
    swaps = [(100, "0xa", 500.0)]
    out = ov.onchain_volume_m5(_conn(swaps), CHAIN, MINT, AT,
                               price_fn=lambda _c, _t: None)
    assert out["usd"] is None and out["reason"] == "quote_not_priceable"


def test_a_tape_with_no_blocks_at_all_is_unknown():
    """A writer that has never run looks exactly like a quiet chain from a swap count alone."""
    conn = _conn([], blocks=[])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)
    assert out["usd"] is None and out["reason"] == "no_feed"
    assert out["newest_block_age_s"] is None


# ------------------------------------------------------- PERSON, from the chain (2026-09-16)
# Reconstructed live from the 4663 chain-owner endpoint: V4 PoolManager 0x8366a39c, pool
# 0xc093bcee..., the 5 minutes before block 64,490,052 (decision 11:57:28.521Z). Six swaps, all
# native-ETH quote, totalling 1.4385 ETH. The provider's whole m5 number that moment was $2,394.60
# -- which is the single 0.99 ETH swap from ~11 s earlier, and nothing else.
PERSON_SWAPS_ETH = [0.99000, 0.09900, 0.04828, 0.10321, 0.09900, 0.09900]
PERSON_ETH_USD = 2398.63
PERSON_FLOOR_USD = 2677.4898034556113
PERSON_PROVIDER_M5 = 2394.60


def test_person_from_the_real_chain_swaps_clears_the_floor():
    """The acceptance case, with the amounts the chain actually carried."""
    conn = _conn([(100, "0xreal%d" % i, eth * PERSON_ETH_USD)
                  for i, eth in enumerate(PERSON_SWAPS_ETH)], blocks=[(100, AT - 4)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)
    assert out["swaps"] == 6
    assert out["usd"] == pytest.approx(sum(PERSON_SWAPS_ETH) * PERSON_ETH_USD, rel=1e-6)
    assert out["usd"] > PERSON_FLOOR_USD, "chain truth clears the floor the provider view failed"
    assert out["usd"] > PERSON_PROVIDER_M5 * 1.4, "the provider saw well under half the volume"


def test_the_provider_saw_exactly_one_of_those_six_swaps():
    """Why the gate was blind: DexScreener's entire m5 number was the largest single swap."""
    biggest = max(PERSON_SWAPS_ETH) * PERSON_ETH_USD
    assert biggest == pytest.approx(PERSON_PROVIDER_M5, rel=0.02)
    assert sum(PERSON_SWAPS_ETH) * PERSON_ETH_USD > PERSON_FLOOR_USD > biggest


# ----------------------------------------------- unpriceable pools are skipped, not fatal (2026-09-16)
# Live 17:51Z: 314 of 488 active 4663 mints (64%) read quote_not_priceable, because ONE swap in a
# USDG-quoted pool voided the whole mint. The number is used only as a lower bound through max(),
# so a sum over the pools we CAN price stays a true lower bound; skipping the rest cannot admit a
# coin that the complete sum would refuse.
UNPRICED = "0xusdg0000000000000000000000000000000000"


def _with_unpriced_pool(conn, swaps):
    conn.execute("INSERT INTO onchain_pool_protocols VALUES (?,?,?,?)", (CHAIN, UNPRICED, "v4", MINT))
    conn.execute("INSERT INTO pool_meta VALUES (?,?,?,?,?,?,?,?)",
                 (CHAIN, UNPRICED, "0x" + "5f" * 20, MINT, None, "2026-09-16T00:00:00+00:00", None, 18))
    for i, (block, tx) in enumerate(swaps):
        conn.execute("INSERT INTO pool_ticks (chain_id, pool, kind, block_no, tx_sig, log_index, "
                     "amount0, amount1, observed_at, block_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (CHAIN, UNPRICED, "v4_swap", block, tx, 100 + i, "5000000", str(-10 ** 18),
                      "", _hash(block)))
    conn.commit()
    return conn


def test_an_unpriceable_pool_is_skipped_and_named_while_priced_pools_still_count():
    conn = _with_unpriced_pool(_conn([(100, "0xa", 500.0)]), [(100, "0xu1"), (100, "0xu2")])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)
    assert out["usd"] == pytest.approx(500.0) and out["swaps"] == 1
    assert out["reason"] == "ok"
    assert out["coverage"] == "partial"
    assert out["unpriced_pools"] == [{"pool": UNPRICED, "protocol": "v4", "swaps": 2}]


def test_a_mint_whose_every_swap_is_unpriceable_stays_unknown_not_zero():
    conn = _with_unpriced_pool(_conn([]), [(100, "0xu1")])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)
    assert out["usd"] is None and out["reason"] == "quote_not_priceable"
    assert out["unpriced_pools"] == [{"pool": UNPRICED, "protocol": "v4", "swaps": 1}]


def test_the_source_transaction_in_an_unpriceable_pool_is_excluded_not_reported():
    conn = _with_unpriced_pool(_conn([(100, "0xa", 500.0)]), [(100, "0xsource")])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, exclude_tx="0xsource", price_fn=_price)
    assert out["usd"] == pytest.approx(500.0)
    assert out["unpriced_pools"] == [] and out["coverage"] == "complete"


# --- RULE 3: an absent source transaction means the tape is blind ------------------------------
# MRLN (4663, 2026-09-23 13:02:39Z): the gate summed $0.11 over one Uniswap pool holding $2.51 and
# refused against a $259 floor, while the coin traded $3,721 in the same five minutes on Pons v2 --
# a venue this subscription does not ingest. The source wallet's own swap was missing from the tape,
# which is the one signal available at decision time that says so.

def test_a_source_transaction_the_tape_holds_leaves_the_measurement_alone():
    """The control. Nothing about a normal read changes."""
    swaps = [(100, "0xsource", 1000.0), (100, "0xother", 250.0)]
    out = ov.onchain_volume_m5(_conn(swaps), CHAIN, MINT, AT, exclude_tx="0xsource",
                               price_fn=_price)
    assert out["usd"] == pytest.approx(250.0)
    assert out["swaps"] == 1
    assert out["coverage"] == "complete"
    assert out["reason"] == "ok"
    assert out["excluded_tx_seen"] is True


def test_a_source_transaction_missing_from_the_tape_is_blind_not_a_small_number():
    """MRLN: other pools traded, but not the one the source used, so the sum is not the coin."""
    swaps = [(100, "0xdust", 0.11)]
    out = ov.onchain_volume_m5(_conn(swaps), CHAIN, MINT, AT, exclude_tx="0xsource",
                               price_fn=_price)
    assert out["usd"] is None, "a lower bound from the wrong venue must not reach the floor"
    assert out["reason"] == "source_tx_absent"
    assert out["coverage"] == "blind_source_tx_absent"
    assert out["excluded_tx_seen"] is False
    assert out["excluded_tx"] == "0xsource"
    # The diagnosis stays readable: what we DID see is still reported.
    assert out["swaps"] == 1
    assert [p["pool"] for p in out["pools"]] == [POOL]


def test_an_empty_window_with_a_missing_source_transaction_is_blind_not_a_measured_zero():
    """Without rule 3 this is the `$0 organic` refusal, and the feed looks healthy throughout."""
    out = ov.onchain_volume_m5(_conn([], blocks=[(100, AT - 3)]), CHAIN, MINT, AT,
                               exclude_tx="0xsource", price_fn=_price)
    assert out["usd"] is None and out["reason"] == "source_tx_absent"
    assert out["newest_block_age_s"] == pytest.approx(3.0)


def test_no_named_source_transaction_leaves_the_seen_flag_unset():
    """A read with nothing to exclude cannot be blind for this reason."""
    out = ov.onchain_volume_m5(_conn([(100, "0xa", 50.0)]), CHAIN, MINT, AT, price_fn=_price)
    assert out["excluded_tx_seen"] is None
    assert out["usd"] == pytest.approx(50.0) and out["reason"] == "ok"


def test_the_runner_treats_source_tx_absent_as_blind():
    """A SOURCE GUARD, because the whole defect would be one missing entry in that tuple.

    `usd: None` alone does not reach the fail-open: memecoin_real_runner only sets
    `m5_floor_undecidable` when the reason is in its blind set. A reason it does not know falls
    through to the provider comparison, which is the refusal this change exists to stop.
    """
    from pathlib import Path
    from sniper.services.memecoin_real_runner import ONCHAIN_M5_BLIND_REASONS
    src = Path("sniper/services/memecoin_real_runner.py").read_text()
    assert "_blind = _blind_reason in ONCHAIN_M5_BLIND_REASONS" in src
    assert "source_tx_absent" in ONCHAIN_M5_BLIND_REASONS


# --- an absent source transaction: venue gap, or a quiet coin? -----------------------------------

def test_an_absent_source_transaction_keeps_the_sum_it_did_produce():
    """`usd` stays None -- this function holds one instrument and must not answer a question it
    cannot see -- but the number is kept so a caller holding a second one can decide."""
    out = ov.onchain_volume_m5(_conn([(100, "0xa", 50.0)]), CHAIN, MINT, AT,
                               exclude_tx="0xsource", price_fn=_price)
    assert out["usd"] is None and out["reason"] == "source_tx_absent"
    assert out["source_absent_usd"] == pytest.approx(50.0)


def test_mrln_is_a_venue_gap_and_ily_is_a_quiet_coin():
    """The two live cases this rule exists to separate, 2026-09-23.

    MRLN (4663, 13:02:39Z): our covered pool held $2.51 and summed $0.11 while the coin did $3,721
    on Pons v2, a venue that subscription did not ingest. ILY (4663, 16:21:38Z): we summed $0 and
    the provider's $5.13 was ONE transaction, the source's own buy. Same flag, opposite answers.
    """
    blind, detail = ov.venue_gap_verdict(0.11, 3_721.87, min_usd=100.0, ratio=2.0)
    assert blind is True and detail["verdict"] == "venue_gap_blind"

    blind, detail = ov.venue_gap_verdict(0.0, 5.13, min_usd=100.0, ratio=2.0)
    assert blind is False and detail["verdict"] == "quiet_covered"
    assert detail["onchain_usd"] == 0.0 and detail["provider_organic_m5_usd"] == pytest.approx(5.13)


@pytest.mark.parametrize("provider", [None, float("nan"), -1.0, "", "n/a"])
def test_an_unreadable_provider_is_never_evidence_that_a_coin_is_quiet(provider):
    """UNKNOWN IS NEVER QUIET. A number we could not read must not retire the blind path."""
    blind, detail = ov.venue_gap_verdict(0.0, provider, min_usd=100.0, ratio=2.0)
    assert blind is True
    assert detail["venue_gap_reason"] == "provider_unknown"


def test_a_zero_sum_still_needs_the_absolute_gap_before_it_reads_blind():
    """Ours can be 0.0, so the ratio leg is a multiplication and every positive provider clears it.
    The absolute gap is then the only thing separating a quiet coin from a venue gap."""
    assert ov.venue_gap_verdict(0.0, 99.0, min_usd=100.0, ratio=2.0)[0] is False
    assert ov.venue_gap_verdict(0.0, 101.0, min_usd=100.0, ratio=2.0)[0] is True


def test_both_legs_are_required_not_either():
    # A big ratio on small money is not a venue gap.
    assert ov.venue_gap_verdict(1.0, 50.0, min_usd=100.0, ratio=2.0)[0] is False
    # A big absolute gap at a small ratio is not one either.
    assert ov.venue_gap_verdict(1_000.0, 1_500.0, min_usd=100.0, ratio=2.0)[0] is False
    assert ov.venue_gap_verdict(1_000.0, 3_000.0, min_usd=100.0, ratio=2.0)[0] is True


@pytest.mark.parametrize("min_usd,ratio", [(0.0, 2.0), (100.0, 0.0), (0.0, 0.0), (-1.0, 2.0)])
def test_zeroing_a_knob_means_blind_and_not_covered(min_usd, ratio):
    """The rollback has to mean what it says. With both knobs at 0 and a coin where ours and the
    provider are both 0, plain arithmetic would read COVERED -- the opposite of off."""
    blind, detail = ov.venue_gap_verdict(0.0, 0.0, min_usd=min_usd, ratio=ratio)
    assert blind is True
    assert detail["venue_gap_reason"] == "rule_disabled"


def test_the_runner_asks_the_provider_before_it_calls_an_absent_source_blind():
    """A SOURCE GUARD, like its sibling above: the defect would be this call going missing, and the
    row would silently return to blind on every absent source transaction."""
    from pathlib import Path
    src = Path("sniper/services/memecoin_real_runner.py").read_text()
    assert 'if _blind_reason == "source_tx_absent":' in src
    start = src.index('if _blind_reason == "source_tx_absent":')
    block = src[start:start + 2000]
    assert "venue_gap_verdict(" in block
    assert "organic_volume(" in block, "the provider side must be the existing organic rule"
    assert "partial_source_tx_absent_quiet" in block


# --- F1: a height is not an identity ------------------------------------------------------------

def test_an_orphaned_swap_from_a_replaced_block_is_not_counted_again():
    """The 2026-09-23 review's fixture: a reorg resets coverage, the backfill re-inserts the
    canonical swap, and the swap stored from the replaced block stays on disk with its old hash.
    Joining on the height alone summed both -- $4,800 over two rows where the chain holds $2,400
    over one. Nothing is deleted; the orphan simply stops matching the canonical header."""
    conn = _conn([(100, "0xcanonical", 2_400.0)], orphans=[(100, "0xorphan", 2_400.0)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)
    assert out["usd"] == pytest.approx(2_400.0)
    assert out["swaps"] == 1
    assert out["reason"] == "ok"
    assert out["orphan_swaps"] == 1, "the orphan is excluded and counted, not silently dropped"
    assert out["unverified_swaps"] == 0, "a mismatch is a decided fact, not an unknown"


def test_a_window_it_cannot_verify_is_unknown_and_never_quiet():
    """The deploy case. Every `block_times` row written before the hash column existed is NULL, so
    the strict join can verify nothing -- and after 225795fc a quiet window is a REFUSAL, not a
    blind. It must report unknown instead of a smaller number that looks measured."""
    conn = _conn([(100, "0xa", 500.0)], unverified_blocks=(100,))
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)
    assert out["usd"] is None
    assert out["reason"] == "hash_unverified"
    assert out["unverified_swaps"] == 1


def test_a_backfill_that_rewrites_the_header_restores_the_measurement():
    """`block_times` is INSERT OR REPLACE on both write paths, so the first backfill pass that
    re-observes a height writes its canonical hash and the window measures again."""
    conn = _conn([(100, "0xa", 500.0)], unverified_blocks=(100,))
    assert ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)["reason"] == "hash_unverified"
    conn.execute("INSERT OR REPLACE INTO block_times VALUES (?,?,?,?,?)",
                 (CHAIN, 100, AT - 10, "", _hash(100)))
    conn.commit()
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)
    assert out["usd"] == pytest.approx(500.0) and out["unverified_swaps"] == 0


def test_an_unverifiable_block_that_ages_out_of_the_window_stops_mattering():
    """A header seen live once before the migration and never backfilled stays NULL until it leaves
    the window, so the recovery is bounded by one window rather than by a restart."""
    conn = _conn([(100, "0xold", 500.0), (200, "0xnew", 250.0)],
                 blocks=[(100, AT - 400), (200, AT - 10)], unverified_blocks=(100,))
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, window_s=300.0, price_fn=_price)
    assert out["usd"] == pytest.approx(250.0), "the unverifiable block is outside the window"
    assert out["unverified_swaps"] == 0
    assert out["reason"] == "ok"


def test_the_canonical_hash_comparison_ignores_case():
    conn = _conn([(100, "0xa", 500.0)])
    conn.execute("UPDATE pool_ticks SET block_hash=upper(block_hash)")
    conn.commit()
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)
    assert out["usd"] == pytest.approx(500.0) and out["unverified_swaps"] == 0


# --- a subscription minutes behind on a healthy socket -------------------------------------------

def test_a_lagging_protocol_makes_its_coins_blind_not_quiet():
    """2026-09-23: publicnode let the V4 subscription drift to 652.9 s of delivery lag while V3 on
    the SAME socket stayed at 0.6-1.4 s and `newHeads` stayed current, so `newest_block_age_s` read
    fresh and the lane summed an empty V4 window as if it were a measurement."""
    conn = _conn([(100, "0xa", 500.0)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price,
                               protocol_lag_s={"v4": 652.9, "v3": 0.7}, max_protocol_lag_s=30.0)
    assert out["usd"] is None
    assert out["reason"] == "protocol_lagging"
    assert out["coverage"] == "blind_protocol_lagging"
    assert out["protocol_lag_s"] == pytest.approx(652.9)
    assert out["lagging_protocols"] == ["v4"]


def test_a_healthy_protocol_measures_exactly_as_before():
    """The control, at the measured healthy median."""
    conn = _conn([(100, "0xa", 500.0)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price,
                               protocol_lag_s={"v4": 0.7, "v3": 0.7}, max_protocol_lag_s=30.0)
    assert out["usd"] == pytest.approx(500.0) and out["reason"] == "ok"
    assert out["protocol_lag_s"] == pytest.approx(0.7)


def test_a_coin_is_judged_on_the_delivery_of_its_own_protocols():
    """This coin's pool is a 32-byte V4 id, so V3's lag says nothing about it, and the reverse."""
    conn = _conn([(100, "0xa", 500.0)])
    slow_v3 = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price,
                                   protocol_lag_s={"v4": 0.7, "v3": 900.0}, max_protocol_lag_s=30.0)
    assert slow_v3["usd"] == pytest.approx(500.0), "a V4 coin is not blinded by V3's delivery"


def test_no_lag_evidence_leaves_todays_behaviour_untouched():
    """Absent is unknown, never fresh AND never a refusal: a store from before the writer measured
    this, or a rolled-back writer, must gate exactly as it did."""
    conn = _conn([(100, "0xa", 500.0)])
    for lags in ({}, None, {"v4": None, "v3": None}):
        out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price,
                                   protocol_lag_s=lags, max_protocol_lag_s=30.0)
        assert out["usd"] == pytest.approx(500.0) and out["reason"] == "ok"
        assert out["protocol_lag_s"] is None


def test_a_zero_ceiling_disables_the_leg():
    conn = _conn([(100, "0xa", 500.0)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price,
                               protocol_lag_s={"v4": 652.9}, max_protocol_lag_s=0.0)
    assert out["usd"] == pytest.approx(500.0) and out["reason"] == "ok"


def test_the_runner_treats_a_lagging_protocol_as_blind():
    """A SOURCE GUARD, like its siblings: the whole fix is one entry in that tuple."""
    from sniper.services.memecoin_real_runner import ONCHAIN_M5_BLIND_REASONS
    assert "protocol_lagging" in ONCHAIN_M5_BLIND_REASONS


# --- covered_s is the window requested; covered_gap_s says how much of it we could not see -------

def test_a_lagging_window_reports_the_part_it_could_not_have_seen():
    """`covered_s` cannot fail: the three attempts inside the 2026-09-23 V4 stall each recorded
    `covered_s = 300.0` with zero swaps while 2,754 blocks were missing."""
    conn = _conn([(100, "0xa", 500.0)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price, window_s=300.0,
                               protocol_lag_s={"v4": 162.4}, max_protocol_lag_s=0.0)
    assert out["covered_gap_s"] == pytest.approx(162.4)
    assert out["usd"] == pytest.approx(500.0), "the leg is off; only the evidence changes"


def test_the_gap_can_never_make_a_window_read_as_more_covered():
    """Monotonic: `covered_s - covered_gap_s <= covered_s`, whatever the lag says."""
    conn = _conn([(100, "0xa", 500.0)])
    for lag in (0.0, 0.7, 299.9, 300.0, 652.9, 86_400.0):
        out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price, window_s=300.0,
                                   protocol_lag_s={"v4": lag}, max_protocol_lag_s=0.0)
        gap = out["covered_gap_s"]
        assert 0.0 <= gap <= 300.0, (lag, gap)


def test_a_healthy_window_has_no_gap():
    conn = _conn([(100, "0xa", 500.0)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price,
                               protocol_lag_s={"v4": 0.7}, max_protocol_lag_s=30.0)
    assert out["covered_gap_s"] == pytest.approx(0.7)
    assert out["reason"] == "ok"


def test_a_coin_with_no_covered_pool_has_an_unknown_gap_not_a_zero():
    """No covered pool means no subscription could be behind. Unknown, never 'nothing was lost'."""
    conn = _conn([(100, "0xa", 500.0)], v4_pools=())
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price,
                               protocol_lag_s={"v4": 652.9}, max_protocol_lag_s=30.0)
    assert out["reason"] == "no_known_pool"
    assert out["covered_gap_s"] is None


def test_no_lag_evidence_leaves_the_gap_unknown():
    conn = _conn([(100, "0xa", 500.0)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price)
    assert out["covered_gap_s"] is None and out["usd"] == pytest.approx(500.0)


def test_the_first_hour_after_this_deploy_reads_unverified_and_then_recovers():
    """Alpha's deploy case, 2026-09-23. Rows written by the old backfill carry NULL hashes and sit
    under ticks for as long as retention keeps them, so a window over them must read
    `hash_unverified` -- unknown, which the runner routes to blind -- and NEVER a quiet measured
    number. Once those heights age out of the window, the same coin measures normally."""
    conn = _conn([(100, "0xold", 500.0), (200, "0xnew", 250.0)],
                 blocks=[(100, AT - 400), (200, AT - 10)], unverified_blocks=(100,))
    inside = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, window_s=600.0, price_fn=_price)
    assert inside["usd"] is None
    assert inside["reason"] == "hash_unverified"
    assert inside["unverified_swaps"] == 1

    after = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, window_s=300.0, price_fn=_price)
    assert after["usd"] == pytest.approx(250.0)
    assert after["reason"] == "ok" and after["unverified_swaps"] == 0


# --- a subscription that went SILENT on a socket whose other subscriptions still speak -----------
# 2026-09-29 01:02Z, 8453: after a reconnect the V4 subscription delivered nothing while V3 carried
# 1,100-3,400 swaps a minute. The writer's V4 lag expired to NULL, the gate skipped it, and V4 coins
# were summed over a window with none of their swaps in it.

def test_a_silent_protocol_beside_a_measured_one_is_blind_not_quiet():
    conn = _conn([(100, "0xa", 500.0)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price,
                               protocol_lag_s={"v4": None, "v3": 0.7}, max_protocol_lag_s=30.0)
    assert out["usd"] is None
    assert out["reason"] == "protocol_lagging"
    assert out["coverage"] == "blind_protocol_silent"
    assert out["lagging_protocols"] == ["v4"]
    assert out["covered_gap_s"] == pytest.approx(300.0), "none of the window was seen"


def test_a_coin_whose_own_protocol_is_measured_is_not_blinded_by_a_silent_sibling():
    conn = _conn([(100, "0xa", 500.0)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price,
                               protocol_lag_s={"v4": 0.7, "v3": None}, max_protocol_lag_s=30.0)
    assert out["usd"] == pytest.approx(500.0) and out["reason"] == "ok"


def test_a_zero_ceiling_disables_the_silent_leg_too():
    conn = _conn([(100, "0xa", 500.0)])
    out = ov.onchain_volume_m5(conn, CHAIN, MINT, AT, price_fn=_price,
                               protocol_lag_s={"v4": None, "v3": 0.7}, max_protocol_lag_s=0.0)
    assert out["usd"] == pytest.approx(500.0) and out["reason"] == "ok"


# --- E7: the sell-share gate's own input, from the same read (2026-09-29, Alpha GO) -----------------
# Spec 2026-09-29-memecoin-dexscreener-out-e6-e7-design.md. Buys and sells are counted per pool in the
# loop that already sums the window; positive quote = INTO the pool = a buy (V4 rows since 05293598e).

def test_the_read_counts_buys_and_sells_per_pool_without_the_source_trade():
    swaps = [(100, "0xbuy1", 100.0), (100, "0xbuy2", 20.0), (100, "0xsell", -40.0),
             (100, "0xsrc", 50.0)]
    out = ov.onchain_volume_m5(_conn(swaps), CHAIN, MINT, AT, price_fn=_price, exclude_tx="0xsrc")
    pool = out["pools"][0]
    assert (pool["buys"], pool["sells"]) == (2, 1), pool
    split = ov.own_sell_share_split(out)
    assert split == {"txns_m5_buys": 2, "txns_m5_sells": 1, "pool": POOL,
                     "covered_s": out.get("covered_s")}


def test_a_v4_buy_through_the_real_decoder_counts_as_a_buy():
    """The split trusts the tape's sign, so it is pinned to the decoder: PoolManager emits the
    swapper's deltas (pays -100 of the quote, receives +55 of the coin) and the row must read IN."""
    from sniper.services.memecoin_tick_stream import decode_evm_log
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP

    def word(v):
        return f"{v + (1 << 256) if v < 0 else v:064x}"
    log = {"address": V4_POOL_MANAGERS[CHAIN], "topics": [V4_SWAP, "0x" + "cd" * 32, "0x" + "00" * 32],
           "data": "0x" + "".join(word(v) for v in (-100 * 10 ** 18, 55 * 10 ** 18, 1, 1, 0, 3000)),
           "blockNumber": hex(100), "logIndex": "0x0", "transactionHash": "0xv4buy", "blockHash": _hash(100)}
    row = decode_evm_log(CHAIN, log, "")
    quote = int(row["amount0"]) / 10 ** 18              # currency0 is the quote in this fixture
    out = ov.onchain_volume_m5(_conn([(100, "0xv4buy", quote)]), CHAIN, MINT, AT, price_fn=_price)
    assert (out["pools"][0]["buys"], out["pools"][0]["sells"]) == (1, 0), out["pools"]


def test_the_split_takes_the_pool_with_the_most_quote_usd():
    m5 = {"usd": 900.0, "reason": "ok", "covered_s": 300.0,
          "pools": [{"pool": "0xarb", "usd": 100.0, "swaps": 40, "buys": 20, "sells": 20},
                    {"pool": "0xpair", "usd": 800.0, "swaps": 10, "buys": 2, "sells": 8}]}
    assert ov.own_sell_share_split(m5)["pool"] == "0xpair"
    assert ov.own_sell_share_split(m5)["txns_m5_sells"] == 8


@pytest.mark.parametrize("m5", [None, {}, {"usd": None, "reason": "protocol_lagging", "pools": []},
                                {"usd": 5.0, "reason": "window_incomplete", "pools": [
                                    {"pool": "0xp", "usd": 5.0, "buys": 1, "sells": 9}]}])
def test_a_window_that_is_not_a_measurement_gives_no_split(m5):
    assert ov.own_sell_share_split(m5) is None


def test_the_quiet_source_absent_window_still_splits():
    m5 = {"usd": 50.0, "reason": "ok", "coverage": "partial_source_tx_absent_quiet", "covered_s": 300.0,
          "pools": [{"pool": "0xp", "usd": 50.0, "buys": 3, "sells": 1}]}
    assert ov.own_sell_share_split(m5)["txns_m5_buys"] == 3


def test_a_v4_pool_row_counts_by_the_into_pool_sign():
    """Gamma MED (2026-09-29): the other split tests use a 20-byte pool id, so nothing pinned the V4
    direction. On a 32-byte (66-char) V4 pool id a positive quote amount is a BUY, as on V3: V4 rows
    are into-pool since 05293598e, and nothing downstream may flip them again."""
    v4_pool = "0x" + "ab" * 32
    c = _conn([(100, "0xbuy", 100.0), (100, "0xbuy2", 30.0), (100, "0xsell", -40.0)])
    for table in ("pool_ticks", "pool_meta", "onchain_pool_protocols"):
        c.execute("UPDATE %s SET pool=? WHERE pool=?" % table, (v4_pool, POOL))
    c.commit()
    out = ov.onchain_volume_m5(c, CHAIN, MINT, AT, price_fn=_price)
    assert len(out["pools"][0]["pool"]) == 66
    assert (out["pools"][0]["buys"], out["pools"][0]["sells"]) == (2, 1), out["pools"]


# --- Pons gets its own delivery key (ULTRONICS, 4663, 2026-10-07 04:51:41Z) ------------------------
# The Pons topic-only subscription was confirmed and delivered nothing (0 frames in 60 s against 119
# chain events) while V3 on the same socket delivered 668. Pons delivery was noted under 'v3', so the
# dead subscription read healthy and a Pons-curve coin was summed over a stray 20%-fee V4 pool.

def _pons_conn(swaps):
    c = _conn(swaps, v4_pools=())
    c.execute("INSERT INTO onchain_pool_protocols VALUES (?,?,?,?)", (CHAIN, POOL, "pons_v2", MINT))
    c.commit()
    return c


def test_a_pons_coin_with_a_silent_pons_subscription_is_blind_not_quiet():
    out = ov.onchain_volume_m5(_pons_conn([(100, "0xa", 30.0)]), CHAIN, MINT, AT, price_fn=_price,
                               protocol_lag_s={"v4": 0.7, "v3": 0.6, "pons": None},
                               max_protocol_lag_s=30.0)
    assert out["usd"] is None and out["coverage"] == "blind_protocol_silent"
    assert out["lagging_protocols"] == ["pons"]


def test_a_pons_coin_with_measured_pons_delivery_is_summed():
    out = ov.onchain_volume_m5(_pons_conn([(100, "0xa", 30.0)]), CHAIN, MINT, AT, price_fn=_price,
                               protocol_lag_s={"v4": 0.7, "v3": None, "pons": 0.9},
                               max_protocol_lag_s=30.0)
    assert out["usd"] == pytest.approx(30.0) and out["reason"] == "ok"


def test_a_store_without_a_pons_lag_keeps_todays_behaviour():
    out = ov.onchain_volume_m5(_pons_conn([(100, "0xa", 30.0)]), CHAIN, MINT, AT, price_fn=_price,
                               protocol_lag_s={"v4": 0.7, "v3": 0.6}, max_protocol_lag_s=30.0)
    assert out["usd"] == pytest.approx(30.0) and out["reason"] == "ok"


# --- rule 3, per venue: the tape must hold the source's coin, not just its transaction ------------
# ULTRONICS: the Relay split route bought 92% from the Pons curve and 8% from a V4 pool. The V4 leg
# put the source tx on the tape, so "source tx seen" passed while the main venue was untaped.

@pytest.mark.parametrize("source_tokens, blind", [(12.0, True), (1.05, False), (None, False)])
def test_the_source_tx_must_put_most_of_its_coin_on_the_tape(source_tokens, blind):
    swaps = [(100, "0xsrc", 50.0), (100, "0xother", 20.0)]       # the source's leg: 1 token out
    out = ov.onchain_volume_m5(_conn(swaps), CHAIN, MINT, AT, price_fn=_price, exclude_tx="0xsrc",
                               source_tokens=source_tokens)
    if blind:
        assert out["usd"] is None and out["coverage"] == "blind_source_venue_untaped"
        assert out["reason"] == "source_venue_untaped"
        assert out["source_tokens_taped"] == pytest.approx(1.0)
    else:
        assert out["usd"] == pytest.approx(20.0) and out["reason"] == "ok"
