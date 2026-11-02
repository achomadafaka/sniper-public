"""Phase 1B Deliverable #4 — Binance observation adapter + persistence.

Binance is a PREDICTOR feature source only (never settlement truth, never the label). The RTDS Binance
feed carries price ONLY (symbol/timestamp/value) — volume/VWAP fields stay None and are NEVER fabricated
from price. Direct aggTrade / 1s-kline streams DO carry volume, so those parsers populate it.
"""
import os

from sniper.algos.polymarket_crypto5m import binance_feed as BF
from sniper.algos.polymarket_crypto5m import persistence as P


# ---- parsers ----------------------------------------------------------------

def test_parse_rtds_price_is_price_only_no_volume():
    obs = BF.parse_rtds_price({"symbol": "btcusdt", "timestamp": 1781947500000, "value": 63456.1},
                              recv_ts=1781947500)
    assert obs.price == 63456.1
    assert obs.symbol == "BTCUSDT"
    assert obs.delivery_source == BF.RTDS_PRICE
    assert obs.quantity is None and obs.quote_quantity is None and obs.trade_count is None  # never faked
    assert obs.dedup_key == "BTCUSDT|1781947500000|rtds_price"


def test_parse_rtds_price_garbage_returns_none():
    assert BF.parse_rtds_price(None, recv_ts=1) is None
    assert BF.parse_rtds_price({"symbol": "BTCUSDT"}, recv_ts=1) is None        # no price/ts


def test_parse_agg_trade_has_price_and_volume():
    msg = {"e": "aggTrade", "s": "BTCUSDT", "p": "63456.10", "q": "0.5", "T": 1781947500123,
           "f": 100, "l": 104}
    obs = BF.parse_agg_trade(msg, recv_ts=1781947500)
    assert obs.price == 63456.10
    assert obs.quantity == 0.5
    assert obs.quote_quantity == 63456.10 * 0.5
    assert obs.trade_count == 5                          # l - f + 1
    assert obs.delivery_source == BF.AGG_TRADE
    assert obs.source_event_ts == 1781947500123


def test_parse_kline_1s_has_close_volume_quote_trades():
    msg = {"e": "kline", "s": "BTCUSDT",
           "k": {"t": 1781947500000, "T": 1781947500999, "c": "63460.0", "v": "12.5", "q": "793250.0",
                 "n": 42, "x": True}}
    obs = BF.parse_kline_1s(msg, recv_ts=1781947501)
    assert obs.price == 63460.0
    assert obs.quantity == 12.5
    assert obs.quote_quantity == 793250.0
    assert obs.trade_count == 42
    assert obs.delivery_source == BF.KLINE_1S
    assert obs.source_event_ts == 1781947500999          # kline close time


def test_parse_kline_missing_close_returns_none():
    assert BF.parse_kline_1s({"e": "kline", "s": "BTCUSDT", "k": {"T": 1}}, recv_ts=1) is None


# ---- persistence ------------------------------------------------------------

def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "b.sqlite"))
    P.ensure_schema(conn)
    return conn


def test_binance_observation_persist_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    obs = BF.parse_agg_trade({"s": "BTCUSDT", "p": "63456.1", "q": "0.5", "T": 1781947500123,
                              "f": 1, "l": 3}, recv_ts=1781947500)
    P.record_binance_observation(conn, obs, now_ts=1781947501)
    row = conn.execute("SELECT symbol, source_event_ts, price, quantity, quote_quantity, trade_count, "
                       "delivery_source FROM crypto5m_binance_observations").fetchone()
    assert row[0] == "BTCUSDT" and row[2] == 63456.1 and row[3] == 0.5
    assert row[6] == "binance_aggtrade"


def test_binance_observation_idempotent_on_dedup_key(tmp_path):
    conn = _conn(tmp_path)
    obs = BF.parse_rtds_price({"symbol": "BTCUSDT", "timestamp": 1781947500000, "value": 63456.1},
                              recv_ts=1781947500)
    P.record_binance_observation(conn, obs, now_ts=1)
    P.record_binance_observation(conn, obs, now_ts=2)                # same dedup_key
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_binance_observations").fetchone()[0] == 1


def test_aggtrade_dedup_key_disambiguates_same_ms_trades():
    a = BF.parse_agg_trade({"s": "BTCUSDT", "p": "100", "q": "1", "T": 1000, "f": 10, "l": 12}, recv_ts=1)
    b = BF.parse_agg_trade({"s": "BTCUSDT", "p": "100", "q": "2", "T": 1000, "f": 13, "l": 15}, recv_ts=1)
    assert a.dedup_key != b.dedup_key          # same ms, distinct trades -> distinct keys


def test_both_same_ms_aggtrades_persist(tmp_path):
    conn = _conn(tmp_path)
    for m in ({"s": "BTCUSDT", "p": "100", "q": "1", "T": 1000, "f": 10, "l": 12},
              {"s": "BTCUSDT", "p": "100", "q": "2", "T": 1000, "f": 13, "l": 15}):
        P.record_binance_observation(conn, BF.parse_agg_trade(m, recv_ts=1), now_ts=1)
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_binance_observations").fetchone()[0] == 2


def test_partial_kline_rejected_final_accepted():
    assert BF.parse_kline_1s({"s": "BTCUSDT", "k": {"T": 1, "c": "100", "x": False}}, recv_ts=1) is None
    assert BF.parse_kline_1s({"s": "BTCUSDT", "k": {"T": 1, "c": "100", "x": True}}, recv_ts=1) is not None


def test_kline_without_closed_flag_rejected():
    assert BF.parse_kline_1s({"s": "BTCUSDT", "k": {"T": 1, "c": "100"}}, recv_ts=1) is None  # no x


def test_garbage_numeric_fields_fail_closed():
    assert BF.parse_rtds_price({"symbol": "BTCUSDT", "timestamp": "notanumber", "value": 1.0},
                               recv_ts=1) is None
    assert BF.parse_agg_trade({"s": "BTCUSDT", "p": "1", "q": "1", "T": "x"}, recv_ts=1) is None


def test_rtds_observation_persists_null_volume(tmp_path):
    conn = _conn(tmp_path)
    obs = BF.parse_rtds_price({"symbol": "BTCUSDT", "timestamp": 1781947500000, "value": 1.0},
                              recv_ts=1)
    P.record_binance_observation(conn, obs, now_ts=1)
    row = conn.execute("SELECT quantity, quote_quantity, trade_count FROM "
                       "crypto5m_binance_observations").fetchone()
    assert tuple(row) == (None, None, None)             # price-only source stays null, never fabricated
