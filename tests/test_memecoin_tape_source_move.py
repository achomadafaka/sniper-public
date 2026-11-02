"""How far the COIN moved since the source's swap, from our own tape (spec
2026-10-08-memecoin-evm-entry-gates-onchain-design.md §3 E3; Gamma REVISE on 9a1396b33).

    move = coin price after the latest swap on the pool / coin price right after the source's swap - 1

Both ends are POST-swap spots (sqrtPrice after the swap) on the source's own pool, oriented by
pool_meta.quote_side: when the coin is token1 its price is 1/P, so a rising sqrtPrice is a FALLING
coin. Unknown, never 0, when the tape did not see the source transaction, the feed is unhealthy, the
source touched two of the coin's pools, or the pool carries no sqrtPrice (V2). Seen and nothing later
is a measured 0. Measurement only for now: the shadow beside the display move (build step 2).
"""
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_evm_tape_marks as T
from sniper.services import memecoin_volume_writer as V

CHAIN = "4663"
MINT = "0x" + "cc" * 20
QUOTE = "0x" + "11" * 20
POOL = "0x" + "aa" * 32
SRC = "0x" + "5e" * 32
NOW = 1_790_000_000.0
Q96 = 2 ** 96


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _tape(tmp_path, *, ticks, quote_side=1, feed_age=0.5, pools=(POOL,), kind="v3_swap"):
    """`ticks`: (pool, tx, block, sqrt or None)."""
    path = tmp_path / "volume.db"
    v = V.connect(str(path))
    for pool in pools:
        coin0 = quote_side == 1                 # quote is token1 -> the coin is token0
        v.execute("INSERT INTO pool_meta(chain_id, pool, token0, token1, quote_side, token0_decimals,"
                  " token1_decimals, resolved_at) VALUES(?,?,?,?,?,?,?,?)",
                  (CHAIN, pool, MINT if coin0 else QUOTE, QUOTE if coin0 else MINT, quote_side,
                   18, 18, NOW))
    for i, (pool, tx, block, sqrt_p) in enumerate(ticks, start=1):
        v.execute("INSERT INTO pool_ticks(chain_id, pool, kind, block_no, tx_sig, log_index,"
                  " observed_at, sqrt_price_x96, block_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                  (CHAIN, pool, kind, block, tx, i, _iso(NOW - 10 + block - 100),
                   None if sqrt_p is None else str(sqrt_p), "0xh%d" % block))
        v.execute("INSERT OR REPLACE INTO block_times(chain_id, block_no, block_ts, fetched_at,"
                  " block_hash) VALUES(?,?,?,?,?)",
                  (CHAIN, block, _iso(NOW - 10), _iso(NOW - 10), "0xh%d" % block))
    if feed_age is not None:
        v.execute("INSERT INTO volume_feed VALUES(?,?,?,?,?)",
                  (CHAIN, NOW - 300, NOW - feed_age, NOW - feed_age, 10))
    v.execute("INSERT INTO volume_feed_lag (chain_id,v4_lag_s,v3_lag_s,samples,observed) "
              "VALUES(?,?,?,?,?)", (CHAIN, 0.5, 0.6, 50, NOW - 1))
    v.commit(); v.close()
    return str(path)


def _move(path):
    return T.source_move(CHAIN, MINT, SRC, NOW, tape_path=path)


def test_coin_as_token0_a_rising_sqrt_price_is_a_rising_coin(tmp_path):
    path = _tape(tmp_path, ticks=[(POOL, SRC, 100, Q96), (POOL, "0x" + "22" * 32, 101, int(Q96 * 1.1))])
    out = _move(path)
    assert out["why"] == "ok" and out["move_pct"] == pytest.approx(21.0, abs=1e-6)


def test_coin_as_token1_a_rising_sqrt_price_is_a_falling_coin(tmp_path):
    path = _tape(tmp_path, quote_side=0,
                 ticks=[(POOL, SRC, 100, Q96), (POOL, "0x" + "22" * 32, 101, int(Q96 * 1.1))])
    out = _move(path)
    assert out["why"] == "ok" and out["move_pct"] == pytest.approx((1 / 1.21 - 1) * 100, abs=1e-6)


def test_the_base_is_the_source_swaps_post_swap_spot_its_last_tick(tmp_path):
    """A source transaction with two swaps on the pool (a hook leg): its LAST tick is the price the
    next buyer faces, so it is the base."""
    path = _tape(tmp_path, ticks=[(POOL, SRC, 100, Q96), (POOL, SRC, 100, int(Q96 * 2)),
                                  (POOL, "0x" + "22" * 32, 101, int(Q96 * 2))])
    assert _move(path)["move_pct"] == pytest.approx(0.0, abs=1e-9)


def test_seen_and_nothing_later_is_a_measured_zero(tmp_path):
    out = _move(_tape(tmp_path, ticks=[(POOL, SRC, 100, Q96)]))
    assert out["why"] == "ok" and out["move_pct"] == 0.0


def test_an_unseen_source_transaction_is_unknown_not_zero(tmp_path):
    out = _move(_tape(tmp_path, ticks=[(POOL, "0x" + "22" * 32, 101, Q96)]))
    assert out["move_pct"] is None and out["why"] == "source_unseen"


def test_an_unhealthy_feed_is_unknown(tmp_path):
    out = _move(_tape(tmp_path, feed_age=30.0, ticks=[(POOL, SRC, 100, Q96)]))
    assert out["move_pct"] is None and out["why"] == "tape_unhealthy"


def test_a_source_across_two_of_the_coins_pools_is_unknown(tmp_path):
    other = "0x" + "bb" * 32
    out = _move(_tape(tmp_path, pools=(POOL, other),
                      ticks=[(POOL, SRC, 100, Q96), (other, SRC, 100, Q96)]))
    assert out["move_pct"] is None and out["why"] == "multi_pool_source"


def test_a_pool_without_a_sqrt_price_is_unknown(tmp_path):
    out = _move(_tape(tmp_path, kind="v2_swap", ticks=[(POOL, SRC, 100, None)]))
    assert out["move_pct"] is None and out["why"] == "source_unseen"


def test_no_store_is_unknown_without_raising(tmp_path):
    out = T.source_move(CHAIN, MINT, SRC, NOW, tape_path=str(tmp_path / "absent.db"))
    assert out["move_pct"] is None and out["why"] == "tape_absent"


# --- the entry-gate shadow (measurement only) ---------------------------------------------------------

class _Trace:
    def __init__(self):
        self.marks = []

    def mark_substage(self, name, **fields):
        self.marks.append((name, fields))


class _Ev:
    mint = MINT
    their_price = 1.0e-4
    fomo_event_id = "swap:raybot:%s:0x%s:%s" % (SRC, "ab" * 20, MINT)


def test_the_gate_shadow_records_the_tape_move_beside_the_display_move(monkeypatch):
    from sniper.services import memecoin_real_runner as R
    calls = []
    monkeypatch.setattr(T, "source_move", lambda chain, mint, tx, now: calls.append(tx) or {
        "move_pct": 12.5, "why": "ok", "pool": POOL, "source_block": 1, "latest_block": 2})
    # Own pool: the shared momentum pool's queue was full from other tests in this xdist worker,
    # so the future came back None (deploy fc24af7f3 refused).
    from sniper.services.memecoin_shadow_reads import ShadowReads
    pool = ShadowReads(workers=1, maxsize=4)
    monkeypatch.setattr(R, "_momentum_read_pool", lambda: pool)
    trace = _Trace()
    R._shadow_tape_move(trace, _Ev(), "4663", {"price": 1.2e-4})
    trace.tape_move_shadow["future"].result(timeout=1)
    R._tape_move_late_best_effort(trace)
    name, f = trace.marks[-1]
    assert name in {"chase_tape_move_shadow", "chase_tape_move_shadow_late"} and calls == [SRC]
    assert f["move_pct"] == 12.5 and f["display_move_pct"] == pytest.approx(20.0)
    assert f["read_ms"] >= 0


@pytest.mark.parametrize("chain", ["1", "56", "5042", "solana"])
def test_the_gate_shadow_reads_nothing_off_the_tape_chains(monkeypatch, chain):
    from sniper.services import memecoin_real_runner as R
    monkeypatch.setattr(T, "source_move", lambda *a: pytest.fail("read off a tape chain"))
    trace = _Trace()
    R._shadow_tape_move(trace, _Ev(), chain, {"price": 1.2e-4})
    assert trace.marks == []


def test_a_failing_shadow_never_raises_into_the_gate(monkeypatch):
    from sniper.services import memecoin_real_runner as R

    def boom(*_a):
        raise RuntimeError("tape")
    monkeypatch.setattr(T, "source_move", boom)
    R._shadow_tape_move(_Trace(), _Ev(), "4663", {})
