"""Phase 5 — OHLC candles over the position-mark tape.

The gate for this phase is that position 11984's story is readable from its chart alone:
the single +50.489% tape mid tick, the +48.296% executable peak the rails actually saw, and
the −60% stop that closed it. So the payload has to carry three things at once — the price
tape, the fills, and the frozen contract's rail levels.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_candles import candles_payload, to_candles


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _mark(c, ts, mid, *, chain="4663", mint="0xt", source="dexscreener"):
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, "
              "executable_exit_value, liquidity, mcap, source, quality) "
              "VALUES(?,?,?,?,?,?,?,?,?)",
              (chain, mint, ts, mid, mid, 1000.0, 5e5, source, "live"))
    c.commit()


# ── bucketing ────────────────────────────────────────────────────────────────

def test_to_candles_buckets_by_interval():
    rows = [
        {"sweep_ts": "2026-07-21T05:00:05+00:00", "mid": 1.0, "source": "s"},
        {"sweep_ts": "2026-07-21T05:00:35+00:00", "mid": 3.0, "source": "s"},
        {"sweep_ts": "2026-07-21T05:00:55+00:00", "mid": 2.0, "source": "s"},
        {"sweep_ts": "2026-07-21T05:01:10+00:00", "mid": 4.0, "source": "s"},
    ]
    out = to_candles(rows, 60)
    assert len(out) == 2
    assert out[0]["t"] == "2026-07-21T05:00:00+00:00"
    assert (out[0]["o"], out[0]["h"], out[0]["l"], out[0]["c"], out[0]["n"]) == (1.0, 3.0, 1.0, 2.0, 3)
    assert (out[1]["o"], out[1]["c"], out[1]["n"]) == (4.0, 4.0, 1)


def test_to_candles_is_ordered_and_gap_tolerant():
    """A 10-minute hole in the tape is a hole, not an interpolation — 4663 gaps reach p99 380s."""
    rows = [{"sweep_ts": "2026-07-21T05:00:00+00:00", "mid": 1.0, "source": "s"},
            {"sweep_ts": "2026-07-21T05:10:00+00:00", "mid": 2.0, "source": "s"}]
    out = to_candles(rows, 60)
    assert [c["t"] for c in out] == ["2026-07-21T05:00:00+00:00", "2026-07-21T05:10:00+00:00"]


def test_to_candles_skips_unusable_rows():
    rows = [{"sweep_ts": "2026-07-21T05:00:00+00:00", "mid": None, "source": "s"},
            {"sweep_ts": None, "mid": 1.0, "source": "s"},
            {"sweep_ts": "2026-07-21T05:00:10+00:00", "mid": 0, "source": "s"},
            {"sweep_ts": "2026-07-21T05:00:20+00:00", "mid": 2.0, "source": "s"}]
    out = to_candles(rows, 60)
    assert len(out) == 1 and out[0]["c"] == 2.0


def test_candle_carries_its_dominant_source():
    """Old-cadence (GT/dexscreener) and new-cadence (fastlane executable) eras must stay
    distinguishable on the chart — the 4663 slippage convention already changed once."""
    rows = [{"sweep_ts": "2026-07-21T05:00:00+00:00", "mid": 1.0, "source": "fastlane_rpc"},
            {"sweep_ts": "2026-07-21T05:00:10+00:00", "mid": 2.0, "source": "fastlane_rpc"},
            {"sweep_ts": "2026-07-21T05:00:20+00:00", "mid": 3.0, "source": "dexscreener"}]
    assert to_candles(rows, 60)[0]["src"] == "fastlane_rpc"


# ── payload ──────────────────────────────────────────────────────────────────

def test_payload_joins_tape_fills_and_rails():
    c = _c()
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="0xt", ticker="T",
                          entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-07-21T05:00:00+00:00", strategy="alpha_b",
                          execution_mode="real", network_id="4663")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=50.0, notional=50.0,
                  fill_source="chain", fraction=1.0, created_at="2026-07-21T05:00:00+00:00")
    c.execute("UPDATE memecoin_positions SET funded_contract_json=?, executable_peak_price=?, "
              "peak_price=? WHERE id=?",
              ('{"policy": {"policy_id": "funded-micro-v1", "derisk_pct": 50.0, '
               '"trail_pct": 50.0, "stop_pct": 60.0, "catastrophe_pct": 80.0}}',
               1.48296, 1.50489, pid))
    c.commit()
    # A funded position's chart draws only our own marks (2026-10-08): the exact series here.
    _mark(c, "2026-07-21T05:00:10+00:00", 1.2, source="fastlane_rpc")
    _mark(c, "2026-07-21T05:01:10+00:00", 1.50489, source="fastlane_rpc")

    p = candles_payload(c, chain_id="4663", mint="0xt", interval_s=60, position_id=pid)
    assert len(p["exec_candles"]) == 2 and p["candles"] == []
    assert [f["kind"] for f in p["fills"]] == ["entry"]
    rails = p["rails"]
    assert rails["entry_price"] == 1.0
    assert rails["stop_pct"] == 60.0 and rails["derisk_pct"] == 50.0
    # the two numbers whose 2.2-point gap is the whole 11984 story
    assert rails["executable_peak_price"] == 1.48296
    assert rails["peak_price"] == 1.50489
    # rails rendered as absolute price levels so the chart can draw horizontal lines
    assert p["levels"]["stop"] == pytest.approx(0.4)      # entry * (1 - 60/100)
    assert p["levels"]["derisk"] == pytest.approx(1.5)    # entry * (1 + 50/100)


def test_position_view_defaults_to_the_hold_window():
    """A chart that draws ticks from outside the hold window makes unreachable rails look
    reachable. On the real 11984 the full-life tape highs +283.7% while the position only ever
    saw +48.3% — enough to suggest its +50% de-risk was hit when it never was."""
    c = _c()
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="0xt", ticker="T",
                          entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-07-21T05:00:00+00:00", strategy="alpha_b",
                          execution_mode="real", network_id="4663")
    m.claim_close(c, pid)
    m.finalize_close(c, pid, "2026-07-21T05:10:00+00:00", -1.0, exit_reason="b_stop")
    _mark(c, "2026-07-21T04:00:00+00:00", 9.0, source="fastlane_rpc")      # long before entry
    _mark(c, "2026-07-21T05:05:00+00:00", 1.2, source="fastlane_rpc")      # inside the hold
    _mark(c, "2026-07-21T06:00:00+00:00", 8.0, source="fastlane_rpc")      # long after exit

    p = candles_payload(c, chain_id="4663", mint="0xt", interval_s=60, position_id=pid)
    assert max(cd["h"] for cd in p["exec_candles"]) == 1.2
    assert p["window"]["from"] and p["window"]["to"]

    wide = candles_payload(c, chain_id="4663", mint="0xt", interval_s=60, position_id=pid,
                           since="2026-07-21T00:00:00+00:00")
    assert max(cd["h"] for cd in wide["exec_candles"]) == 9.0   # explicit since overrides


def test_open_position_view_has_no_upper_bound():
    c = _c()
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="0xt", ticker="T",
                          entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-07-21T05:00:00+00:00", strategy="alpha_b",
                          execution_mode="real", network_id="4663")
    _mark(c, "2026-07-21T05:05:00+00:00", 1.2, source="fastlane_rpc")
    _mark(c, "2026-07-21T09:00:00+00:00", 3.0, source="fastlane_rpc")
    p = candles_payload(c, chain_id="4663", mint="0xt", interval_s=60, position_id=pid)
    assert max(cd["h"] for cd in p["exec_candles"]) == 3.0
    assert p["window"]["to"] is None


def test_payload_without_a_position_is_tape_only():
    c = _c()
    _mark(c, "2026-07-21T05:00:10+00:00", 1.2)
    p = candles_payload(c, chain_id="4663", mint="0xt", interval_s=60)
    assert len(p["candles"]) == 1
    assert p["fills"] == [] and p["rails"] == {} and p["levels"] == {}


def test_payload_is_keyed_on_network_and_mint():
    c = _c()
    _mark(c, "2026-07-21T05:00:10+00:00", 1.0, chain="4663", mint="SAME")
    _mark(c, "2026-07-21T05:00:20+00:00", 9.0, chain="solana", mint="SAME")
    p = candles_payload(c, chain_id="4663", mint="SAME", interval_s=60)
    assert p["candles"][0]["c"] == 1.0


def test_payload_respects_since_and_caps_rows():
    c = _c()
    for i in range(10):
        _mark(c, f"2026-07-21T05:{i:02d}:00+00:00", float(i + 1))
    p = candles_payload(c, chain_id="4663", mint="0xt", interval_s=60,
                        since="2026-07-21T05:07:00+00:00")
    assert [cd["c"] for cd in p["candles"]] == [8.0, 9.0, 10.0]
    capped = candles_payload(c, chain_id="4663", mint="0xt", interval_s=60, max_rows=4)
    assert len(capped["candles"]) == 4


# ── review 2026-07-22: three defects ─────────────────────────────────────────

def test_mid_and_executable_are_separate_series():
    """Executable and mid must never share a candle. Merging them and labelling the bar with
    the dominant source hides exactly the divergence the chart exists to show — 11984's
    +50.489% mid vs +48.296% executable. Two series, or the phase's premise is void."""
    c = _c()
    _mark(c, "2026-07-21T05:00:05+00:00", 1.0, source="dexscreener")
    _mark(c, "2026-07-21T05:00:15+00:00", 2.0, source="fastlane_rpc")
    _mark(c, "2026-07-21T05:00:25+00:00", 1.1, source="geckoterminal")
    p = candles_payload(c, chain_id="4663", mint="0xt", interval_s=60)
    assert [cd["c"] for cd in p["candles"]] == [1.1]          # mid series: GT + DS only
    assert [cd["c"] for cd in p["exec_candles"]] == [2.0]     # executable series: fastlane only


def test_executable_series_is_empty_without_fastlane_marks():
    c = _c()
    _mark(c, "2026-07-21T05:00:05+00:00", 1.0, source="dexscreener")
    p = candles_payload(c, chain_id="4663", mint="0xt", interval_s=60)
    assert len(p["candles"]) == 1 and p["exec_candles"] == []


def test_position_id_must_match_the_requested_coin():
    """The endpoint could otherwise render coin A's fills and rails over coin B's candles."""
    c = _c()
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="0xA", ticker="A",
                          entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-07-21T05:00:00+00:00", strategy="alpha_b",
                          execution_mode="real", network_id="4663")
    _mark(c, "2026-07-21T05:00:10+00:00", 5.0, mint="0xB")
    with pytest.raises(ValueError):
        candles_payload(c, chain_id="4663", mint="0xB", interval_s=60, position_id=pid)


def test_position_id_mismatch_on_chain_is_rejected_too():
    c = _c()
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="0xA", ticker="A",
                          entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-07-21T05:00:00+00:00", strategy="alpha_b",
                          execution_mode="real", network_id="4663")
    with pytest.raises(ValueError):
        candles_payload(c, chain_id="8453", mint="0xA", interval_s=60, position_id=pid)


def test_operator_override_rails_are_reported_alongside_the_frozen_contract():
    """Position 9380 froze stop_pct=0 (4663 chain default) but carried an ACTIVE operator
    sl_pct=-60 and exited b_stop. Drawing only the frozen contract shows no stop on a
    position that stopped out."""
    c = _c()
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="0xt", ticker="T",
                          entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-07-21T05:00:00+00:00", strategy="alpha_b",
                          execution_mode="real", network_id="4663")
    c.execute("UPDATE memecoin_positions SET funded_contract_json=? WHERE id=?",
              ('{"policy": {"policy_id": "funded-small-v1", "stop_pct": 0.0, '
               '"catastrophe_pct": 80.0}}', pid))
    m.set_position_exit_override(c, pid, "operator", sl_pct=-60.0)
    c.commit()
    _mark(c, "2026-07-21T05:00:10+00:00", 0.9)
    p = candles_payload(c, chain_id="4663", mint="0xt", interval_s=60, position_id=pid)
    assert p["rails"]["stop_pct"] == 0.0                   # frozen contract, unchanged
    assert p["override"]["sl_pct"] == -60.0                # what actually governed the exit
    assert p["levels"].get("stop") is None                 # frozen rail is genuinely absent
    assert p["override_levels"]["stop"] == pytest.approx(0.4)
