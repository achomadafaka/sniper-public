"""Native float drift (operator order 2026-10-08 ~16:35Z, via Alpha).

The runner samples the idle native float (ETH on 4663/8453/1, BNB on 56, SOL on Solana; native and
wrapped) and its USD price once a minute into its own files; the web panel reads those files only.
Spec docs/superpowers/specs/2026-10-08-memecoin-native-float-drift-panel-design.md.
"""
from __future__ import annotations

import json
import math
import os

import pytest

from sniper.services import memecoin_native_float as nf

T0 = 1_791_500_000.0          # an epoch second, 2026-10-08


@pytest.fixture(autouse=True)
def _clean_recorder():
    nf._LAST.clear()
    yield
    nf._LAST.clear()


def _store(tmp_path):
    return nf.Store(str(tmp_path / "native_float.db"))


# ------------------------------------------------------------------ the recorder (no new reads)

def test_the_recorder_keeps_the_last_good_value_and_its_time():
    nf.remember("4663", "native", 0.05, now=T0)
    nf.remember("4663", "wrapped", 0.11, now=T0 + 1)
    nf.remember("4663", "native", 0.04, now=T0 + 2)
    got = nf.recorded("4663", now=T0 + 3)
    assert got == {"native": 0.04, "wrapped": 0.11, "at": T0 + 1}      # oldest of the two parts


@pytest.mark.parametrize("bad", [None, -1.0, float("nan"), float("inf"), "x", True])
def test_the_recorder_ignores_a_value_that_is_not_a_balance(bad):
    nf.remember("56", "native", 0.3, now=T0)
    nf.remember("56", "native", bad, now=T0 + 1)
    nf.remember("56", "wrapped", 0.0, now=T0)                            # a real zero is kept
    assert nf.recorded("56", now=T0 + 2) == {"native": 0.3, "wrapped": 0.0, "at": T0}


def test_a_recorded_value_older_than_the_limit_is_unknown():
    nf.remember("56", "native", 0.3, now=T0)
    nf.remember("56", "wrapped", 0.1, now=T0)
    assert nf.recorded("56", now=T0 + nf.RECORDER_MAX_AGE_S + 1) is None
    assert nf.recorded("8453", now=T0) is None                           # never recorded


def test_the_recorder_never_raises():
    nf.remember(object(), object(), object())                           # garbage in, nothing out


def test_the_raw_recorder_divides_by_the_decimals():
    nf.remember_raw("4663", "wrapped", 110_000_000_000_000_000, 18, now=T0)
    nf.remember_raw("4663", "native", None, 18, now=T0)                 # a failed read: ignored
    nf.remember_raw("4663", "native", 50_000_000_000_000_000, 18, now=T0)
    assert nf.recorded("4663", now=T0) == {"native": 0.05, "wrapped": 0.11, "at": T0}


# ------------------------------------------------------------------ prices (Binance, public)

def _binance(prices):
    def get(url, timeout):
        assert "api.binance.com" in url and timeout <= 6
        return [{"symbol": s, "price": str(p)} for s, p in prices.items()]
    return get


def test_prices_come_from_one_binance_call():
    calls = []

    def get(url, timeout):
        calls.append(url)
        return _binance({"ETHUSDT": 2400.5, "BNBUSDT": 590.1, "SOLUSDT": 140.25})(url, timeout)
    assert nf.fetch_prices(get=get) == {"ETH": 2400.5, "BNB": 590.1, "SOL": 140.25}
    assert len(calls) == 1 and "ticker/price" in calls[0]


@pytest.mark.parametrize("answer", [RuntimeError("down"), {"code": -1121}, [{"symbol": "ETHUSDT", "price": "0"}],
                                    [{"symbol": "ETHUSDT", "price": "nan"}]])
def test_a_failed_or_bad_price_is_absent_never_zero(answer):
    def get(url, timeout):
        if isinstance(answer, Exception):
            raise answer
        return answer
    got = nf.fetch_prices(get=get)
    assert "ETH" not in got and all(v > 0 for v in got.values())


def test_klines_backfill_returns_each_candles_open_price_at_its_open_time():
    # Binance kline row: [open time, open, high, low, close, volume, close time, ...] (verified on
    # the live API 2026-10-08). The close is the price at the CLOSE time; stamping it at the open
    # time put every backfilled price an hour early, and the running candle's close is now's price.
    def get(url, timeout):
        assert "klines" in url and "interval=1h" in url and "limit=168" in url
        return [[int(T0 * 1000), "2390.0", "1", "1", "2400.0", "9", int(T0 * 1000) + 3599999],
                [int((T0 + 3600) * 1000), "2400.0", "1", "1", "2380.0", "9", int(T0 * 1000) + 7199999]]
    assert nf.fetch_klines("ETH", get=get) == [(T0, 2390.0), (T0 + 3600, 2400.0)]


# ------------------------------------------------------------------ public balances for 8453 and 1

def _rpc(chain_id_hex, native_wei, wrapped_wei):
    seen = []

    def post(body):
        seen.append(body)
        out = []
        for call in body:
            if call["method"] == "eth_chainId":
                out.append({"id": call["id"], "result": chain_id_hex})
            elif call["method"] == "eth_getBalance":
                out.append({"id": call["id"], "result": hex(native_wei)})
            else:
                out.append({"id": call["id"], "result": "0x" + format(wrapped_wei, "064x")})
        return out
    return post, seen


def test_public_balances_read_native_and_weth_in_one_batch():
    post, seen = _rpc("0x2105", 30_000_000_000_000_000, 10_000_000_000_000_000)
    assert nf.public_balances("8453", "0x" + "11" * 20, post=post) == (0.03, 0.01)
    (batch,) = seen
    assert [c["method"] for c in batch] == ["eth_chainId", "eth_getBalance", "eth_call"]
    assert batch[2]["params"][0]["to"].lower() == "0x4200000000000000000000000000000000000006"


def test_a_public_endpoint_on_the_wrong_chain_is_refused():
    post, _ = _rpc("0x1", 30_000_000_000_000_000, 0)
    assert nf.public_balances("8453", "0x" + "11" * 20, post=post) is None


def test_a_public_read_that_fails_is_unknown_not_zero():
    def post(body):
        raise OSError("timeout")
    assert nf.public_balances("1", "0x" + "11" * 20, post=post) is None


# ------------------------------------------------------------------ drift math

def test_drift_is_units_times_the_price_move_at_constant_units():
    prices = [(T0, 2000.0), (T0 + 60, 1900.0), (T0 + 120, 1800.0)]
    units = [(T0, 0.1)]
    assert nf.drift(prices, units, T0, T0 + 120) == pytest.approx(-20.0)


def test_a_balance_change_is_not_price_drift():
    """We sold 0.05 ETH at minute 1: the value fell by 0.05 x 1900, but drift counts only price."""
    prices = [(T0, 2000.0), (T0 + 60, 1900.0), (T0 + 120, 1900.0)]
    units = [(T0, 0.1), (T0 + 60, 0.05)]
    assert nf.drift(prices, units, T0, T0 + 120) == pytest.approx(-10.0)


def test_drift_before_the_first_unit_sample_uses_the_earliest_units():
    prices = [(T0 - 3600, 2100.0), (T0, 2000.0)]
    units = [(T0, 0.1)]
    assert nf.drift(prices, units, T0 - 3600, T0) == pytest.approx(-10.0)


def test_drift_without_units_or_prices_is_unknown():
    assert nf.drift([], [(T0, 0.1)], T0, T0 + 60) is None
    assert nf.drift([(T0, 1.0), (T0 + 60, 2.0)], [], T0, T0 + 60) is None


# ------------------------------------------------------------------ the sampler and its snapshot

def _sample(tmp_path, *, get, post_for=lambda chain: None, now=T0, store=None, public_due=True):
    store = store or _store(tmp_path)
    snap = nf.sample_once(store, now=now, get=get, post_for=post_for, evm_address="0x" + "11" * 20,
                          json_path=str(tmp_path / "native_float.json"), public_due=public_due)
    return store, snap


def test_a_sample_values_every_asset_and_writes_the_json_atomically(tmp_path):
    nf.remember("4663", "native", 0.05, now=T0)
    nf.remember("4663", "wrapped", 0.10, now=T0)
    nf.remember("56", "native", 0.30, now=T0)
    nf.remember("56", "wrapped", 0.034, now=T0)
    nf.remember("solana", "native", 2.0, now=T0)
    nf.remember("solana", "wrapped", 0.0, now=T0)
    posts = {c: _rpc(h, w1, w2)[0] for c, h, w1, w2 in (
        ("8453", "0x2105", 10 ** 16, 0), ("1", "0x1", 40_200_000_000_000_000, 0))}
    _store_, snap = _sample(tmp_path, get=_binance({"ETHUSDT": 2400.0, "BNBUSDT": 600.0, "SOLUSDT": 150.0}),
                            post_for=posts.get)
    eth = snap["assets"]["ETH"]
    assert eth["units"] == pytest.approx(0.05 + 0.10 + 0.01 + 0.0402)
    assert eth["value_usd"] == pytest.approx(eth["units"] * 2400.0)
    assert {c["chain"] for c in eth["chains"]} == {"4663", "8453", "1"}
    assert snap["assets"]["BNB"]["value_usd"] == pytest.approx(0.334 * 600.0)
    assert snap["assets"]["SOL"]["value_usd"] == pytest.approx(300.0)
    assert snap["partial"] is False and snap["price_ok"] is True
    on_disk = json.loads((tmp_path / "native_float.json").read_text())
    assert on_disk["ts"] == T0 and on_disk["assets"]["ETH"]["price_usd"] == 2400.0
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".tmp")]


def test_binance_down_is_price_unavailable_never_the_last_price(tmp_path):
    nf.remember("solana", "native", 2.0, now=T0)
    nf.remember("solana", "wrapped", 0.0, now=T0)
    store, _ = _sample(tmp_path, get=_binance({"SOLUSDT": 150.0}), now=T0)

    def down(url, timeout):
        raise OSError("unreachable")
    nf.remember("solana", "native", 2.0, now=T0 + 60)
    nf.remember("solana", "wrapped", 0.0, now=T0 + 60)
    _, snap = _sample(tmp_path, get=down, now=T0 + 60, store=store)
    sol = snap["assets"]["SOL"]
    assert sol["price_usd"] is None and sol["value_usd"] is None
    assert snap["price_ok"] is False
    assert sol["last_price_usd"] == 150.0 and sol["last_price_age_s"] == 60.0     # shown as old, not live


def test_an_unknown_chain_makes_the_total_partial_not_zero(tmp_path):
    nf.remember("4663", "native", 0.05, now=T0)
    nf.remember("4663", "wrapped", 0.10, now=T0)
    _, snap = _sample(tmp_path, get=_binance({"ETHUSDT": 2400.0, "BNBUSDT": 600.0, "SOLUSDT": 150.0}))
    assert snap["partial"] is True
    eth = snap["assets"]["ETH"]
    unknown = [c for c in eth["chains"] if c["units"] is None]
    assert {c["chain"] for c in unknown} == {"8453", "1"}
    assert eth["value_usd"] == pytest.approx(0.15 * 2400.0)
    assert snap["assets"]["BNB"]["value_usd"] is None                   # nothing known: not $0


def test_public_reads_run_only_when_due_and_keep_their_units(tmp_path):
    calls = []

    def post_for(chain):
        def post(body):
            calls.append(chain)
            return _rpc({"8453": "0x2105", "1": "0x1"}[chain], 10 ** 16, 0)[0](body)
        return post
    for chain in ("4663", "56", "solana"):                     # the recorder covers these
        nf.remember(chain, "native", 0.1, now=T0)
        nf.remember(chain, "wrapped", 0.0, now=T0)
    store, _ = _sample(tmp_path, get=_binance({"ETHUSDT": 2400.0}), post_for=post_for, public_due=True)
    _, snap = _sample(tmp_path, get=_binance({"ETHUSDT": 2400.0}), post_for=post_for, now=T0 + 60,
                      store=store, public_due=False)
    assert sorted(calls) == ["1", "8453"]                              # once, not twice
    base = next(c for c in snap["assets"]["ETH"]["chains"] if c["chain"] == "8453")
    assert base["units"] == pytest.approx(0.01) and base["age_s"] == 60.0


def test_a_public_read_falls_back_for_4663_when_the_recorder_is_silent(tmp_path):
    asked = []

    def post_for(chain):
        def post(body):
            asked.append(chain)
            return _rpc({"4663": hex(4663), "56": "0x38", "8453": "0x2105", "1": "0x1"}[chain], 10 ** 16, 0)[0](body)
        return post
    _, snap = _sample(tmp_path, get=_binance({"ETHUSDT": 2400.0}), post_for=post_for)
    assert "4663" in asked and "56" in asked


def _eth_rows(store, ts, *, c4663=0.1, c8453=None, c1=None):
    for chain, units in (("4663", c4663), ("8453", c8453), ("1", c1)):
        if units is not None:
            store.add_units(ts, chain, "ETH", units, 0.0, "test")


def test_units_carry_each_chain_forward_between_its_reads(tmp_path):
    # 2026-10-08 live defect: 8453 and 1 are read every 5 min, 4663 every minute, and the sum per
    # timestamp dropped 8453/1 on four samples of five, so ETH drift counted half the float. The
    # fix is on the READ path, so the rows already on disk come out right too.
    store = _store(tmp_path)
    _eth_rows(store, T0, c8453=0.05, c1=0.04)
    _eth_rows(store, T0 + 60)
    _eth_rows(store, T0 + 120, c4663=0.2)
    assert store.units_since("ETH", T0) == [
        (T0, pytest.approx(0.19), T0), (T0 + 60, pytest.approx(0.19), T0), (T0 + 120, pytest.approx(0.29), T0)]
    # A read just before `since` still counts; the oldest observation in force rides with each point.
    assert store.units_since("ETH", T0 + 30) == [(T0 + 60, pytest.approx(0.19), T0),
                                                 (T0 + 120, pytest.approx(0.29), T0)]


def test_review_sf06_probe_constant_balances_on_two_cadences(tmp_path):
    # Engineering review 2026-10-08 SF06, exact probe: 4663=1 and Base=1 at T, 4663 alone at T+60,
    # ETH 100/200/300. Constant 2 ETH is $400; the per-timestamp sum gave $300. Chain 1 is an
    # explicit zero here: the strict rule needs every ETH chain read.
    store = _store(tmp_path)
    for i, price in enumerate((100.0, 200.0, 300.0)):
        store.add_prices(T0 + 60 * i, {"ETH": price}, "ticker")
    _eth_rows(store, T0, c4663=1.0, c8453=1.0, c1=0.0)
    _eth_rows(store, T0 + 60, c4663=1.0)
    assert nf.drift(store.prices_since("ETH", T0), store.units_since("ETH", T0), T0, T0 + 120) == pytest.approx(400.0)


def test_a_chain_past_the_age_limit_makes_the_total_unknown_not_smaller(tmp_path):
    store = _store(tmp_path)
    _eth_rows(store, T0, c8453=0.05, c1=0.04)
    _eth_rows(store, T0 + nf.UNITS_MAX_AGE_S + 1, c1=0.04)          # 8453 not read again
    assert store.units_since("ETH", T0 + 1) == [(T0 + nf.UNITS_MAX_AGE_S + 1, None, None)]


def test_a_chain_first_read_mid_window_is_not_booked_as_drift(tmp_path):
    store = _store(tmp_path)
    for i, price in enumerate((2000.0, 2100.0, 2200.0, 2300.0)):
        store.add_prices(T0 + 60 * i, {"ETH": price}, "ticker")
    _eth_rows(store, T0, c8453=0.05)                                 # chain 1 not read yet
    _eth_rows(store, T0 + 60)
    _eth_rows(store, T0 + 120, c8453=0.05, c1=1.0)                   # chain 1's first read
    _eth_rows(store, T0 + 180)
    prices, units = store.prices_since("ETH", T0), store.units_since("ETH", T0)
    assert nf.drift(prices, units, T0, T0 + 180) is None             # unknown, not 0.15 x $300
    assert nf.drift(prices, units, T0 + 120, T0 + 180) == pytest.approx(1.15 * 100.0)


def test_an_asset_that_stops_writing_rows_is_unknown_after_the_age_limit():
    # Gamma 2026-10-08: units_since emits None only at a timestamp with some row of the asset; a
    # whole-asset stall (SOL slot-lane recorder) wrote no row, so drift held the last units for ever.
    prices = [(T0 + 60 * i, 150.0 + i) for i in range(61)]                  # one hour, $1 a minute
    units = [(T0, 2.0)]
    assert nf.drift(prices, units, T0, T0 + 3600) is None
    assert nf.drift(prices, units, T0, T0 + nf.UNITS_MAX_AGE_S) == pytest.approx(2.0 * nf.UNITS_MAX_AGE_S / 60)
    # The backfill before the first unit sample stays: prices older than the units use them.
    assert nf.drift([(T0 - 3600, 140.0), (T0, 150.0)], [(T0, 2.0)], T0 - 3600, T0) == pytest.approx(20.0)


@pytest.mark.parametrize("coverage", ["gap", "continuous", "copied", "no_refresh"])
def test_sparse_prices_do_not_hide_a_balance_observation_gap(coverage):
    prices = [(T0, 140.0), (T0 + 3600, 150.0)]
    times = range(0, 3601, 300) if coverage in ("continuous", "copied") else (0, 3600)
    units = [(T0 + seconds, 2.0, T0 + seconds) for seconds in times]
    if coverage == "copied":
        units = [(ts, held, T0 if ts < T0 + 3600 else ts) for ts, held, _seen in units]
    elif coverage == "no_refresh":
        units = units[:1]
    assert nf.drift(prices, units, T0, T0 + 3600) == (20.0 if coverage == "continuous" else None)


def test_a_missing_asset_makes_the_headline_partial_and_names_it(tmp_path):
    for chain in ("4663", "8453", "1", "56", "solana"):
        nf.remember(chain, "native", 0.1, now=T0)
        nf.remember(chain, "wrapped", 0.0, now=T0)
    store, _ = _sample(tmp_path, get=_binance({"ETHUSDT": 2000.0, "BNBUSDT": 600.0, "SOLUSDT": 150.0}))
    for chain in ("4663", "8453", "1", "56", "solana"):
        nf.remember(chain, "native", 0.1, now=T0 + 60)
        nf.remember(chain, "wrapped", 0.0, now=T0 + 60)
    _, snap = _sample(tmp_path, get=_binance({"ETHUSDT": 2100.0, "SOLUSDT": 160.0}), now=T0 + 60, store=store)
    hero = snap["headline"]["1h"]
    assert hero["usd"] == pytest.approx(0.3 * 100.0 + 0.1 * 10.0)
    assert hero["partial"] is True and hero["missing"] == ["BNB"]


def test_drift_counts_a_chain_between_its_public_reads(tmp_path):
    # The live writer: 4663/56/Solana from the recorder every 60 s, 8453 and 1 public every 300 s.
    def post_for(chain):
        return _rpc({"8453": "0x2105", "1": "0x1"}[chain], 10 ** 17, 0)[0]
    store = None
    prices = (2000.0, 2000.0, 2000.0, 2100.0, 2100.0, 2100.0, 2200.0)    # moves between public reads
    for i, price in enumerate(prices):
        for chain in ("4663", "56", "solana"):
            nf.remember(chain, "native", 0.1, now=T0 + 60 * i)
            nf.remember(chain, "wrapped", 0.0, now=T0 + 60 * i)
        store, snap = _sample(tmp_path, get=_binance({"ETHUSDT": price, "BNBUSDT": 600.0, "SOLUSDT": 150.0}),
                              post_for=post_for, now=T0 + 60 * i, store=store, public_due=(i % 5 == 0))
    # 0.1 (4663) + 0.1 (8453) + 0.1 (1) ETH held across two $100 moves: $60, not the $20 of 4663 alone.
    end = T0 + 60 * (len(prices) - 1)
    assert nf.drift(store.prices_since("ETH", T0), store.units_since("ETH", T0), T0, end) == pytest.approx(60.0)


def test_windows_and_sparkline_come_from_the_store(tmp_path):
    store = _store(tmp_path)
    store.add_prices(T0 - 7200, {"ETH": 2500.0}, "klines")
    store.add_prices(T0 - 3600, {"ETH": 2450.0}, "klines")
    for chain, units in (("4663", 0.1), ("8453", 0.0), ("1", 0.0)):
        nf.remember(chain, "native", units, now=T0)
        nf.remember(chain, "wrapped", 0.0, now=T0)
    _, snap = _sample(tmp_path, get=_binance({"ETHUSDT": 2400.0}), store=store)
    eth = snap["assets"]["ETH"]
    assert eth["change"]["1h"]["pct"] == pytest.approx(100.0 * (2400.0 / 2450.0 - 1))
    assert eth["change"]["1h"]["usd"] == pytest.approx(0.1 * (2400.0 - 2450.0))
    assert eth["sparkline"]["24h"][-1] == [T0, 2400.0]
    assert eth["sparkline"]["1h"][0] == [T0 - 3600, 2450.0] and len(eth["sparkline"]["7d"]) == 3
    assert snap["headline"]["24h"]["usd"] == pytest.approx(0.1 * (2400.0 - 2500.0))


def test_there_is_no_baseline_window(tmp_path):
    # Operator 2026-10-08: the baseline reset is removed; drift has 1h, 24h and 7d only.
    for chain in ("4663", "8453", "1"):
        nf.remember(chain, "native", 0.1, now=T0)
        nf.remember(chain, "wrapped", 0.0, now=T0)
    _, snap = _sample(tmp_path, get=_binance({"ETHUSDT": 2400.0}))
    assert set(snap["headline"]) == set(snap["assets"]["ETH"]["change"]) == {"1h", "24h", "7d"}
    assert set(snap["assets"]["ETH"]["sparkline"]) == {"1h", "24h", "7d"} and "baseline" not in snap


def test_the_sampler_never_raises_into_the_runner(tmp_path, caplog):
    class Broken:
        def add_prices(self, *a, **k):
            raise RuntimeError("disk full")
    snap = nf.sample_once(Broken(), now=T0, get=_binance({"ETHUSDT": 1.0}), post_for=lambda c: None,
                          evm_address=None, json_path=str(tmp_path / "x.json"), public_due=True)
    assert snap is None
    assert any("native float" in r.getMessage() for r in caplog.records)


def test_old_rows_are_pruned_after_30_days(tmp_path):
    store = _store(tmp_path)
    store.add_prices(T0 - 31 * 86400, {"ETH": 1.0}, "ticker")
    store.add_prices(T0 - 1, {"ETH": 2.0}, "ticker")
    store.prune(now=T0)
    assert store.prices_since("ETH", 0) == [(T0 - 1, 2.0)]


# ------------------------------------------------------------------ no funded seam (Alpha condition 1)

def test_the_sampler_cannot_reach_a_funded_seam(tmp_path, monkeypatch):
    """Jupiter, custody RPC and DexScreener raise if touched; a full sample still completes."""
    from sniper.execution import evm_wallet, solana_real_seams
    from sniper.execution import solana_quote

    def boom(*a, **k):
        raise AssertionError("funded seam reached")
    monkeypatch.setattr(solana_real_seams, "_quote_with_fallback", boom)
    monkeypatch.setattr(evm_wallet, "native_balance_or_none", boom)
    monkeypatch.setattr(evm_wallet, "erc20_balance", boom)
    monkeypatch.setattr(evm_wallet, "native_usd_price", boom)
    monkeypatch.setattr(solana_quote, "dexscreener_stats_batch_keyed", boom, raising=False)
    nf.remember("solana", "native", 1.0, now=T0)
    nf.remember("solana", "wrapped", 0.0, now=T0)
    _, snap = _sample(tmp_path, get=_binance({"SOLUSDT": 150.0, "ETHUSDT": 2400.0, "BNBUSDT": 600.0}))
    assert snap["assets"]["SOL"]["value_usd"] == 150.0


def test_the_module_imports_no_funded_seam():
    import inspect
    src = inspect.getsource(nf)
    for forbidden in ("solana_real_seams", "_quote_with_fallback", "dexscreener", "evm_rpc_for_chain",
                      "MEMECOIN_SOLANA_RPC_URL", "solana_funded_rpc_urls", "jupiter"):
        assert forbidden not in src, forbidden


# ------------------------------------------------------------------ runner wiring

def test_the_gas_pass_and_the_prefetch_feed_the_recorder():
    import inspect
    from sniper.services import memecoin_evm_leg, memecoin_slot_lane
    leg = inspect.getsource(memecoin_evm_leg)
    assert "from sniper.services.memecoin_native_float import remember_raw as _float_remember" in leg
    assert '_float_remember(\n            chain, "native", native_balance_raw_or_none(' in leg
    assert '_float_remember(\n            chain, "wrapped", erc20_balance_raw(' in leg
    lane = inspect.getsource(memecoin_slot_lane.LanePrefetcher)
    assert 'remember("solana", "native"' in lane


def test_the_sampler_thread_is_off_with_its_knob(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_NATIVE_FLOAT_SAMPLER", False, raising=False)
    assert nf.start_sampler(evm_address=None) is None


# ------------------------------------------------------------------ SF07: observation time, not sample time
# Engineering review 2026-10-08 SF07: a recorded balance copied into the store at sample time looked
# observed then, so an old reading outlived the 15 min units limit; the age shown restarted at each copy.

def _sol(snap):
    return next(c for c in snap["assets"]["SOL"]["chains"] if c["chain"] == "solana")


def test_review_sf07_probe_a_recorded_value_keeps_its_own_age(tmp_path):
    nf.remember("solana", "native", 1.0, now=T0)
    store, snap = _sample(tmp_path, get=_binance({"SOLUSDT": 100.0}), now=T0 + 600, public_due=False)
    assert _sol(snap)["units"] == 1.0 and _sol(snap)["age_s"] == 600.0
    _, snap = _sample(tmp_path, get=_binance({"SOLUSDT": 100.0}), now=T0 + 1200, store=store, public_due=False)
    assert _sol(snap)["units"] is None and _sol(snap)["age_s"] is None        # 1200 s old: unknown


@pytest.mark.parametrize("age, known", [(nf.UNITS_MAX_AGE_S - 1, True), (nf.UNITS_MAX_AGE_S, True),
                                        (nf.UNITS_MAX_AGE_S + 1, False)])
def test_a_stored_balance_expires_on_its_observation_age(tmp_path, age, known):
    nf.remember("solana", "native", 1.0, now=T0)
    store, _ = _sample(tmp_path, get=_binance({"SOLUSDT": 100.0}), now=T0 + 1, public_due=False)
    nf._LAST.clear()                                              # the recorder stops
    _, snap = _sample(tmp_path, get=_binance({"SOLUSDT": 100.0}), now=T0 + age, store=store, public_due=False)
    assert (_sol(snap)["units"] is not None) is known
    if known:
        assert _sol(snap)["age_s"] == age


def test_the_age_grows_until_a_new_read(tmp_path):
    nf.remember("solana", "native", 1.0, now=T0)
    store, ages = None, []
    for t in (T0 + 60, T0 + 120, T0 + 180):
        store, snap = _sample(tmp_path, get=_binance({"SOLUSDT": 100.0}), now=t, store=store, public_due=False)
        ages.append(_sol(snap)["age_s"])
    nf.remember("solana", "native", 1.0, now=T0 + 200)
    _, snap = _sample(tmp_path, get=_binance({"SOLUSDT": 100.0}), now=T0 + 240, store=store, public_due=False)
    assert ages == [60.0, 120.0, 180.0] and _sol(snap)["age_s"] == 40.0


def test_the_older_of_native_and_wrapped_is_the_observation(tmp_path):
    nf.remember("4663", "native", 0.1, now=T0 + 500)
    nf.remember("4663", "wrapped", 0.2, now=T0)
    store, _ = _sample(tmp_path, get=_binance({"ETHUSDT": 2000.0}), now=T0 + 550, public_due=False)
    nf._LAST.clear()
    _, snap = _sample(tmp_path, get=_binance({"ETHUSDT": 2000.0}), now=T0 + 950, store=store, public_due=False)
    chain = next(c for c in snap["assets"]["ETH"]["chains"] if c["chain"] == "4663")
    assert chain["units"] is None                         # wrapped observed at T0: 950 s old


def test_history_expires_a_copied_old_reading(tmp_path):
    # The recorder holds T0's reading; the sampler copied it into a row each minute until T0+600.
    # Drift must go unknown 900 s after the OBSERVATION (T0), not 900 s after the last copy.
    nf.remember("solana", "native", 1.0, now=T0)
    store = None
    for i in range(1, 18):
        store, _ = _sample(tmp_path, get=_binance({"SOLUSDT": 100.0 + i}), now=T0 + 60 * i, store=store,
                           public_due=False)
    prices, units = store.prices_since("SOL", T0), store.units_since("SOL", T0)
    assert nf.drift(prices, units, T0 + 60, T0 + 900) == pytest.approx(1.0 * 14)
    assert nf.drift(prices, units, T0 + 60, T0 + 1020) is None


def test_observation_times_survive_a_restart(tmp_path):
    nf.remember("solana", "native", 1.0, now=T0)
    _sample(tmp_path, get=_binance({"SOLUSDT": 100.0}), now=T0 + 300, public_due=False)
    nf._LAST.clear()
    reopened = _store(tmp_path)
    assert reopened.last_units("solana")["at"] == T0


def test_a_store_written_before_observation_times_reads_its_rows_as_sample_time(tmp_path):
    import sqlite3
    path = tmp_path / "native_float.db"
    db = sqlite3.connect(str(path))
    db.executescript("CREATE TABLE native_float_units(ts REAL NOT NULL, chain TEXT NOT NULL, asset TEXT NOT NULL,"
                     " native_units REAL, wrapped_units REAL, source TEXT, PRIMARY KEY(chain, ts));")
    db.execute("INSERT INTO native_float_units VALUES (?,?,?,?,?,?)", (T0, "solana", "SOL", 1.0, None, "recorder"))
    db.commit()
    db.close()
    store = nf.Store(str(path))
    assert store.last_units("solana")["at"] == T0 and store.units_since("SOL", T0) == [(T0, 1.0, T0)]


def test_the_migration_drops_the_klines_stamped_an_hour_early(tmp_path):
    # Gamma LOW on 68c8f1e07: the live store holds klines stamped close-at-open; has_prices() would
    # keep them for 7 days. The one-time observed_at migration drops them so the backfill refetches.
    import sqlite3
    path = tmp_path / "native_float.db"
    db = sqlite3.connect(str(path))
    db.executescript("CREATE TABLE native_float_prices(ts REAL NOT NULL, asset TEXT NOT NULL, price_usd REAL NOT NULL,"
                     " source TEXT, PRIMARY KEY(asset, ts));"
                     "CREATE TABLE native_float_units(ts REAL NOT NULL, chain TEXT NOT NULL, asset TEXT NOT NULL,"
                     " native_units REAL, wrapped_units REAL, source TEXT, PRIMARY KEY(chain, ts));")
    db.executemany("INSERT INTO native_float_prices VALUES (?,?,?,?)",
                   [(T0 - 7200, "ETH", 2400.0, "klines"), (T0, "ETH", 2410.0, "ticker")])
    db.commit()
    db.close()
    store = nf.Store(str(path))
    assert store.prices_since("ETH", 0) == [(T0, 2410.0)]
    store.add_prices(T0 - 3600, {"ETH": 2405.0}, "klines")              # a fresh backfill stays
    assert nf.Store(str(path)).prices_since("ETH", 0) == [(T0 - 3600, 2405.0), (T0, 2410.0)]


# ------------------------------------------------------------------ the restart gap (Gamma 2026-10-08)

@pytest.mark.parametrize("gap_s,history_ok", [(60, True), (22 * 3600, True), (22 * 3600, False)])
def test_restart_refreshes_price_history_after_a_long_gap(tmp_path, monkeypatch, gap_s, history_ok):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_NATIVE_FLOAT_SAMPLER", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EQUITY_RECORDER", False)
    store = _store(tmp_path)
    for asset in nf.SYMBOLS:
        store.add_prices(T0 - nf.WINDOWS["7d"] - 3600, {asset: 100.0}, "ticker")
        store.add_prices(T0 - gap_s, {asset: 100.0}, "ticker")
    for chain, asset in nf.ASSET_OF_CHAIN.items():
        store.add_units(T0 - gap_s, chain, asset, 1.0, None if chain == "solana" else 1.0,
                        "recorder", observed_at=T0 - gap_s)
    store.db.close()
    for chain in nf.ASSET_OF_CHAIN:
        nf.remember(chain, "native", 1.0, now=T0)
        if chain != "solana":
            nf.remember(chain, "wrapped", 1.0, now=T0)
    history_calls = []

    def get(url, timeout):
        if "klines" in url:
            history_calls.append(url)
            if not history_ok:
                raise TimeoutError("history unavailable")
            return [[int((T0 - 3600) * 1000), "100.0", "1", "1", "100.0", "9", 0]]
        return _binance({symbol: 125.0 for symbol in nf.SYMBOLS.values()})(url, timeout)

    class Done(BaseException):
        pass

    def sleep(seconds):
        assert seconds == nf.PERIOD_S
        raise Done()

    monkeypatch.setattr(nf.threading, "Thread", lambda target, **kw: type("T", (), {
        "start": lambda self: None, "run": staticmethod(target)})())
    thread = nf.start_sampler(evm_address=None, state_path_fn=lambda *parts: str(tmp_path / parts[-1]),
                              get=get, sleep=sleep, clock=lambda: T0)
    with pytest.raises(Done):
        thread.run()
    assert len(history_calls) == (3 if gap_s > nf.PRICE_STALE_S else 0)
    snapshot = json.loads((tmp_path / "native_float.json").read_text())
    assert snapshot["price_ok"] is True
    if gap_s > nf.PRICE_STALE_S:
        for asset in snapshot["assets"].values():
            assert asset["price_usd"] == 125.0
            if history_ok:
                assert asset["change"]["1h"]["pct"] == 25.0
            else:
                assert asset["change"]["1h"] is None
            assert asset["change"]["7d"]["usd"] is None  # price backfill cannot invent held units


def test_the_first_sample_waits_for_the_solana_recorder_within_a_bound(tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_NATIVE_FLOAT_SAMPLER", True, raising=False)
    now, sleeps, sampled = [T0], [], []

    class Done(Exception):
        pass

    def sleep(s):
        sleeps.append(s)
        now[0] += s
        if len(sleeps) == 3:
            nf.remember("solana", "native", 2.0, now=now[0])     # the prefetch's first balance read
        if s == nf.PERIOD_S:
            raise Done

    monkeypatch.setattr(nf, "sample_once", lambda store, **kw: sampled.append(nf.recorded("solana", kw["now"])))
    monkeypatch.setattr(nf.threading, "Thread", lambda target, **kw: type("T", (), {"start": lambda self: None,
                                                                                   "run": staticmethod(target)})())
    thread = nf.start_sampler(evm_address=None, state_path_fn=lambda *p: str(tmp_path.joinpath(*p[-1:])),
                              get=_binance({"SOLUSDT": 100.0}), sleep=sleep, clock=lambda: now[0])
    with pytest.raises(Done):
        thread.run()
    assert sampled and sampled[0] is not None and sampled[0]["native"] == 2.0
    assert sum(s for s in sleeps if s != nf.PERIOD_S) <= nf.RECORDER_WAIT_S


def test_the_first_sample_does_not_wait_past_the_bound(tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_NATIVE_FLOAT_SAMPLER", True, raising=False)
    now, sleeps, sampled = [T0], [], []

    class Done(Exception):
        pass

    def sleep(s):
        sleeps.append(s)
        now[0] += s
        if s == nf.PERIOD_S:
            raise Done
    monkeypatch.setattr(nf, "sample_once", lambda store, **kw: sampled.append(kw["now"]))
    monkeypatch.setattr(nf.threading, "Thread", lambda target, **kw: type("T", (), {"start": lambda self: None,
                                                                                   "run": staticmethod(target)})())
    thread = nf.start_sampler(evm_address=None, state_path_fn=lambda *p: str(tmp_path.joinpath(*p[-1:])),
                              get=_binance({"SOLUSDT": 100.0}), sleep=sleep, clock=lambda: now[0])
    with pytest.raises(Done):
        thread.run()
    assert sampled == [T0 + nf.RECORDER_WAIT_S]          # no slot lane: the sample goes on, SOL unknown


# ------------------------------------------------------------------ web: read-only route

def _web(tmp_path):
    from test_memecoin_real_deploy_routes import _client
    cl, _c = _client({"native_float_json": str(tmp_path / "native_float.json")})
    return cl


def _snapshot(ts, *, price_ok=True):
    return {"ts": ts, "price_ok": price_ok, "partial": False, "headline": {"24h": {"usd": -4.1}},
            "assets": {"ETH": {"price_usd": 2400.0 if price_ok else None, "last_price_usd": 2400.0,
                               "last_price_age_s": 0.0, "units": 0.1, "value_usd": 240.0 if price_ok else None,
                               "chains": [], "change": {}, "sparkline": []}}}


def test_the_route_says_unavailable_when_the_sampler_has_written_nothing(tmp_path):
    got = _web(tmp_path).get("/api/memecoin/real/native-drift").get_json()
    assert got["available"] is False and got["stale"] is True


def test_a_fresh_snapshot_is_live(tmp_path, monkeypatch):
    nf.write_json_atomic(str(tmp_path / "native_float.json"), _snapshot(T0))
    monkeypatch.setattr(nf.time, "time", lambda: T0 + 30)
    got = _web(tmp_path).get("/api/memecoin/real/native-drift").get_json()
    assert got["available"] is True and got["stale"] is False and got["age_s"] == 30.0
    assert got["assets"]["ETH"]["price_usd"] == 2400.0


def test_a_snapshot_older_than_3_min_never_shows_a_live_price(tmp_path, monkeypatch):
    nf.write_json_atomic(str(tmp_path / "native_float.json"), _snapshot(T0))
    monkeypatch.setattr(nf.time, "time", lambda: T0 + 181)
    got = _web(tmp_path).get("/api/memecoin/real/native-drift").get_json()
    eth = got["assets"]["ETH"]
    assert got["stale"] is True and eth["price_usd"] is None and eth["value_usd"] is None
    assert eth["last_price_usd"] == 2400.0 and eth["last_price_age_s"] == 181.0
    assert got["headline"]["24h"] is None                                 # no old drift shown as today's


def test_a_failed_price_source_is_stale(tmp_path, monkeypatch):
    nf.write_json_atomic(str(tmp_path / "native_float.json"), _snapshot(T0, price_ok=False))
    monkeypatch.setattr(nf.time, "time", lambda: T0 + 5)
    assert _web(tmp_path).get("/api/memecoin/real/native-drift").get_json()["stale"] is True


def test_the_baseline_reset_route_is_gone(tmp_path):
    cl = _web(tmp_path)
    assert cl.post("/api/memecoin/real/native-drift/baseline", json={"confirm": True}).status_code in (404, 405)
    assert not hasattr(nf, "write_baseline") and not hasattr(nf, "read_baseline")


def test_a_long_window_is_downsampled_and_keeps_the_newest_point():
    pts = [(T0 + i * 60.0, 1000.0 + i) for i in range(1000)]
    out = nf._downsample(pts)
    assert len(out) <= nf.SPARK_POINTS + 1 and out[0] == [T0, 1000.0] and out[-1] == [T0 + 999 * 60.0, 1999.0]


def test_chain_1_falls_back_to_a_keyless_public_endpoint_never_a_keyed_one(monkeypatch):
    from sniper.services import memecoin_evm_holdings as H

    def no_public(chain):
        raise RuntimeError("no independent public read endpoint configured")
    monkeypatch.setattr(H, "_public_post", no_public)
    seen = []

    def fake_urlopen(req, timeout):
        seen.append((req.full_url, req.get_header("User-agent")))
        raise OSError("offline in tests")
    monkeypatch.setattr(nf.urllib.request, "urlopen", fake_urlopen)
    post = nf.public_post_for("1")
    assert post is not None and nf.public_balances("1", "0x" + "11" * 20, post=post) is None
    assert seen == [("https://ethereum-rpc.publicnode.com", "curl/8.5.0")]
    assert nf.public_post_for("8453") is None                                           # no fallback
    src = __import__("inspect").getsource(nf)
    assert "RESEARCH_DRPC" not in src and "MEMECOIN_EVM_RPC_URL_1" not in src


def test_solana_wsol_is_unknown_not_a_recorded_zero(tmp_path):
    nf.remember("solana", "native", 1.5, now=T0)
    got = nf.recorded("solana", now=T0 + 1)
    assert got == {"native": 1.5, "wrapped": None, "at": T0}
    _, snap = _sample(tmp_path, get=_binance({"SOLUSDT": 100.0}))
    sol = next(c for c in snap["assets"]["SOL"]["chains"] if c["chain"] == "solana")
    assert sol["wrapped"] is None and sol["units"] == 1.5
    assert snap["assets"]["SOL"]["value_usd"] == 150.0


def test_the_klines_purge_is_keyed_on_its_own_schema_version(tmp_path):
    # Live 2026-10-08: the store had observed_at (added by an earlier SF07 tip, or a reader that opened
    # it) and still 504 klines stamped close-at-open, because the purge rode the column migration.
    import sqlite3
    path = tmp_path / "native_float.db"
    db = sqlite3.connect(str(path))
    db.executescript("CREATE TABLE native_float_prices(ts REAL NOT NULL, asset TEXT NOT NULL, price_usd REAL NOT NULL,"
                     " source TEXT, PRIMARY KEY(asset, ts));"
                     "CREATE TABLE native_float_units(ts REAL NOT NULL, chain TEXT NOT NULL, asset TEXT NOT NULL,"
                     " native_units REAL, wrapped_units REAL, source TEXT, observed_at REAL, PRIMARY KEY(chain, ts));")
    db.executemany("INSERT INTO native_float_prices VALUES (?,?,?,?)",
                   [(T0 - 7200, "ETH", 2400.0, "klines"), (T0, "ETH", 2410.0, "ticker")])
    db.commit()
    db.close()
    store = nf.Store(str(path))
    assert store.prices_since("ETH", 0) == [(T0, 2410.0)]
    assert store.db.execute("PRAGMA user_version").fetchone()[0] >= 1
    store.add_prices(T0 - 3600, {"ETH": 2405.0}, "klines")
    assert nf.Store(str(path)).prices_since("ETH", 0) == [(T0 - 3600, 2405.0), (T0, 2410.0)]   # once only
