"""Task 1 — token decimals cached in pool_meta, bounded and fail-closed."""
import sqlite3

from sniper.analysis import tick_features as tf


def _tc():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    tf.ensure_feature_tables(c)
    return c


def _eth_call(responses):
    """Injected seam: {(address, selector): hex}. Missing -> None (a failed call)."""
    def call(addr, selector):
        return responses.get((addr, selector))
    return call


def test_decimals_are_resolved_and_cached():
    tc = _tc()
    quote, base = "0x" + "1" * 40, "0x" + "2" * 40
    calls = {
        ("0xp", tf.SEL_TOKEN0): "0x" + "0" * 24 + quote[2:],
        ("0xp", tf.SEL_TOKEN1): "0x" + "0" * 24 + base[2:],
        (quote, tf.SEL_DECIMALS): hex(18),
        (base, tf.SEL_DECIMALS): hex(9),
    }
    tf.resolve_pool_meta(tc, "4663", ["0xp"], _eth_call(calls), {quote}, "t")
    r = tc.execute("SELECT quote_side, token0_decimals, token1_decimals FROM pool_meta").fetchone()
    assert r["quote_side"] == 0
    assert r["token0_decimals"] == 18 and r["token1_decimals"] == 9


def test_failed_decimals_call_leaves_null_not_a_guess():
    tc = _tc()
    quote, base = "0x" + "1" * 40, "0x" + "2" * 40
    calls = {
        ("0xp", tf.SEL_TOKEN0): "0x" + "0" * 24 + quote[2:],
        ("0xp", tf.SEL_TOKEN1): "0x" + "0" * 24 + base[2:],
        (quote, tf.SEL_DECIMALS): hex(18),
        # base decimals call fails -> absent
    }
    tf.resolve_pool_meta(tc, "4663", ["0xp"], _eth_call(calls), {quote}, "t")
    r = tc.execute("SELECT token0_decimals, token1_decimals FROM pool_meta").fetchone()
    assert r["token0_decimals"] == 18
    assert r["token1_decimals"] is None      # never 18, never 0, never guessed


def test_non_decimals_response_is_rejected_not_stored():
    """Regression: a contract without decimals() (or a node echoing another word) returns an
    address-shaped value. int() of that is astronomically large — storing it would be a garbage
    scale, and it overflowed SQLite outright. ERC-20 decimals is a uint8; anything else is None."""
    tc = _tc()
    quote, base = "0x" + "1" * 40, "0x" + "2" * 40
    calls = {
        ("0xp", tf.SEL_TOKEN0): "0x" + "0" * 24 + quote[2:],
        ("0xp", tf.SEL_TOKEN1): "0x" + "0" * 24 + base[2:],
        (quote, tf.SEL_DECIMALS): "0x" + "0" * 24 + quote[2:],   # address-shaped, not a uint8
        (base, tf.SEL_DECIMALS): hex(256),                        # one past uint8
    }
    tf.resolve_pool_meta(tc, "4663", ["0xp"], _eth_call(calls), {quote}, "t")
    r = tc.execute("SELECT token0_decimals, token1_decimals FROM pool_meta").fetchone()
    assert r["token0_decimals"] is None and r["token1_decimals"] is None


def test_existing_pool_missing_decimals_is_backfilled():
    """Production defect: pools resolved BEFORE the decimals columns existed are already 'known',
    so a naive skip-if-known leaves their decimals NULL forever and the tape price never works.
    A usable pool (quote_side set) with missing decimals must be revisited."""
    tc = _tc()
    quote, base = "0x" + "1" * 40, "0x" + "2" * 40
    # simulate a pre-existing row: orientation resolved, decimals absent
    tc.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, quote_side, resolved_at)"
               " VALUES ('4663','0xp',?,?,0,'old')", (quote, base))
    tc.commit()
    calls = {
        ("0xp", tf.SEL_TOKEN0): "0x" + "0" * 24 + quote[2:],
        ("0xp", tf.SEL_TOKEN1): "0x" + "0" * 24 + base[2:],
        (quote, tf.SEL_DECIMALS): hex(18),
        (base, tf.SEL_DECIMALS): hex(6),
    }
    tf.resolve_pool_meta(tc, "4663", ["0xp"], _eth_call(calls), {quote}, "t")
    r = tc.execute("SELECT token0_decimals, token1_decimals FROM pool_meta").fetchone()
    assert r["token0_decimals"] == 18 and r["token1_decimals"] == 6


def test_pool_with_unusable_quote_side_is_not_retried_forever():
    """A pool whose quote token is not a known quote asset has quote_side NULL. It has been checked
    and is not usable — it must stay 'known' so it never consumes the resolution budget again."""
    tc = _tc()
    other, base = "0x" + "9" * 40, "0x" + "2" * 40
    tc.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, quote_side, resolved_at)"
               " VALUES ('4663','0xp',?,?,NULL,'old')", (other, base))
    tc.commit()
    seen = []

    def call(addr, selector):
        seen.append((addr, selector))
        return None

    tf.resolve_pool_meta(tc, "4663", ["0xp"], call, {"0x" + "1" * 40}, "t")
    assert seen == []          # not revisited


def test_no_db_write_happens_before_the_network_calls_finish():
    """Regression, 2026-07-24: writing per pool held the ticks-DB write lock across every eth_call,
    starving the tickstream's set_cursor — 'database is locked' went 1/day to 3,333 the day this job
    first ran. All network I/O must complete BEFORE the single short write transaction."""
    tc = _tc()
    quote, base = "0x" + "1" * 40, "0x" + "2" * 40
    writes_seen_during_calls = []

    def call(addr, selector):
        # if any pool_meta row exists while calls are still in flight, the lock is being held open
        writes_seen_during_calls.append(
            tc.execute("SELECT COUNT(*) FROM pool_meta").fetchone()[0])
        return {(("0xp"), tf.SEL_TOKEN0): "0x" + "0" * 24 + quote[2:],
                (("0xp"), tf.SEL_TOKEN1): "0x" + "0" * 24 + base[2:],
                (quote, tf.SEL_DECIMALS): hex(18),
                (base, tf.SEL_DECIMALS): hex(9)}.get((addr, selector))

    tf.resolve_pool_meta(tc, "4663", ["0xp"], call, {quote}, "t")
    assert writes_seen_during_calls and max(writes_seen_during_calls) == 0
    assert tc.execute("SELECT COUNT(*) FROM pool_meta").fetchone()[0] == 1   # written after
