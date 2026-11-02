import importlib.util
import os

spec = importlib.util.spec_from_file_location(
    "run_cascade_capture",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "run_cascade_capture.py"))
rcc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rcc)


def test_aggregate_buckets_counts_and_notional_in_window():
    trades = [
        {"coin": "BTC", "px": "100", "sz": "2", "time": 9_000},     # in window [0,10000)
        {"coin": "BTC", "px": "101", "sz": "1", "time": 9_500},
        {"coin": "ETH", "px": "50",  "sz": "4", "time": 8_000},
        {"coin": "BTC", "px": "100", "sz": "5", "time": 20_000},    # outside window (>= now)
        {"coin": "BTC", "px": "100", "sz": "5", "time": -1},        # before window
    ]
    out = rcc.aggregate_buckets(trades, 10_000, now_ms=10_000)
    assert out["BTC"] == (2, 100 * 2 + 101 * 1)
    assert out["ETH"] == (1, 50 * 4)
    assert out["BTC"][0] == 2          # the 20000 and -1 trades excluded


def test_aggregate_buckets_ignores_malformed():
    trades = [
        {"coin": "BTC", "px": "x", "sz": "2", "time": 5_000},       # bad px -> skipped
        {"coin": None, "px": "1", "sz": "1", "time": 5_000},        # no coin -> skipped
        {"coin": "BTC", "px": "10", "sz": "3", "time": 5_000},      # good
    ]
    out = rcc.aggregate_buckets(trades, 10_000, now_ms=10_000)
    assert out == {"BTC": (1, 30.0)}


def test_extract_trade_updates_handles_wrapped_trade_frame():
    msg = {
        "channel": "trades",
        "data": {
            "coin": "BTC",
            "trades": [
                {"px": "100", "sz": "2", "time": 9_000},
                {"coin": "ETH", "px": "50", "sz": "1", "time": 9_100},
            ],
        },
    }

    out = rcc.extract_trade_updates(msg)

    assert out[0]["coin"] == "BTC"
    assert out[1]["coin"] == "ETH"
    assert rcc.aggregate_buckets(out, 10_000, now_ms=10_000) == {
        "BTC": (1, 200.0),
        "ETH": (1, 50.0),
    }


def test_extract_trade_updates_handles_official_trade_list_frame():
    msg = {
        "channel": "trades",
        "data": [{"coin": "BTC", "px": "100", "sz": "2", "time": 9_000}],
    }

    assert rcc.extract_trade_updates(msg) == msg["data"]


def test_aggregate_buckets_normalizes_coin_case():
    trades = [{"coin": "kPEPE", "px": "2", "sz": "3", "time": 9_000}]

    assert rcc.aggregate_buckets(trades, 10_000, now_ms=10_000) == {"KPEPE": (1, 6.0)}


def test_bucket_boundary_floors():
    assert rcc.bucket_boundary(10_000, 10_000) == 10_000
    assert rcc.bucket_boundary(19_999, 10_000) == 10_000
    assert rcc.bucket_boundary(20_001, 10_000) == 20_000


def test_update_clock_offset_converges_to_min():
    # host clock +7h ahead: recv_ms = trade.time + true_offset + jitter; min() -> true_offset
    true_offset = 7 * 3600 * 1000
    trades = [{"time": 1_000_000, "coin": "BTC"}]
    off = rcc.update_clock_offset(None, trades, 1_000_000 + true_offset + 900)   # +900ms jitter
    assert off == true_offset + 900
    off = rcc.update_clock_offset(off, trades, 1_000_000 + true_offset + 40)     # smaller jitter wins
    assert off == true_offset + 40
    assert rcc.update_clock_offset(off, [{"time": 0}], 999) == off               # time=0 ignored


def test_resolve_bucket_uses_exchange_time_not_host_clock():
    bucket_ms = 10_000
    true_offset = 7 * 3600 * 1000 + 123
    ex_trade_time = 1_000_000_000                      # real/exchange time of a trade
    local_now = ex_trade_time + true_offset + 4_000    # 4s later on the skewed host clock
    # offset measured from that same trade
    offset = rcc.update_clock_offset(None, [{"time": ex_trade_time, "coin": "BTC"}], local_now)
    ex_flush = rcc.resolve_bucket(local_now, offset, bucket_ms, last_ex_flush=None)
    # the just-closed exchange bucket must contain the real trade time (not be ~7h ahead of it)
    assert ex_flush is not None
    assert ex_flush - bucket_ms <= ex_trade_time < ex_flush + bucket_ms
    trades = [{"coin": "BTC", "px": "100", "sz": "2", "time": ex_trade_time}]
    agg = rcc.aggregate_buckets(trades, bucket_ms, now_ms=rcc.bucket_boundary(ex_trade_time, bucket_ms) + bucket_ms)
    assert agg["BTC"] == (1, 200.0)                    # trade is counted, not dropped


def test_resolve_bucket_none_when_offset_unknown_or_stale():
    assert rcc.resolve_bucket(1_000_000, None, 10_000, None) is None       # no trade seen yet
    off = 7 * 3600 * 1000
    ex = rcc.resolve_bucket(1_000_000 + off, off, 10_000, None)
    assert rcc.resolve_bucket(1_000_000 + off, off, 10_000, ex) is None     # same bucket -> no re-emit


def test_next_backoff_exponential_capped():
    assert rcc.next_backoff(1) == 2
    assert rcc.next_backoff(2) == 4
    assert rcc.next_backoff(0) == 2                       # floors to 1, then doubles
    assert rcc.next_backoff(40) == rcc.BACKOFF_CAP_S      # capped
    assert rcc.next_backoff(100) == rcc.BACKOFF_CAP_S


def test_feed_stalled_threshold():
    assert rcc.feed_stalled(1_000_000, 1_000_000 - rcc.STALL_MS) is True
    assert rcc.feed_stalled(1_000_000, 1_000_000 - rcc.STALL_MS + 1) is False
    assert rcc.feed_stalled(1_000_000, 1_000_000) is False
