"""DexScreener out: E9 (EVM chain resolution) and E10 (V4 discovery depth), Alpha GO on 711d17720.

Spec docs/superpowers/specs/2026-09-29-memecoin-dexscreener-out-e9-e10-design.md.
E9: a dead path (0 of 79,250 EVM events since 09-01 lack a network); off = None, no request.
E10 phase 1 records every V4 ranking with each source's depth (GeckoTerminal, DexScreener, our own
tape) and the pool the quote won; phase 2 (E10:<chain>) skips the DexScreener round trip and ranks by
our own tape first. Default: both switches empty, today's behaviour.
"""
import sqlite3

import pytest

from sniper.execution import evm_v4_quote as V4
from sniper.execution import solana_quote as SQ
from sniper.settings import memecoin as cfg


@pytest.fixture(autouse=True)
def _switch_empty(monkeypatch):
    monkeypatch.delenv("MEMECOIN_DEXSCREENER_OFF", raising=False)


# --- E9 ------------------------------------------------------------------------------------------------

def test_e9_off_resolves_nothing_and_asks_nobody(monkeypatch):
    calls = []
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E9")
    assert SQ.resolve_evm_chain("0x" + "ab" * 20, http=lambda *a, **k: calls.append(a) or (200, {}, None)) is None
    assert calls == []
    assert SQ.dexscreener_consumer_counts()["suppressed"].get("E9", 0) >= 1


def test_e9_on_is_todays_lookup():
    calls = []
    SQ.resolve_evm_chain("0x" + "ab" * 20, http=lambda *a, **k: calls.append(a) or (200, {"pairs": []}, None))
    assert len(calls) == 1


# --- own-tape V4 depth ------------------------------------------------------------------------------------

Q96 = 2 ** 96
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"


def _tape(tmp_path, pools):
    """pools: [(pool, quote_side, L, sqrtP, observed_at)]. Quote = WETH ($2,000), 18 decimals."""
    path = tmp_path / "tape.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE pool_ticks (id INTEGER PRIMARY KEY, chain_id TEXT, pool TEXT, kind TEXT,
            liquidity TEXT, sqrt_price_x96 TEXT, observed_at TEXT);
        CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT, quote_side INTEGER,
            token0_decimals INTEGER, token1_decimals INTEGER);
        CREATE TABLE volume_quote_prices (chain_id TEXT, token TEXT, usd REAL, observed REAL);
    """)
    c.execute("INSERT INTO volume_quote_prices VALUES ('4663', ?, 2000.0, 1000.0)", (WETH,))
    for pool, qs, L, S, at in pools:
        t0, t1 = (WETH, "0x" + "cc" * 20) if qs == 0 else ("0x" + "cc" * 20, WETH)
        c.execute("INSERT INTO pool_meta VALUES ('4663', ?, ?, ?, ?, 18, 18)", (pool, t0, t1, qs))
        c.execute("INSERT INTO pool_ticks (chain_id, pool, kind, liquidity, sqrt_price_x96, observed_at) "
                  "VALUES ('4663', ?, 'v3_swap', ?, ?, ?)", (pool, str(L), str(S), at))
    c.commit()
    return path


def test_own_tape_depth_is_the_quote_side_virtual_reserve_in_usd(tmp_path):
    from sniper.services.memecoin_onchain_volume import own_v4_depth_usd
    a, b, gone = "0x" + "a1" * 32, "0x" + "b2" * 32, "0x" + "c3" * 32
    # quote = token0: reserve = L * 2^96 / S; with S = 2^96 that is L (in wei) -> L / 1e18 ETH
    path = _tape(tmp_path, [(a, 0, 10 ** 18, Q96, "2026-09-29T04:00:00+00:00"),
                            (b, 1, 3 * 10 ** 18, Q96, "2026-09-29T04:00:00+00:00")])
    out = own_v4_depth_usd("4663", [a, b, gone], path=path, now_ts=1000.0)
    assert out[a] == pytest.approx(2000.0) and out[b] == pytest.approx(6000.0)
    assert out[gone] is None, "no swap on the tape: unknown, never 0"


def test_a_stale_quote_price_is_unknown_and_the_default_clock_works(tmp_path):
    """A six-day-old quote price (the table keeps hop-priced tokens that long) prices nothing; the
    default `now_ts` path must run (it raised NameError once, which a clock-pinned test cannot see)."""
    from sniper.services.memecoin_onchain_volume import own_v4_depth_usd
    a = "0x" + "a" * 64
    path = _tape(tmp_path, [(a, 0, 10 ** 18, Q96, "2026-09-29T04:00:00+00:00")])
    assert own_v4_depth_usd("4663", [a], path=path, now_ts=1000.0 + 6 * 86400)[a] is None
    import time
    with sqlite3.connect(path) as c:
        c.execute("UPDATE volume_quote_prices SET observed=?", (time.time(),))
    assert own_v4_depth_usd("4663", [a], path=path)[a] == pytest.approx(2000.0)


def test_an_unpriced_quote_is_unknown(tmp_path):
    from sniper.services.memecoin_onchain_volume import own_v4_depth_usd
    a = "0x" + "a1" * 32
    path = _tape(tmp_path, [(a, 0, 10 ** 18, Q96, "2026-09-29T04:00:00+00:00")])
    sqlite3.connect(path).execute("DELETE FROM volume_quote_prices").connection.commit()
    assert own_v4_depth_usd("4663", [a], path=path, now_ts=1000.0)[a] is None


# --- E10: V4 discovery without DexScreener (phase 1 record, phase 2 cut) ---------------------------

from sniper.execution import evm_v4_quote as V  # noqa: E402
from sniper.execution import evm_v4_multihop as MH  # noqa: E402
import sniper.services.memecoin_onchain_volume as OV  # noqa: E402

MINT = "0x" + "ab" * 20
CASH = "0x" + "cd" * 20
P = ["0x" + ("%02x" % i) * 32 for i in range(1, 11)]       # ten 66-char V4 pool ids


@pytest.fixture
def v4(monkeypatch):
    V._pool_cache.clear()
    V._key_cache.clear()
    V._reserve_cache.clear()
    monkeypatch.setattr(V, "_stored_pool_key", lambda *_a: None)
    monkeypatch.setattr(V, "_store_pool_key", lambda *_a: None)
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *_a, **_k: [])
    monkeypatch.setattr(OV, "own_v4_depth_usd", lambda chain, pools, **_k: {p: None for p in pools})
    V.begin_quote()
    yield monkeypatch
    V.begin_quote()


def _gt(pairs):
    return lambda _url: {"data": [{"id": "robinhood_" + pid,
                                   "attributes": {"reserve_in_usd": str(r)}} for pid, r in pairs]}


class _DS:
    def __init__(self, pairs=()):
        self.calls, self.pairs = [], list(pairs)

    def __call__(self, url):
        self.calls.append(url)
        return {"pairs": [{"chainId": "robinhood", "pairAddress": pid,
                           "baseToken": {"address": MINT}, "quoteToken": {"address": CASH},
                           "liquidity": {"usd": r}}
                          for pid, r in self.pairs]}


def _own(monkeypatch, depths):
    monkeypatch.setattr(OV, "own_v4_depth_usd",
                        lambda chain, pools, **_k: {p: depths.get(p) for p in pools})


def test_e10_off_asks_dexscreener_nothing_in_discovery(v4, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E10:4663")
    ds = _DS([(P[0], 5000.0)])
    before = SQ.dexscreener_consumer_counts()["suppressed"].get("E10:4663", 0)
    pools = V._pools_for_mint("4663", MINT, gt_http=_gt([(P[1], 100.0)]), ds_http=ds)
    assert ds.calls == [], "E10:4663 off still asked DexScreener"
    assert pools == [P[1]]
    assert SQ.dexscreener_consumer_counts()["suppressed"].get("E10:4663", 0) == before + 1


def test_e10_off_on_one_chain_leaves_the_other_asking(v4, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E10:8453")
    ds = _DS([(P[0], 5000.0)])
    V._pools_for_mint("4663", MINT, gt_http=_gt([]), ds_http=ds)
    assert len(ds.calls) == 1


def test_e10_off_leaves_the_multihop_asking_dexscreener_until_phase_1_records_it(v4, monkeypatch):
    """Alpha 2026-09-29: v4_multihop won 23 quotes on 4663 and phase 1 records none of its legs, so
    E10:<chain> must not blind its DexScreener reads yet (D-E10-3 deferred)."""
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E10:4663")
    ds = _DS([(P[0], 5000.0)])
    out = MH._pools_with_depth("4663", MINT, gt_http=_gt([(P[1], 100.0)]), ds_http=ds)
    assert len(ds.calls) == 1 and [pid for pid, _d in out] == [P[0], P[1]]
    assert MH._direct_bridge_pools("4663", CASH, MINT, ds_http=ds) == [(P[0], 5000.0)]
    assert len(ds.calls) == 2


def test_e10_off_reads_as_blind_not_as_no_pools(v4, monkeypatch):
    """A switched-off DexScreener is a source that did not answer ([], failed=True), exactly like its
    open breaker. With GeckoTerminal failing too and no chain scan, discovery is BLIND
    (`rate_limit`), which the requote re-enters; ([], False) would call it `discovery_unproven`."""
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E10:4663")
    ds = _DS([(P[0], 5000.0)])
    assert V._pools_for_mint("4663", MINT, gt_http=lambda _url: None, ds_http=ds) == []
    assert ds.calls == [] and V.last_provider_blind() == "rate_limit"


def test_e10_off_ranks_a_pool_with_tape_depth_ahead_of_one_without(v4, monkeypatch):
    """GeckoTerminal says P0 is deep and P1 shallow; only P1 swapped on our tape this hour."""
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E10:4663")
    _own(monkeypatch, {P[1]: 900.0})
    pools = V._pools_for_mint("4663", MINT, gt_http=_gt([(P[0], 50000.0), (P[1], 10.0)]),
                              ds_http=_DS())
    assert pools[:2] == [P[1], P[0]]


def test_e10_off_ranks_measured_pools_by_tape_depth(v4, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E10:4663")
    _own(monkeypatch, {P[0]: 10.0, P[1]: 30.0, P[2]: 20.0})
    pools = V._pools_for_mint("4663", MINT, gt_http=_gt([(P[0], 3.0), (P[1], 2.0), (P[2], 1.0)]),
                              ds_http=_DS())
    assert pools == [P[1], P[2], P[0]]


def test_e10_off_takes_four_when_the_tape_knows_every_candidate_and_eight_when_stale(v4, monkeypatch):
    """Ten pools from our own store (no provider depth). A live tape measures all of them: cap 4.
    A stale tape measures none: every depth is unknown and the cap widens to 8, as today."""
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E10:4663")
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *_a, **_k: list(P))
    _own(monkeypatch, {pid: float(100 + i) for i, pid in enumerate(P)})
    live = V._pools_for_mint("4663", MINT, gt_http=_gt([]), ds_http=_DS())
    assert live == [P[9], P[8], P[7], P[6]]
    V._pool_cache.clear()
    _own(monkeypatch, {})
    assert len(V._pools_for_mint("4663", MINT, gt_http=_gt([]), ds_http=_DS())) == 8


def test_e10_on_is_todays_ranking_and_asks_dexscreener(v4, monkeypatch):
    """The tape's opinion must not leak into today's ranking while E10 is on. Only the shallowest
    pool has tape depth, so a tier leak (tape first) and a value leak would both move P2 up."""
    _own(monkeypatch, {P[2]: 3.0})
    ds = _DS([(P[1], 700.0)])
    pools = V._pools_for_mint("4663", MINT, gt_http=_gt([(P[0], 900.0), (P[2], 100.0)]), ds_http=ds)
    assert len(ds.calls) == 1
    assert pools == [P[0], P[1], P[2]]
    assert V.top_pool_reserve_usd("4663", MINT) == 900.0


def test_the_ranking_record_carries_every_source_and_the_cut(v4, monkeypatch):
    _own(monkeypatch, {P[0]: 11.0})
    pools = V._pools_for_mint("4663", MINT, gt_http=_gt([(P[0], 900.0)]),
                              ds_http=_DS([(P[0], 800.0), (P[1], 700.0)]))
    rec = V.take_v4_ranking(MINT)
    assert rec["cut"] == pools == [P[0], P[1]] and rec["cap"] == 4 and rec["e10_off"] is False
    assert rec["pools"][P[0]] == {"gt": 900.0, "ds": 800.0, "own": 11.0, "src": "gt",
                                  "fee_rank": 1}
    assert rec["pools"][P[1]] == {"gt": None, "ds": 700.0, "own": None, "src": "ds",
                                  "fee_rank": 1}
    assert V.take_v4_ranking(MINT) is None, "a record is consumed once"


def test_a_cached_discovery_carries_the_ranking_that_built_its_list(v4, monkeypatch):
    """A prewarm, an earlier signal or the exit sweep's warm-up often discovers first, on another
    thread; the quote that then wins reads the CACHE. It must still carry the ranking behind the
    list it used, marked cached, or phase 1 records almost nothing and exits nothing at all."""
    V._pools_for_mint("4663", MINT, gt_http=_gt([(P[0], 900.0)]), ds_http=_DS())
    fresh = V.take_v4_ranking(MINT)
    assert fresh["cached"] is False and fresh["cut"] == [P[0]]
    V.begin_quote()                                  # another quote, maybe on another thread
    V._pools_for_mint("4663", MINT, gt_http=_gt([]), ds_http=_DS())     # cache hit
    assert V.take_v4_ranking("0x" + "ef" * 20) is None, "a foreign mint takes nothing"
    cached = V.take_v4_ranking(MINT)
    assert cached["cached"] is True and cached["cut"] == [P[0]] and cached["pools"] == fresh["pools"]
    assert V.take_v4_ranking(MINT) is None, "consumed once"


def test_the_entry_trace_carries_the_ranking_and_the_won_pool():
    """Phase 1's whole output: removing either field from `evm.quote_initial` must fail here."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from test_memecoin_evm_entry_executor import _QUOTE, _TraceRecorder, _c, _reserve, _seams
    from sniper.execution.evm_swap_executor import execute_entry_evm
    ranking = {"cut": [P[0]], "cap": 4, "e10_off": False, "pools": {}}
    quote = dict(_QUOTE, route={"protocol": "uniswap_v4", "pool_id": P[0]},
                 _sniper_v4_ranking=ranking)
    c = _c(); pid = _reserve(c)
    seams, _calls = _seams(quote=quote)
    trace = _TraceRecorder()
    execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                      strategy="alpha_b", source_user_id="trader1",
                      now_iso="2026-07-09T00:00:01Z", trace=trace, **seams)
    meta = [md for name, md in trace.substages if name == "evm.quote_initial"][0]
    assert meta["v4_ranking"] == ranking and meta["won_pool"] == P[0]


def test_e10_off_a_tape_zero_ranks_behind_geckoterminal_depth(v4, monkeypatch):
    """L = 0 is a real zero: the pool has no in-range liquidity. Four such pools must not push the
    one pool GeckoTerminal measured out of a 4-deep cut."""
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E10:4663")
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *_a, **_k: P[:4])
    _own(monkeypatch, {pid: 0.0 for pid in P[:4]})
    pools = V._pools_for_mint("4663", MINT, gt_http=_gt([(P[4], 20000.0)]), ds_http=_DS())
    assert pools[0] == P[4] and len(pools) == 4


def test_a_full_exit_records_the_ranking_and_the_won_pool():
    """Exits share `_pools_for_mint`, so phase 2 changes their candidates too (Alpha 2026-09-29):
    the full close's exit_timing_json carries the same ranking and the pool the sell was won on."""
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from test_memecoin_evm_exit_executor import _QUOTE as EXIT_QUOTE, _c as exit_c, _pos, _seams as exit_seams
    from sniper.execution.evm_exit_executor import execute_exit_evm
    from sniper.persistence import db_memecoin as m
    ranking = {"cut": [P[0]], "cap": 4, "e10_off": False, "cached": True, "pools": {}}
    for quote, want in ((dict(EXIT_QUOTE, route={"protocol": "uniswap_v4", "pool_id": P[0]},
                              _sniper_v4_ranking=ranking), True), (EXIT_QUOTE, False)):
        c = exit_c(); pos = _pos(c)
        seams, _calls = exit_seams(quote=quote)
        r = execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3], **seams)
        assert r["status"] == "closed"
        timing = json.loads(m.get_position(c, pos["id"])["exit_timing_json"])
        if want:
            assert timing["v4_ranking"] == ranking and timing["won_pool"] == P[0]
        else:
            assert "v4_ranking" not in timing and "won_pool" not in timing


def test_the_record_is_capped_and_its_e10_cut_is_the_cut_phase_2_takes(v4, monkeypatch):
    """Phase 1 goes live at merge, so the record in every entry trace and exit_timing_json is capped:
    the cut, the phase 2 cut and the top 8 per source, never every merged pool. Its `e10_cut` is
    computed by the same code that cuts with E10 off, so the read never replays the rule."""
    many = ["0x%064x" % (1000 + i) for i in range(40)]
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *_a, **_k: list(many))
    _own(monkeypatch, {many[i]: float(100 - i) for i in range(12)})          # 12 on the tape
    gt = _gt([(many[39 - i], float(1000 - i)) for i in range(10)])           # 10 elsewhere on GT
    pools = V._pools_for_mint("4663", MINT, gt_http=gt, ds_http=_DS())
    rec = V.take_v4_ranking(MINT)
    assert rec["n_candidates"] == 40 and rec["cut"] == pools
    top = lambda src: sorted((p for p in many if rec["pools"].get(p, {}).get(src) is not None),
                             key=lambda p: -rec["pools"][p][src])
    assert set(rec["pools"]) == set(pools) | set(rec["e10_cut"]) | set(top("own")[:8]) | set(top("gt")[:8])
    assert len(rec["pools"]) < 40 and many[0] in rec["pools"], "the tape's top pool is kept"
    V._pool_cache.clear()
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E10:4663")
    assert V._pools_for_mint("4663", MINT, gt_http=gt, ds_http=_DS()) == rec["e10_cut"]


@pytest.mark.parametrize("e10", ["", "E10:4663"])
def test_a_discovery_reads_each_unknown_pool_key_no_more_often_than_today(v4, monkeypatch, e10):
    """Gamma 2026-09-29: a key not in memory costs `_stored_pool_key`, a NEW read-only connection to
    the ledger (p50 2.06 ms) that is never memoized on not-found. The record and the phase 2 cut
    each ranked every pool's fee again: 16 lookups at cdf85e5f4 became 28 for six unknown pools,
    ~+25 ms on the entry and exit quote paths. Today's 16 = the derive check (6) + one fee rank per
    pool (6) + the TTL's sane-pool check over the 4-pool cut (4)."""
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", e10)
    calls = []
    monkeypatch.setattr(V, "_stored_pool_key", lambda chain, pid: calls.append(pid))
    six = P[:6]
    gt = _gt([(pid, float(100 + i)) for i, pid in enumerate(six)])
    pools = V._pools_for_mint("4663", MINT, gt_http=gt, ds_http=_DS())
    assert len(pools) == 4 and len(calls) <= 16, len(calls)
