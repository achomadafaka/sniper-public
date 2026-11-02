"""Phase 2B — pure parsers for extra price/vol sources (Coinbase/Kraken/Deribit/DVOL) and DVOL->sigma."""
import math

from sniper.algos.polymarket_crypto5m import quant_sources as QS


def test_parse_coinbase():
    assert QS.parse_coinbase_ticker({"price": "63778.34"}) == 63778.34
    assert QS.parse_coinbase_ticker({}) is None


def test_parse_kraken():
    resp = {"result": {"XXBTZUSD": {"c": ["63778.1", "0.01"]}}}
    assert QS.parse_kraken_ticker(resp) == 63778.1
    assert QS.parse_kraken_ticker({"result": {}}) is None
    assert QS.parse_kraken_ticker({"error": ["bad"]}) is None


def test_parse_deribit_ticker_and_index():
    assert QS.parse_deribit_ticker({"result": {"last_price": 63797.5}}) == 63797.5
    assert QS.parse_deribit_ticker({"result": {"mark_price": 63800.0}}) == 63800.0  # fallback
    assert QS.parse_deribit_index({"result": {"index_price": 63787.56}}) == 63787.56
    assert QS.parse_deribit_index({"result": {}}) is None


def test_parse_dvol_latest_close():
    resp = {"result": {"data": [[1, 40, 41, 39, 40.5], [2, 41, 43, 40, 42.0]]}}
    assert QS.parse_dvol(resp) == 42.0
    assert QS.parse_dvol({"result": {"data": []}}) is None


def test_dvol_to_vol_per_s():
    # DVOL is annualized vol in %; per-second sigma = (dvol/100)/sqrt(seconds_per_year)
    v = QS.dvol_to_vol_per_s(60.0)
    assert v is not None
    assert abs(v - (0.60 / math.sqrt(365 * 86400))) < 1e-12
    assert QS.dvol_to_vol_per_s(None) is None
    assert QS.dvol_to_vol_per_s(0) is None
