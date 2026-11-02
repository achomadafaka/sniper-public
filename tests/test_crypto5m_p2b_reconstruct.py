"""Weakness #7 — reconstruct Price-to-Beat candidates from captured Chainlink ticks and persist them
(with oracle comparison when known), so the boundary-rule calibration can eventually run."""
import os

from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m import price_to_beat_resolver as R
from sniper.algos.polymarket_crypto5m.models import ChainlinkTick


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "c.sqlite"))
    P.ensure_schema(conn)
    return conn


def _tick(ws, off_ms, val):
    return ChainlinkTick(symbol="BTC/USD", source_ts_ms=ws * 1000 + off_ms, server_ts_ms=None,
                         recv_ts=ws, value=val, full_accuracy_value=None, is_snapshot=False)


def test_reconstruct_window_records_and_matches_oracle(tmp_path):
    conn = _conn(tmp_path)
    ws = 1781980000
    for off_ms, val in [(-2000, 63000.0), (-100, 63010.0), (500, 63020.0), (3000, 63030.0)]:
        P.append_chainlink_tick(conn, _tick(ws, off_ms, val), window_start_ts=ws)
    cmp = R.reconstruct_window(conn, condition_id="0xabc", window_start_ts=ws, oracle=63010.0,
                               code_version="test", now_ts=1)
    rows = {r["candidate_rule"]: r for r in conn.execute(
        "SELECT candidate_rule, value, gamma_oracle, match FROM crypto5m_price_to_beat "
        "WHERE condition_id='0xabc'")}
    assert set(rows) >= {"last_at_or_before", "first_at_or_after", "nearest"}
    lab = rows["last_at_or_before"]                       # last tick at/before window start = 63010
    assert float(lab["value"]) == 63010.0 and lab["match"] == 1 and lab["gamma_oracle"] == 63010.0
    assert cmp["first_at_or_after"]["match"] is False     # 63020 != 63010


def test_reconstruct_window_no_ticks_returns_empty(tmp_path):
    conn = _conn(tmp_path)
    assert R.reconstruct_window(conn, condition_id="0x1", window_start_ts=999, oracle=1.0,
                                code_version="t", now_ts=1) == {}


def test_reconstruct_window_without_oracle_still_records_candidates(tmp_path):
    conn = _conn(tmp_path)
    ws = 1781980300
    P.append_chainlink_tick(conn, _tick(ws, -50, 100.0), window_start_ts=ws)
    cmp = R.reconstruct_window(conn, condition_id="0x2", window_start_ts=ws, oracle=None,
                               code_version="t", now_ts=1)
    assert cmp == {}
    n = conn.execute("SELECT COUNT(*) FROM crypto5m_price_to_beat WHERE condition_id='0x2'").fetchone()[0]
    assert n >= 1
