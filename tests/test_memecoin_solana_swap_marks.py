import asyncio
import json
import sqlite3

import pytest

from sniper.services.memecoin_solana_amounts import USDC, WSOL
from sniper.services.memecoin_solana_ws import VaultTape
from sniper.services.memecoin_solana_swap_marks import (
    BIND_MAX_ATTEMPTS,
    PoolBinding,
    bind_open_positions,
    SolanaSwapMarkCache,
    build_price_book,
    run_price_stream_epoch,
    ws_url_from_read_rpc,
)


def _binding(quote_mint=WSOL):
    return PoolBinding(
        mint="MintA",
        pool="PoolA",
        coin_vault="CoinV",
        quote_vault="QuoteV",
        quote_mint=quote_mint,
    )


def _swap(**overrides):
    row = {
        "chain_id": "solana",
        "pool": "PoolA",
        "kind": "sol_swap",
        "block_no": 42,
        "amount0": "-2000000",
        "amount1": "300000000",
        "decimals0": 6,
        "decimals1": 9,
    }
    row.update(overrides)
    return row


def test_wsol_swap_converts_to_a_usd_mark():
    cache = SolanaSwapMarkCache()
    assert cache.record(
        _swap(),
        _binding(),
        now_ts=100.0,
        observed_at="2026-08-12T12:00:00+00:00",
    )
    marks = cache.fresh_price_map({WSOL: 200.0}, now_ts=101.0, max_age_s=5.0)
    mark = marks[("solana", "MintA")]
    assert mark["price"] == pytest.approx(30.0)
    assert mark["source"] == "fastlane_sol_swap"
    assert mark["observed_at"] == "2026-08-12T12:00:00+00:00"


def test_usdc_swap_uses_the_usdc_reference():
    cache = SolanaSwapMarkCache()
    row = _swap(amount1="3000000", decimals1=6)
    cache.record(row, _binding(USDC), now_ts=100.0, observed_at="t")
    marks = cache.fresh_price_map({USDC: 1.0}, now_ts=101.0, max_age_s=5.0)
    assert marks[("solana", "MintA")]["price"] == pytest.approx(1.5)


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "sol_sync"},
        {"amount0": "0"},
        {"amount1": "-1"},
        {"decimals0": None},
        {"decimals1": 19},
    ],
)
def test_invalid_rows_do_not_enter_the_cache(change):
    cache = SolanaSwapMarkCache()
    assert cache.record(
        _swap(**change), _binding(), now_ts=100.0, observed_at="t"
    ) is False
    assert cache.quote_mints() == set()


def test_stale_and_invalidated_marks_are_absent():
    cache = SolanaSwapMarkCache()
    cache.record(_swap(), _binding(), now_ts=100.0, observed_at="t")
    assert cache.fresh_price_map({WSOL: 200.0}, now_ts=106.0, max_age_s=5.0) == {}
    cache.record(_swap(), _binding(), now_ts=110.0, observed_at="t2")
    cache.invalidate()
    assert cache.fresh_price_map({WSOL: 200.0}, now_ts=110.0, max_age_s=5.0) == {}


def test_price_book_uses_only_durable_supported_bindings():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE sol_position_pools (position_id INTEGER PRIMARY KEY, "
        "mint TEXT, pool TEXT, venue_kind TEXT, resolution_tx TEXT, resolved_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE sol_pool_vaults (pool TEXT PRIMARY KEY, mint TEXT, "
        "coin_vault TEXT, quote_vault TEXT, quote_mint TEXT, resolved_at TEXT)"
    )
    conn.execute("INSERT INTO sol_position_pools VALUES (7,'MintA','PoolA','amm','sig','t')")
    conn.execute(
        "INSERT INTO sol_pool_vaults VALUES ('PoolA','MintA','CoinV','QuoteV',?,'t')",
        (WSOL,),
    )
    conn.execute("INSERT INTO sol_position_pools VALUES (8,'CurveMint','Curve','pump_curve','sig','t')")
    conn.execute(
        "INSERT INTO sol_pool_vaults VALUES ('Curve','CurveMint','Curve','NativeSOL',?,'t')",
        (WSOL,),
    )
    vaults, bindings = build_price_book(
        [{"id": 7, "mint": "MintA", "network_id": "solana"},
         {"id": 8, "mint": "CurveMint", "network_id": "solana"}], conn
    )
    # Since 2026-09-27 a pump curve is subscribed too: at its curve PDA (the binding's
    # quote_vault), base64, priced from its own reserves (tests/test_memecoin_solana_curve_marks.py).
    from sniper.services.memecoin_solana_ws import CurveTape
    assert set(vaults) == {"CoinV", "QuoteV", "NativeSOL"}
    assert isinstance(vaults["NativeSOL"], CurveTape)
    assert bindings["PoolA"] == _binding()
    assert bindings["NativeSOL"].mint == "CurveMint" and bindings["NativeSOL"].quote_mint == WSOL


def test_read_rpc_url_converts_without_using_a_custody_setting():
    assert ws_url_from_read_rpc("https://rpc.invalid/path?q=1") == (
        "wss://rpc.invalid/path?q=1"
    )
    assert ws_url_from_read_rpc("") is None


def test_stream_epoch_records_then_invalidates_on_disconnect():
    cache = SolanaSwapMarkCache()
    binding = _binding()
    seen = []

    async def stream(_vaults, on_row, **_kwargs):
        await on_row(_swap())
        seen.append(cache.quote_mints())

    asyncio.run(
        run_price_stream_epoch(
            {"CoinV": object()},
            {"PoolA": binding},
            cache,
            ws_url="wss://rpc.invalid",
            duration_s=1.0,
            stream_fn=stream,
            now_ts_fn=lambda: 100.0,
            now_iso_fn=lambda: "2026-08-12T12:00:00+00:00",
        )
    )
    assert seen == [{WSOL}]
    assert cache.quote_mints() == set()


def _open_position(pid=1, network_id="solana"):
    return {"id": pid, "mint": "MintA", "network_id": network_id}


def test_an_unbound_position_binds_from_its_own_fill_without_the_tape_pass():
    calls = []

    def resolve(tc, mconn, pos, get_tx_fn):
        calls.append(pos["id"])
        return "CoinV"

    attempts = {}
    assert bind_open_positions(
        None, None, [_open_position()], object(),
        attempts=attempts, resolve_fn=resolve,
    ) == 1
    # A binding is durable, so the next pass must not spend a second getTransaction.
    bind_open_positions(
        None, None, [_open_position()], object(),
        attempts=attempts, resolve_fn=resolve,
    )
    assert calls == [1]


def test_a_fill_that_never_decodes_stops_spending_rpc_at_the_cap():
    calls = []

    def resolve(tc, mconn, pos, get_tx_fn):
        calls.append(pos["id"])
        return None

    attempts = {}
    for _ in range(BIND_MAX_ATTEMPTS + 3):
        bind_open_positions(
            None, None, [_open_position()], object(),
            attempts=attempts, resolve_fn=resolve,
        )
    assert len(calls) == BIND_MAX_ATTEMPTS


def test_binding_never_raises_and_skips_other_chains():
    def resolve(tc, mconn, pos, get_tx_fn):
        raise RuntimeError("rpc down")

    attempts = {}
    assert bind_open_positions(
        None, None, [_open_position(), _open_position(2, "4663")], object(),
        attempts=attempts, resolve_fn=resolve,
    ) == 0
    assert 2 not in attempts


def test_closed_positions_leave_the_attempt_map():
    def resolve(tc, mconn, pos, get_tx_fn):
        return None

    attempts = {}
    bind_open_positions(None, None, [_open_position()], object(),
                        attempts=attempts, resolve_fn=resolve)
    bind_open_positions(None, None, [], object(),
                        attempts=attempts, resolve_fn=resolve)
    assert attempts == {}


def test_a_position_opening_mid_epoch_joins_the_live_socket(monkeypatch):
    """Regression (2026-08-12): a position that opened during a 30 s epoch waited it out.

    Measured bind lag was 18.2 / 21.4 / 27.9 s — the most volatile part of a position's life priced
    by the 30 s DexScreener mid. `accountSubscribe` is per-account and the socket takes one at any
    time, so the new vault joins the LIVE connection. Shortening the epoch instead is not equivalent:
    `run_price_stream_epoch` invalidates the mark cache when an epoch ends.
    """
    import asyncio

    from sniper.services import memecoin_solana_ws as ws_mod

    sent = []
    frames = asyncio.Queue()

    class _FakeWS:
        async def send(self, raw):
            sent.append(json.loads(raw))

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await frames.get()

    class _Conn:
        async def __aenter__(self):
            return _FakeWS()

        async def __aexit__(self, *a):
            return False

    vaults = {"CoinV": VaultTape("CoinV", "QuoteV", mint="PoolA")}
    later = {"CoinB": VaultTape("CoinB", "QuoteB", mint="PoolB")}
    calls = {"n": 0}

    def refresh():
        calls["n"] += 1
        return dict(vaults, **later) if calls["n"] >= 1 else dict(vaults)

    async def drive():
        task = asyncio.ensure_future(ws_mod.run_vault_stream(
            vaults, lambda row: None, ws_url="wss://x",
            connect=lambda url: _Conn(), refresh_fn=refresh, refresh_s=0.01))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    subscribed = [f["params"][0] for f in sent if f.get("method") == "accountSubscribe"]
    assert "CoinV" in subscribed, "the initial vault was never subscribed"
    assert "CoinB" in subscribed, "a vault added mid-epoch never joined the live socket"
    assert "CoinB" in vaults, "the reader's vault map did not gain the new tape"


def test_a_failing_refresh_never_tears_down_a_healthy_stream():
    import asyncio

    from sniper.services import memecoin_solana_ws as ws_mod

    frames = asyncio.Queue()

    class _FakeWS:
        async def send(self, raw):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await frames.get()

    class _Conn:
        async def __aenter__(self):
            return _FakeWS()

        async def __aexit__(self, *a):
            return False

    vaults = {"CoinV": VaultTape("CoinV", "QuoteV", mint="PoolA")}

    def refresh():
        raise RuntimeError("db unavailable")

    async def drive():
        task = asyncio.ensure_future(ws_mod.run_vault_stream(
            vaults, lambda row: None, ws_url="wss://x",
            connect=lambda url: _Conn(), refresh_fn=refresh, refresh_s=0.01))
        await asyncio.sleep(0.12)
        alive = not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return alive

    assert asyncio.run(drive()), "a raising refresh killed the stream"


# --- pool-STATE marks (2026-08-14): price the venue's reserves, not what an aggregator shows ----

def _sync(**overrides):
    """A `sol_sync` row: absolute reserves of both vaults, as run_vault_stream emits every slot
    where one side moved. amount0/amount1 are BALANCES here, not deltas."""
    row = {
        "chain_id": "solana", "pool": "PoolA", "kind": "sol_sync", "block_no": 431,
        "tx_sig": None, "log_index": 0, "maker": None,
        "amount0": "2000000000000",          # 2,000,000 tokens at 6 decimals
        "amount1": "300000000000",           # 300 WSOL at 9 decimals
        "decimals0": 6, "decimals1": 9,
        "observed_at": None,
    }
    row.update(overrides)
    return row


def _agreeing_swap(**overrides):
    """A swap at 1.5e-4 WSOL/token — the same price `_sync()` reads off the reserves.

    `_swap()` itself prices 0.15, a thousand times the sync, so it can never corroborate one.
    """
    return _swap(amount0="-2000000", amount1="300000", **overrides)


def test_a_sync_row_prices_the_pool_state():
    """The operator's instruction verbatim: get the price from where fomo's fills execute — the
    pool's own reserves — not from what an aggregator displays. spot = quote_reserve/coin_reserve
    in human units; here 300 WSOL / 2,000,000 tokens = 1.5e-4 WSOL, at $200/SOL -> $0.03.

    CORROBORATION REQUIRED SINCE 2026-09-22, so the agreeing swap below is part of the contract:
    the reserve ratio is only a price on a constant-product pool, and a swap at the same price is
    what says this pool is one.
    """
    cache = SolanaSwapMarkCache()
    cache.record(_agreeing_swap(), _binding(), now_ts=99.0, observed_at="t0")
    assert cache.record(_sync(), _binding(), now_ts=100.0,
                        observed_at="2026-08-14T19:00:00+00:00")
    mark = cache.fresh_price_map({WSOL: 200.0}, now_ts=101.0, max_age_s=5.0)[("solana", "MintA")]
    assert mark["price"] == pytest.approx(0.03)
    assert mark["source"] == "fastlane_sol_state"


def test_a_swap_and_a_sync_both_land_and_the_freshest_wins():
    """Both kinds now keep their OWN slot, so the sync is still there to be chosen."""
    cache = SolanaSwapMarkCache()
    cache.record(_agreeing_swap(), _binding(), now_ts=100.0, observed_at="t1")
    cache.record(_sync(), _binding(), now_ts=101.0, observed_at="t2")
    mark = cache.fresh_price_map({WSOL: 200.0}, now_ts=102.0, max_age_s=5.0)[("solana", "MintA")]
    assert mark["source"] == "fastlane_sol_state"
    assert mark["observed_at"] == "t2"


def test_the_swap_mark_survives_a_sync_in_the_same_slot():
    """THE COLLISION THIS FIX EXISTS FOR. VaultTape emits `sol_sync` then `sol_swap` for the same
    slot, and one entry per mint meant the second destroyed the first. A STATE row then reached
    the tape only when the newest event was one-sided: SWAP-only ran 96% of mints at one mark and
    4% above 300, which measured the sample, not the pool."""
    cache = SolanaSwapMarkCache()
    cache.record(_sync(), _binding(), now_ts=100.0, observed_at="sync")
    cache.record(_agreeing_swap(), _binding(), now_ts=100.0, observed_at="swap")
    # both are still held: the swap is not older, so it wins, but the sync was not destroyed
    assert cache._state_marks[("solana", "MintA")].observed_at == "sync"
    assert cache._marks[("solana", "MintA")].observed_at == "swap"


def test_raw_reserves_witness_silence_without_becoming_a_price_mark():
    cache = SolanaSwapMarkCache()
    start = "2026-09-26T12:00:30+00:00"
    now = "2026-09-26T12:01:01+00:00"
    witness = lambda: cache.reserve_witness(
        mint="MintA", since_iso="2026-09-26T12:00:00+00:00",
        window_start_iso=start, now_iso=now, health_window_s=60.0)
    assert witness() == "uncovered_transient"
    cache.record(_sync(), _binding(), now_ts=1.0,
                 observed_at="2026-09-26T12:00:00+00:00")
    # A raw sync outside the price corroboration band still establishes a reserve baseline.
    assert cache.fresh_price_map({WSOL: 200.0}, now_ts=2.0, max_age_s=5.0) == {}
    assert witness() == "reader_dark"
    cache.record(_sync(), _binding(), now_ts=2.0,
                 observed_at="2026-09-26T12:00:50+00:00")
    assert witness() == "still"
    cache.record(_sync(amount0="2100000000000"), _binding(), now_ts=3.0,
                 observed_at="2026-09-26T12:00:55+00:00")
    assert witness() == "moving"
    other = PoolBinding("MintA", "PoolB", "CoinB", "QuoteB", WSOL)
    cache.record(_sync(pool="PoolB"), other, now_ts=4.0,
                 observed_at="2026-09-26T12:00:20+00:00")
    assert witness() == "uncovered_transient"  # two possible venues for one mint
    cache.invalidate()
    assert witness() == "uncovered_transient"


def test_normal_stream_epoch_keeps_raw_witness_but_drops_price_marks():
    cache = SolanaSwapMarkCache()
    cache.record(_agreeing_swap(), _binding(), now_ts=1.0,
                 observed_at="2026-09-26T12:00:00+00:00")
    cache.record(_sync(), _binding(), now_ts=2.0,
                 observed_at="2026-09-26T12:00:00+00:00")

    async def quiet_stream(_vaults, _on_row, **_kwargs):
        await asyncio.sleep(1.0)

    asyncio.run(run_price_stream_epoch(
        {"CoinV": object()}, {"PoolA": _binding()}, cache,
        ws_url="wss://rpc.invalid", duration_s=0.01, stream_fn=quiet_stream))
    assert cache.quote_mints() == set()
    assert cache.reserve_witness(
        mint="MintA", since_iso="2026-09-26T12:00:00+00:00",
        window_start_iso="2026-09-26T12:00:30+00:00",
        now_iso="2026-09-26T12:00:45+00:00") == "still"


def test_provider_timeout_clears_raw_witness():
    cache = SolanaSwapMarkCache()
    cache.record(_sync(), _binding(), now_ts=1.0,
                 observed_at="2026-09-26T12:00:00+00:00")

    async def failed_stream(_vaults, _on_row, **_kwargs):
        raise TimeoutError("provider timed out")

    with pytest.raises(TimeoutError, match="provider timed out"):
        asyncio.run(run_price_stream_epoch(
            {"CoinV": object()}, {"PoolA": _binding()}, cache,
            ws_url="wss://rpc.invalid", duration_s=1.0, stream_fn=failed_stream))
    assert cache.reserve_witness(
        mint="MintA", since_iso="2026-09-26T12:00:00+00:00",
        window_start_iso="2026-09-26T12:00:30+00:00",
        now_iso="2026-09-26T12:00:45+00:00") == "uncovered_transient"


@pytest.mark.parametrize("change", [
    {"amount0": "0"},                        # empty coin side -> no spot
    {"amount1": "0"},                        # empty quote side -> no spot
    {"amount0": "-5"},                       # a delta smuggled in as a balance
    {"decimals0": None},
    {"decimals1": 40},
    {"pool": "OtherPool"},
])
def test_invalid_sync_rows_stay_out(change):
    cache = SolanaSwapMarkCache()
    assert not cache.record(_sync(**change), _binding(), now_ts=100.0, observed_at="t")
    assert cache.fresh_price_map({WSOL: 200.0}, now_ts=100.0, max_age_s=5.0) == {}


def test_swap_marks_keep_their_own_source_label():
    cache = SolanaSwapMarkCache()
    cache.record(_swap(), _binding(), now_ts=100.0, observed_at="t1")
    mark = cache.fresh_price_map({WSOL: 200.0}, now_ts=101.0, max_age_s=5.0)[("solana", "MintA")]
    assert mark["source"] == "fastlane_sol_swap"



# --- STATE marks must not price a concentrated pool (2026-09-22) --------------------------------
#
# A STATE mark is quote_vault/coin_vault. That IS the spot price on a constant-product pool and is
# not on concentrated liquidity, where the vaults hold every tick or bin. The funded stop, the
# catastrophe floor and the mcap SL read the nominal mid, so a mark 4.40x or 0.55x the truth is a
# false exit on a funded position.
#
# The pool's program cannot gate it: sol_pool_vaults.program is the SPL vault owner, NULL for 64%
# of pools, and pumpswap derives one per pool — it classifies 23.1% of STATE-emitting mints. So a
# fresh SWAP mark for the same mint is the reference, and the band is +/-10%, tied to the tightest
# funded loss rail rather than to a percentile: an admitted mark cannot move the read 10% against
# the swap that corroborated it, so it cannot trip a 35% stop by itself.

# Gamma's measured ratio table (STATE mid / swap mid of the same mint within 5 s), 2026-09-22.
RATIO_TABLE = {
    "meteora_dlmm":  {"median": 4.40,  "p10": 0.93,  "p90": 29.5, "concentrated": True},
    "raydium_clmm":  {"median": 0.805, "p10": 0.546, "p90": 23.2, "concentrated": True},
    "raydium_cpmm":  {"median": 1.012, "p10": 0.979, "p90": 1.338, "concentrated": False},
    "pumpswap_amm":  {"median": 0.923, "p10": 0.797, "p90": 0.983, "concentrated": False},
}


def _cache_at_ratio(ratio):
    """A cache whose STATE mark sits `ratio` times its corroborating SWAP mark."""
    cache = SolanaSwapMarkCache()
    cache.record(_agreeing_swap(), _binding(), now_ts=100.0, observed_at="swap")
    # _sync() prices 1.5e-4; scale the quote reserve to move the ratio.
    cache.record(_sync(amount1=str(int(300000000000 * ratio))), _binding(),
                 now_ts=101.0, observed_at="state")
    return cache


def _admits(ratio):
    cache = _cache_at_ratio(ratio)
    mark = cache.fresh_price_map({WSOL: 200.0}, now_ts=102.0, max_age_s=5.0)[("solana", "MintA")]
    return mark["source"] == "fastlane_sol_state"


@pytest.mark.parametrize("program", ["meteora_dlmm", "raydium_clmm"])
def test_the_median_concentrated_pool_mark_is_refused(program):
    """DLMM's median is 4.40x and CLMM's is 0.805 — both outside the band, both refused."""
    assert RATIO_TABLE[program]["concentrated"]
    assert not _admits(RATIO_TABLE[program]["median"])
    assert not _admits(RATIO_TABLE[program]["p90"]), "the p90 tail must never price a position"


@pytest.mark.parametrize("program", ["raydium_cpmm", "pumpswap_amm"])
def test_the_median_constant_product_mark_is_admitted(program):
    """The reserve ratio IS the price here, so the median mark must survive the gate."""
    assert not RATIO_TABLE[program]["concentrated"]
    assert _admits(RATIO_TABLE[program]["median"])


def test_the_clmm_low_tail_that_could_trip_a_stop_is_refused():
    """The acute case. raydium_clmm p10 is 0.546 — a mark 45% BELOW the trade price, enough on its
    own to cross a 35% stop and to approach the 80% catastrophe floor."""
    assert not _admits(0.546)


def test_the_band_edges_are_exact():
    assert _admits(1.0), "a mark equal to the swap must always pass"
    assert _admits(0.90) and _admits(1.10), "the band is inclusive"
    assert not _admits(0.899) and not _admits(1.101)


def test_a_state_mark_with_no_swap_is_never_admitted():
    """No corroboration is possible, so there is no evidence this pool is constant product."""
    cache = SolanaSwapMarkCache()
    cache.record(_sync(), _binding(), now_ts=100.0, observed_at="state")
    assert cache.fresh_price_map({WSOL: 200.0}, now_ts=101.0, max_age_s=5.0) == {}


def test_a_stale_swap_cannot_corroborate():
    """Corroboration is evidence about the pool NOW. An old swap is not that, and admitting on one
    would reopen the gate whenever a pool went quiet."""
    cache = SolanaSwapMarkCache()
    cache.record(_agreeing_swap(), _binding(), now_ts=100.0, observed_at="swap")
    cache.record(_sync(), _binding(), now_ts=120.0, observed_at="state")
    assert cache.fresh_price_map({WSOL: 200.0}, now_ts=121.0, max_age_s=5.0) == {}


def test_a_refused_state_mark_leaves_the_swap_mark_in_place():
    """WHY REJECTION IS CHEAP. The gate needs a fresh swap to reject at all, so the mint always
    keeps a price — the lane lands exactly where it stands today, never blind."""
    cache = _cache_at_ratio(4.40)
    mark = cache.fresh_price_map({WSOL: 200.0}, now_ts=102.0, max_age_s=5.0)[("solana", "MintA")]
    assert mark["source"] == "fastlane_sol_swap"
    assert mark["price"] == pytest.approx(0.03)


def test_an_older_state_mark_never_replaces_a_newer_swap():
    """The state lane earns its place by being NEWER: the pool moved after the last trade. An
    older one only adds a second opinion on a price the swap already reported better."""
    cache = SolanaSwapMarkCache()
    cache.record(_sync(), _binding(), now_ts=100.0, observed_at="state")
    cache.record(_agreeing_swap(), _binding(), now_ts=101.0, observed_at="swap")
    mark = cache.fresh_price_map({WSOL: 200.0}, now_ts=102.0, max_age_s=5.0)[("solana", "MintA")]
    assert mark["source"] == "fastlane_sol_swap"


def test_a_cross_quote_pair_never_corroborates():
    """A ratio across two quote units is not a ratio."""
    cache = SolanaSwapMarkCache()
    cache.record(_swap(amount1="3000000", decimals1=6), _binding(USDC),
                 now_ts=100.0, observed_at="swap")
    cache.record(_sync(), _binding(), now_ts=101.0, observed_at="state")
    marks = cache.fresh_price_map({WSOL: 200.0, USDC: 1.0}, now_ts=102.0, max_age_s=5.0)
    assert marks[("solana", "MintA")]["source"] == "fastlane_sol_swap"


def test_the_hot_tp_wake_never_fires_on_an_uncorroborated_state_mark():
    """The wake is a level reader: it starts a quote pass against the mark's price."""
    woken = []
    cache = SolanaSwapMarkCache(on_mark=woken.append)
    cache.record(_agreeing_swap(), _binding(), now_ts=100.0, observed_at="swap")
    cache.record(_sync(amount1=str(int(300000000000 * 4.40))), _binding(),
                 now_ts=101.0, observed_at="bad-state")
    assert [m.observed_at for m in woken] == ["swap"]
    cache.record(_sync(), _binding(), now_ts=102.0, observed_at="good-state")
    assert [m.observed_at for m in woken] == ["swap", "good-state"]


def test_invalidate_clears_both_slots():
    cache = SolanaSwapMarkCache()
    cache.record(_agreeing_swap(), _binding(), now_ts=100.0, observed_at="swap")
    cache.record(_sync(), _binding(), now_ts=101.0, observed_at="state")
    cache.invalidate()
    assert cache._marks == {} and cache._state_marks == {}
    assert cache.quote_mints() == set()


def test_the_epoch_logs_which_pools_it_subscribed_to(caplog):
    """QQQ 63509 could not be diagnosed because nothing recorded what the epoch was watching.

    Five minutes, a pool that traded six times besides our own entry and exit, zero marks — while
    the SAME pool and quote asset produced a mark nine seconds after binding on 09-20. Two
    explanations survived and no persisted artefact separated them, because the only trace either
    leaves is the absence itself. This line is that artefact.
    """
    import asyncio
    import logging

    async def stream(_vaults, on_row, **_kwargs):
        return None

    with caplog.at_level(logging.INFO, logger="memecoin"):
        asyncio.run(run_price_stream_epoch(
            {"CoinV": object(), "QuoteV": object()},
            {"PoolA": _binding()},
            SolanaSwapMarkCache(),
            ws_url="wss://rpc.invalid", duration_s=1.0, stream_fn=stream,
            now_ts_fn=lambda: 100.0, now_iso_fn=lambda: "t"))

    line = next((r.getMessage() for r in caplog.records
                 if "sol price stream epoch" in r.getMessage()), None)
    assert line, "the epoch recorded nothing about its own subscription"
    assert "2 vault(s)" in line and "1 pool(s)" in line
    assert "PoolA" in line, "the pool key must be nameable in the log"


def test_a_broken_epoch_log_never_costs_the_epoch(monkeypatch):
    """Observability that can break the funded lane is not observability."""
    import asyncio

    calls = []

    async def stream(_vaults, on_row, **_kwargs):
        calls.append(1)
        return None

    class _Exploding(dict):
        def __iter__(self):
            raise RuntimeError("cannot enumerate")

    asyncio.run(run_price_stream_epoch(
        {"CoinV": object()}, _Exploding(), SolanaSwapMarkCache(),
        ws_url="wss://rpc.invalid", duration_s=1.0, stream_fn=stream,
        now_ts_fn=lambda: 100.0, now_iso_fn=lambda: "t"))
    assert calls == [1], "a logging failure stopped the stream"
