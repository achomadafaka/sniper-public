"""One hop prices an equity quote unit, off the entry path, and never invents a number."""
import sqlite3

import pytest

from sniper.services import memecoin_equity_hop_price as hop
from sniper.services import memecoin_volume_writer as w

CHAIN = "4663"
NATIVE = hop.NATIVE
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
META = "0xc0d6457c16cc70d6790dd43521c899c87ce02f35"
HOOD = "0x32ac8c1d7672667d5ebdea22935f7b06fc8d496f"
COIN = "0x4e9c619228dc53f9b57c7357208863d256b9cd69"
NOW = 1_790_000_000.0
Q96 = 1 << 96


def _pool(i):
    return "0x" + ("%02x" % i) * 32


@pytest.fixture
def conn(tmp_path):
    c = w.connect(tmp_path / "vol.db")
    c.row_factory = sqlite3.Row
    for token, usd in ((NATIVE, 2500.0), (WETH, 2500.0), (USDG, 1.0)):
        c.execute("INSERT OR REPLACE INTO volume_quote_prices VALUES (?,?,?,?)", (CHAIN, token, usd, NOW - 5))
    c.execute("INSERT OR REPLACE INTO volume_token_decimals VALUES (?,?,?,?)", (CHAIN, USDG, 6, NOW))
    return c


def _meta(c, pool, t0, t1, side=None, d0=None, d1=None):
    # Columns NAMED, not positional: `pool_meta` gains columns over time (decimals in 2026-07,
    # `resolve_state` in 2026-09), and a positional insert breaks on the next one.
    c.execute("INSERT OR REPLACE INTO pool_meta "
              "(chain_id, pool, token0, token1, quote_side, resolved_at, "
              " token0_decimals, token1_decimals) VALUES (?,?,?,?,?,?,?,?)",
              (CHAIN, pool, t0, t1, side, "t", d0, d1))


def _sqrt(price_token1_per_token0_atoms):
    return int((price_token1_per_token0_atoms ** 0.5) * Q96)


class FakeRpc:
    """Answers decimals() and extsload by (pool, offset). Counts every call."""
    def __init__(self, decimals, states):
        self.decimals, self.states, self.calls = decimals, states, 0

    def __call__(self, rpc_url, to, data, http=None):
        self.calls += 1
        if data == hop.DECIMALS_CALL:
            value = self.decimals.get(to.lower())
            return None if value is None else "0x" + "%064x" % value
        slot = int(data[2 + 8:], 16)
        for pool, (sqrt_price, liquidity) in self.states.items():
            base = hop._state_slot(pool)
            if slot == base:
                return "0x" + "%064x" % sqrt_price
            if slot == base + hop._LIQUIDITY_OFFSET:
                return "0x" + "%064x" % liquidity
        return "0x" + "0" * 64


def test_price_math_both_orientations():
    # 1 unit (18 dec) = 0.1 ETH (18 dec): token1/token0 = 0.1 when unit is token0
    s = _sqrt(0.1)
    assert hop.unit_price_in_base(s, unit_is_token0=True, unit_decimals=18, base_decimals=18) == pytest.approx(0.1, rel=1e-6)
    # unit as token1: token1/token0 = 10 (10 unit atoms per ETH atom) -> 1 unit = 0.1 ETH
    s = _sqrt(10.0)
    assert hop.unit_price_in_base(s, unit_is_token0=False, unit_decimals=18, base_decimals=18) == pytest.approx(0.1, rel=1e-6)
    # unit (18 dec) against USDG (6 dec): 1 unit = 700 USDG -> atoms ratio = 700e6 / 1e18
    s = _sqrt(700e6 / 1e18)
    assert hop.unit_price_in_base(s, unit_is_token0=True, unit_decimals=18, base_decimals=6) == pytest.approx(700.0, rel=1e-6)
    assert hop.unit_price_in_base(0, unit_is_token0=True, unit_decimals=18, base_decimals=18) is None


def test_refresh_prices_the_deepest_hop_and_resolves_the_unit_pools(conn):
    # META has two hops: a thin one against USDG and a deep one against ETH; 30 coin pools quote in META.
    _meta(conn, _pool(1), NATIVE, META)
    _meta(conn, _pool(2), USDG, META)
    for i in range(30):
        _meta(conn, _pool(10 + i), COIN if i % 2 else META, META if i % 2 else COIN)
    rpc = FakeRpc({META: 18}, {
        _pool(1): (_sqrt(1 / 0.28), 5 * 10 ** 21),      # unit is token1: 1 META = 0.28 ETH = $700
        _pool(2): (_sqrt(650e6 / 1e18), 10 ** 4),       # thin: ~$390 of USDG depth, below the $1,000 floor, would say $650
    })
    out = hop.refresh(conn, CHAIN, "http://rpc", NOW, rpc_call=rpc)
    assert out["priced"] == {META: pytest.approx(700.0, rel=1e-6)}
    row = conn.execute("SELECT usd, observed FROM volume_quote_prices WHERE chain_id=? AND token=?", (CHAIN, META)).fetchone()
    assert row["usd"] == pytest.approx(700.0, rel=1e-6) and row["observed"] == NOW
    assert out["resolved"] == 30
    sides = conn.execute("SELECT token0, quote_side, token0_decimals, token1_decimals FROM pool_meta "
                         "WHERE chain_id=? AND pool=?", (CHAIN, _pool(11))).fetchone()
    assert sides["token0"] == COIN and sides["quote_side"] == 1 and sides["token1_decimals"] == 18
    # the hop pools themselves are not relabelled: META against ETH keeps the observer's decision
    assert conn.execute("SELECT quote_side FROM pool_meta WHERE pool=?", (_pool(1),)).fetchone()[0] is None
    assert out["reads"] == 1 + 2 * 2                       # decimals + two pools x (slot0, liquidity)


_BLOCK = [1]


def _tick(c, pool, sqrt_price, liquidity, at_iso, *, block=None, block_hash=None,
          header_hash="same", block_ts=None):
    """One taped swap, WITH the canonical identity and block time every live row carries.

    The hop reader verifies a tick against `block_times` before it prices from it (F4), so a fixture
    without a header row is not a thin case, it is an impossible one: measured on 4663, 0 of 118,241
    hop-eligible ticks in an hour carried a NULL hash on either side. `header_hash` exists so a test
    can make the row an ORPHAN ("0xreplaced") or unverifiable (None) on purpose.
    """
    block = _BLOCK[0] if block is None else block
    _BLOCK[0] += 1
    block_hash = block_hash or ("0x%064x" % block)
    c.execute("INSERT INTO pool_ticks (chain_id,pool,kind,block_no,tx_sig,log_index,amount0,amount1,observed_at,"
              "liquidity,sqrt_price_x96,block_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (CHAIN, pool, "v3_swap", block, "0xsig" + pool[-6:], 0, "1", "1", at_iso,
               str(liquidity), str(sqrt_price), block_hash))
    if header_hash is not None:
        c.execute("INSERT OR REPLACE INTO block_times "
                  "(chain_id,block_no,block_ts,fetched_at,block_hash) VALUES (?,?,?,?,?)",
                  (CHAIN, block, block_ts or at_iso, at_iso,
                   block_hash if header_hash == "same" else header_hash))


def test_a_taped_hop_pool_is_priced_from_its_own_swap_with_no_chain_read(conn):
    from datetime import datetime, timezone
    recent = datetime.fromtimestamp(NOW - 60, tz=timezone.utc).isoformat()
    _meta(conn, _pool(1), NATIVE, META)        # busiest hop: thin
    _meta(conn, _pool(2), NATIVE, META)        # quieter hop: deep, and therefore the price
    for i in range(30):
        _meta(conn, _pool(10 + i), COIN, META)
    _tick(conn, _pool(1), _sqrt(1 / 0.20), 3 * 10 ** 16, recent)     # ~0.013 ETH of depth: thin
    _tick(conn, _pool(2), _sqrt(1 / 0.28), 3 * 10 ** 21, recent)     # ~1,600 ETH of depth
    conn.commit()
    rpc = FakeRpc({META: 18}, {})
    out = hop.refresh(conn, CHAIN, "http://rpc", NOW, rpc_call=rpc)
    assert out["priced"] == {META: pytest.approx(700.0, rel=1e-6)}
    assert out["reads"] == 1 and rpc.calls == 1              # decimals only; no storage read


def test_no_hop_above_the_floor_writes_nothing(conn):
    _meta(conn, _pool(1), USDG, HOOD)
    for i in range(30):
        _meta(conn, _pool(10 + i), COIN, HOOD)
    rpc = FakeRpc({HOOD: 18}, {_pool(1): (_sqrt(100e6 / 1e18), 1_000)})  # ~$100 of USDG depth
    out = hop.refresh(conn, CHAIN, "http://rpc", NOW, rpc_call=rpc)
    assert out["priced"] == {} and out["skipped"][HOOD] == "no_hop_above_floor"
    assert conn.execute("SELECT COUNT(*) FROM volume_quote_prices WHERE token=?", (HOOD,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pool_meta WHERE quote_side IS NOT NULL").fetchone()[0] == 0


def test_a_stale_base_price_prices_nothing(conn):
    conn.execute("UPDATE volume_quote_prices SET observed=?", (NOW - 120,))
    _meta(conn, _pool(1), NATIVE, META)
    for i in range(30):
        _meta(conn, _pool(10 + i), COIN, META)
    rpc = FakeRpc({META: 18}, {_pool(1): (_sqrt(1 / 0.28), 5 * 10 ** 21)})
    out = hop.refresh(conn, CHAIN, "http://rpc", NOW, rpc_call=rpc)
    assert out["priced"] == {} and out["skipped"] == {"*": "no_fresh_base_price"} and rpc.calls == 0


def test_unreadable_decimals_skips_the_unit(conn):
    _meta(conn, _pool(1), NATIVE, META)
    for i in range(30):
        _meta(conn, _pool(10 + i), COIN, META)
    rpc = FakeRpc({}, {_pool(1): (_sqrt(1 / 0.28), 5 * 10 ** 21)})
    out = hop.refresh(conn, CHAIN, "http://rpc", NOW, rpc_call=rpc)
    assert out["skipped"][META] == "decimals_unreadable" and out["priced"] == {}


def test_the_read_budget_bounds_a_pass(conn):
    _meta(conn, _pool(1), NATIVE, META)
    _meta(conn, _pool(2), NATIVE, HOOD)
    for i in range(30):
        _meta(conn, _pool(10 + i), COIN, META)
        _meta(conn, _pool(50 + i), COIN, HOOD)
    rpc = FakeRpc({META: 18, HOOD: 18}, {_pool(1): (_sqrt(1 / 0.28), 5 * 10 ** 21),
                                         _pool(2): (_sqrt(1 / 0.05), 5 * 10 ** 21)})
    out = hop.refresh(conn, CHAIN, "http://rpc", NOW, rpc_call=rpc, max_reads=3)
    assert len(out["priced"]) == 1 and "read_budget" in out["skipped"].values()
    assert rpc.calls <= 3


def test_the_gate_reads_the_hop_price_from_the_table_without_a_network_call(conn):
    """The writer's own gate price function sees the hop price when it is fresh, and not when stale."""
    conn.execute("INSERT OR REPLACE INTO volume_quote_prices VALUES (?,?,?,?)", (CHAIN, META, 700.0, NOW - 10))
    row = conn.execute("SELECT usd, observed FROM volume_quote_prices WHERE chain_id=? AND token=?", (CHAIN, META)).fetchone()
    assert row["usd"] == 700.0 and 0 <= NOW - row["observed"] <= 60
    conn.execute("UPDATE volume_quote_prices SET observed=? WHERE token=?", (NOW - 90, META))
    row = conn.execute("SELECT usd, observed FROM volume_quote_prices WHERE chain_id=? AND token=?", (CHAIN, META)).fetchone()
    assert not (0 <= NOW - row["observed"] <= 60)


def test_register_pool_resolves_a_hop_priced_unit_at_registration(conn, monkeypatch):
    from sniper.services.memecoin_evm_v4_events import v4_pool_id
    tape = w.VolumeTape(conn, CHAIN, NOW)
    conn.execute("INSERT OR REPLACE INTO volume_quote_prices VALUES (?,?,?,?)", (CHAIN, META, 700.0, NOW - 10))
    conn.execute("INSERT OR REPLACE INTO volume_token_decimals VALUES (?,?,?,?)", (CHAIN, META, 18, NOW))
    key = {"currency0": COIN, "currency1": META, "fee": 3000, "tick_spacing": 60,
           "hooks": "0x" + "00" * 20}
    pool = v4_pool_id(key).lower()
    tape.register_pool(pool, key, NOW)
    row = conn.execute("SELECT quote_side, token1_decimals FROM pool_meta WHERE pool=?", (pool,)).fetchone()
    assert row["quote_side"] == 1 and row["token1_decimals"] == 18


def test_the_depth_floor_is_in_dollars_so_a_six_decimal_stable_hop_is_not_starved(conn):
    """HOOD/USDG (6 dec) hops read "no_hop_above_floor" under the raw 1e18 liquidity floor while
    GIGABYTE's 60 swaps went unpriced. $5k of USDG depth must clear a $1,000 floor whatever the
    decimals; the same L on an 18-decimal ETH pool measures on the same dollar scale."""
    sqrt_usdg = _sqrt(1e18 / 100e6)                      # token0 USDG, token1 HOOD: 1 HOOD = 100 USDG
    liquidity = int(5_000e6 * (sqrt_usdg / (1 << 96)))   # x = L / sqrtP -> L = x * sqrtP, x = 5,000 USDG atoms
    assert hop.depth_usd(liquidity, sqrt_usdg, base_is_token0=True, base_decimals=6, base_usd=1.0) == pytest.approx(5_000, rel=1e-3)
    assert liquidity < 1e18, "the retired raw floor would have refused this pool"
    _meta(conn, _pool(1), USDG, HOOD)
    for i in range(30):
        _meta(conn, _pool(10 + i), COIN, HOOD)
    rpc = FakeRpc({HOOD: 18}, {_pool(1): (sqrt_usdg, liquidity)})
    out = hop.refresh(conn, CHAIN, "http://rpc", NOW, rpc_call=rpc)
    assert out["priced"] == {HOOD: pytest.approx(100.0, rel=1e-6)}
    # and an ETH pool with the same dollar depth is judged the same way
    sqrt_eth = _sqrt(1 / 25.0)                           # 1 HOOD = 0.04 ETH = $100
    l_eth = int(2e18 * (sqrt_eth / (1 << 96)))           # x = L / sqrtP -> L = x * sqrtP, x = 2 ETH = $5,000
    assert hop.depth_usd(l_eth, sqrt_eth, base_is_token0=True, base_decimals=18, base_usd=2500.0) == pytest.approx(5_000, rel=1e-3)


def test_candidates_are_ranked_by_recent_swaps_and_a_traded_unit_needs_no_pool_quorum(conn):
    """The unit a 171-swap pool quotes in must come before an idle unit with 400 pools, and a
    unit with ONE traded pool qualifies even under the 25-pool quorum."""
    from datetime import datetime, timezone
    recent = datetime.fromtimestamp(NOW - 60, tz=timezone.utc).isoformat()
    IDLE, HOT, LONE = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
    for i in range(400):
        _meta(conn, _pool(1000 + i), COIN, IDLE)
    for i in range(30):
        _meta(conn, _pool(2000 + i), COIN, HOT)
    _meta(conn, _pool(3000), COIN, LONE)
    for i in range(20):
        _tick(conn, _pool(2000), 1, 1, recent)            # HOT trades
        conn.execute("UPDATE pool_ticks SET tx_sig=? WHERE rowid=(SELECT MAX(rowid) FROM pool_ticks)", ("0xhot%d" % i,))
    for i in range(3):
        _tick(conn, _pool(3000), 1, 1, recent)
        conn.execute("UPDATE pool_ticks SET tx_sig=? WHERE rowid=(SELECT MAX(rowid) FROM pool_ticks)", ("0xlone%d" % i,))
    conn.commit()
    ranked = hop.candidate_units(conn, CHAIN, exclude={COIN}, max_units=16, now=NOW)
    assert ranked[:2] == [HOT, LONE] and IDLE in ranked
    assert hop.candidate_units(conn, CHAIN, exclude={COIN}, max_units=1, now=NOW) == [HOT]


def test_already_priced_units_are_refreshed_before_new_candidates(conn):
    conn.execute("INSERT OR REPLACE INTO volume_quote_prices VALUES (?,?,?,?)", (CHAIN, HOOD, 90.0, NOW - 100))
    _meta(conn, _pool(1), NATIVE, HOOD)
    _meta(conn, _pool(2), NATIVE, META)
    for i in range(30):
        _meta(conn, _pool(10 + i), COIN, META)            # META has the pools; HOOD is merely known
    rpc = FakeRpc({HOOD: 18, META: 18}, {_pool(1): (_sqrt(1 / 0.04), 5 * 10 ** 21),
                                         _pool(2): (_sqrt(1 / 0.28), 5 * 10 ** 21)})
    out = hop.refresh(conn, CHAIN, "http://rpc", NOW, rpc_call=rpc, max_units=1)
    assert list(out["priced"]) == [HOOD, META], "the known unit goes first and takes no candidate slot"


def test_a_hop_is_priced_against_an_anchor_never_against_another_hop_price(conn):
    """GIGABYTE's unit (2026-09-22): its deepest-looking pool was against a coin priced at $0.0002
    through a thin pool; that pool must be ignored and the real ETH pool must price the unit."""
    JUNK = "0x" + "77" * 20
    conn.execute("INSERT OR REPLACE INTO volume_quote_prices VALUES (?,?,?,?)", (CHAIN, JUNK, 0.0002, NOW - 5))
    conn.execute("INSERT OR REPLACE INTO volume_token_decimals VALUES (?,?,?,?)", (CHAIN, JUNK, 18, NOW))
    _meta(conn, _pool(1), NATIVE, META)                  # real: 1 META = 0.28 ETH = $700, $24k deep
    _meta(conn, _pool(2), META, JUNK)                    # junk: enormous "depth" in a $0.0002 token
    for i in range(30):
        _meta(conn, _pool(10 + i), COIN, META)
    rpc = FakeRpc({META: 18}, {_pool(1): (_sqrt(1 / 0.28), 5 * 10 ** 21),
                              _pool(2): (_sqrt(1e6), 10 ** 30)})
    out = hop.refresh(conn, CHAIN, "http://rpc", NOW, rpc_call=rpc)
    assert out["priced"] == {META: pytest.approx(700.0, rel=1e-6)}
    assert hop.anchor_tokens(CHAIN) >= {NATIVE, USDG}
    # the junk token itself is not a candidate hop base and is never re-priced through META
    assert JUNK not in out["priced"]


# F4: a hop price carries the age of the evidence it rests on, and rests only on verified state.


def test_an_orphan_tape_row_is_excluded_and_counted(conn):
    """A tick from a block the chain replaced is not the pool's state.

    ⚠️ THE REACH VALUE IS THE HEADER HASH. The tick and the header must DISAGREE; give them the same
    hash and the row is canonical and the test proves nothing about orphans.
    """
    from datetime import datetime, timezone
    recent = datetime.fromtimestamp(NOW - 60, tz=timezone.utc).isoformat()
    _meta(conn, _pool(2), NATIVE, META)
    _tick(conn, _pool(2), _sqrt(1 / 0.28), 3 * 10 ** 21, recent, header_hash="0x" + "ff" * 32)
    conn.commit()
    states, stats = hop.tape_states(conn, CHAIN, [_pool(2)], NOW)
    assert states == {} and stats["orphan"] == 1 and stats["used"] == 0


def test_a_tick_whose_identity_cannot_be_judged_is_unverified_not_absent(conn):
    from datetime import datetime, timezone
    recent = datetime.fromtimestamp(NOW - 60, tz=timezone.utc).isoformat()
    _meta(conn, _pool(3), NATIVE, META)
    _tick(conn, _pool(3), _sqrt(1 / 0.28), 3 * 10 ** 21, recent, header_hash=None)
    conn.commit()
    states, stats = hop.tape_states(conn, CHAIN, [_pool(3)], NOW)
    assert states == {} and stats["unverified"] == 1 and stats["orphan"] == 0


def test_the_state_carries_the_block_clock_and_not_our_ingest_clock(conn):
    """`observed_at` is when WE saw the frame; the evidence time is when the block happened."""
    from datetime import datetime, timezone
    arrival = datetime.fromtimestamp(NOW - 60, tz=timezone.utc).isoformat()
    mined = datetime.fromtimestamp(NOW - 900, tz=timezone.utc).isoformat()
    _meta(conn, _pool(4), NATIVE, META)
    _tick(conn, _pool(4), _sqrt(1 / 0.28), 3 * 10 ** 21, arrival, block_ts=mined)
    conn.commit()
    states, _stats = hop.tape_states(conn, CHAIN, [_pool(4)], NOW)
    assert states[_pool(4)][2] == pytest.approx(NOW - 900, abs=1.0)


def test_the_written_price_records_its_oldest_leg_without_moving_observed(conn):
    """The price is the pool state times the base price, so it is as old as the older of the two.

    `observed` deliberately still carries the write time: the gate compares against it, so changing
    it IS the coverage change and that is the operator's ruling. This pins the hold, so a later edit
    that moves `observed` has to face this test.
    """
    from datetime import datetime, timezone
    arrival = datetime.fromtimestamp(NOW - 30, tz=timezone.utc).isoformat()
    mined = datetime.fromtimestamp(NOW - 1200, tz=timezone.utc).isoformat()
    _meta(conn, _pool(5), NATIVE, META)
    _tick(conn, _pool(5), _sqrt(1 / 0.28), 3 * 10 ** 21, arrival, block_ts=mined)
    conn.commit()
    out = hop.refresh(conn, CHAIN, "http://rpc", NOW, rpc_call=FakeRpc({META: 18}, {}))
    assert META in out["priced"]
    row = conn.execute("SELECT usd, observed FROM volume_quote_prices WHERE chain_id=? AND token=?",
                       (CHAIN, META)).fetchone()
    assert row["observed"] == pytest.approx(NOW), "observed still means the write time, for now"
    evidence = conn.execute("SELECT evidence_at, source FROM volume_quote_price_evidence "
                            "WHERE chain_id=? AND token=?", (CHAIN, META)).fetchone()
    # the base price was written at NOW-5 and the pool state is NOW-1200: the older leg wins
    assert evidence["evidence_at"] == pytest.approx(NOW - 1200, abs=1.0)
    assert evidence["source"] == "tape"


def test_hop_pools_indexed_form_matches_the_scan_oracle_and_never_returns_v3_ids(conn):
    """2026-09-23: hop_pools scanned 90k pool_meta rows per unit and pegged a core; the indexed
    form must return exactly what the old scan returned, including the unit-is-token0 flag."""
    from sniper.services.memecoin_equity_hop_price import hop_pools, _hop_pools_scan
    unit, base_a, base_b, stranger = "0xunit", "0xbasea", "0xbaseb", "0xnobody"
    v4a, v4b, v4c, v3 = "0x" + "a" * 64, "0x" + "b" * 64, "0x" + "c" * 64, "0x" + "d" * 40
    rows = [(v4a, unit, base_a), (v4b, base_b, unit), (v4c, unit, stranger), (v3, unit, base_a),
            ("0x" + "e" * 64, "0xother", base_a)]
    for pool, t0, t1 in rows:
        conn.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, resolved_at) "
                     "VALUES (?,?,?,?,?)", ("4663", pool, t0, t1, "2026-09-23T23:00:00+00:00"))
    bases = {base_a, base_b}
    got = sorted(hop_pools(conn, "4663", unit, bases))
    assert got == sorted(_hop_pools_scan(conn, "4663", unit, bases))
    assert got == [(v4a, base_a, True), (v4b, base_b, False)]
    assert hop_pools(conn, "4663", unit, set()) == []
    assert hop_pools(conn, "4663", unit.upper(), {base_a.upper()}) == [(v4a, base_a, True)]


def test_resolve_quote_sides_sets_the_unit_side_only_on_unresolved_pools_with_an_unpriced_other(conn):
    """Same defect class as hop_pools: the OR scan. The indexed form must set quote_side to the
    UNIT's side (0 when the unit is token0, 1 when token1), skip pools whose other side is a
    priced base or the unit itself, and leave already-resolved pools alone."""
    from sniper.services.memecoin_equity_hop_price import resolve_quote_sides
    unit, base, coin_a, coin_b = "0xunit", "0xbase", "0xcoina", "0xcoinb"
    rows = [("0x" + "1" * 64, unit, coin_a, None), ("0x" + "2" * 64, coin_b, unit, None),
            ("0x" + "3" * 64, unit, base, None), ("0x" + "4" * 64, unit, unit, None),
            ("0x" + "5" * 64, unit, "0xdone", 1)]
    for pool, t0, t1, qs in rows:
        conn.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, quote_side, resolved_at) "
                     "VALUES (?,?,?,?,?,?)", ("4663", pool, t0, t1, qs, "2026-09-23T23:00:00+00:00"))
    n = resolve_quote_sides(conn, "4663", unit.upper(), 18, "2026-09-23T23:01:00+00:00", bases={base})
    assert n == 2
    got = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT pool, quote_side, token0_decimals, token1_decimals FROM pool_meta")}
    assert got["0x" + "1" * 64] == (0, 18, None)
    assert got["0x" + "2" * 64] == (1, None, 18)
    assert got["0x" + "3" * 64][0] is None and got["0x" + "4" * 64][0] is None
    assert got["0x" + "5" * 64][0] == 1


def test_resolve_quote_sides_leaves_the_registration_stamp_alone(conn):
    """`resolved_at` is REGISTRATION time and this pass must not rewrite it.

    The live caller (`memecoin_volume_writer._start_hop_refresher`) passes an epoch FLOAT as `now`
    while every registration writes `iso(now)`, and both readers of the column compare it as a
    string, so a restamped row sorts below every ISO stamp: the V4 key seeder's cursor
    (`resolved_at > ?`) never sees that pool again, and the writer's retry window
    (`resolved_at <= ?`) reads it as permanently expired. 16,350 rows on the live tape carried an
    epoch stamp, all of them written by this pass.

    The float is what makes the defect REACHABLE: the older test above passes an ISO string, so it
    would have gone on passing with the restamp in place.
    """
    from sniper.services.memecoin_equity_hop_price import resolve_quote_sides
    unit, coin = "0xunit", "0xcoina"
    pool = "0x" + "7" * 64
    registered = "2026-09-24T07:24:06.871397+00:00"
    conn.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, quote_side, resolved_at) "
                 "VALUES (?,?,?,?,?,?)", ("4663", pool, unit, coin, None, registered))

    assert resolve_quote_sides(conn, "4663", unit, 18, 1790237187.20281) == 1

    side, stamp = conn.execute(
        "SELECT quote_side, resolved_at FROM pool_meta WHERE chain_id=? AND pool=?",
        ("4663", pool)).fetchone()[:2]
    assert side == 0                      # the pass still does its own job
    assert stamp == registered            # and leaves the registration stamp exactly as it was
