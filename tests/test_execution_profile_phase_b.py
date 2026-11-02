"""Tests for scripts/execution_profile.py — Phase B (per-fill helpers)."""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from scripts.execution_profile import (
    DRIFT_HORIZONS,
    IN_PLAY_THRESHOLD_SEC,
    PRECEDING_BAR_WINDOW_SEC,
    TAPE_CONFIDENCE_MIN,
    FillResult,
    _bar_at_offset,
    _parse_iso_to_ts,
    _preceding_bar,
    _probe_fill,
    _resolve_ts_from_sqlite,
    _resolve_ts_lookup,
    _tape_midpoint,
)


# ---------------------------------------------------------------------------
# _preceding_bar
# ---------------------------------------------------------------------------


def _hist(*pairs: tuple[int, float]) -> list[dict]:
    return [{"timestamp": ts, "price": p} for ts, p in pairs]


def test_preceding_bar_returns_last_strictly_earlier():
    h = _hist((100, 0.10), (150, 0.15), (200, 0.20), (250, 0.25))
    assert _preceding_bar(h, 200) == 0.15  # bar at 200 is not strictly earlier


def test_preceding_bar_handles_exact_equality():
    """A bar exactly at trade_ts is not 'strictly earlier'."""
    h = _hist((100, 0.10), (200, 0.20))
    assert _preceding_bar(h, 200) == 0.10


def test_preceding_bar_none_when_only_future_bars():
    h = _hist((300, 0.30), (400, 0.40))
    assert _preceding_bar(h, 200) is None


def test_preceding_bar_none_when_bars_outside_window():
    gap_beyond_window = PRECEDING_BAR_WINDOW_SEC + 100
    h = _hist((1000, 0.10))
    assert _preceding_bar(h, 1000 + gap_beyond_window) is None


def test_preceding_bar_includes_bar_at_window_floor():
    floor = PRECEDING_BAR_WINDOW_SEC
    h = _hist((1000, 0.42))
    assert _preceding_bar(h, 1000 + floor) == 0.42  # ts == trade_ts - 1800 is inclusive


def test_preceding_bar_excludes_bar_before_window_floor():
    h = _hist((1000, 0.42))
    # 1 second before floor
    assert _preceding_bar(h, 1000 + PRECEDING_BAR_WINDOW_SEC + 1) is None


# ---------------------------------------------------------------------------
# _bar_at_offset
# ---------------------------------------------------------------------------


def test_bar_at_offset_finds_nearest():
    h = _hist((3600, 0.50), (3700, 0.55), (3800, 0.60))
    # trade_ts=0, offset=3700 → target 3700, exact match
    assert _bar_at_offset(h, 0, 3700) == 0.55


def test_bar_at_offset_respects_tolerance():
    # 1h horizon → tolerance max(300, 3600//12=300) = 300
    h = _hist((3901, 0.55))  # 301s past target, outside tolerance
    assert _bar_at_offset(h, 0, 3600) is None
    h2 = _hist((3899, 0.55))  # 299s past, inside tolerance
    assert _bar_at_offset(h2, 0, 3600) == 0.55


def test_bar_at_offset_24h_has_wider_tolerance():
    # 24h horizon → tolerance max(300, 86400//12=7200) = 7200
    h = _hist((86400 + 5000, 0.88))
    assert _bar_at_offset(h, 0, 86400) == 0.88


def test_bar_at_offset_returns_none_on_empty_history():
    assert _bar_at_offset([], 0, 3600) is None


# ---------------------------------------------------------------------------
# _tape_midpoint
# ---------------------------------------------------------------------------


def test_tape_midpoint_excludes_own_wallet():
    client = MagicMock()
    client.get_trades_window.return_value = [
        {"proxyWallet": "0xSELF", "asset": "tok", "price": "0.50", "side": "BUY"},
        {"proxyWallet": "0xOTHER", "asset": "tok", "price": "0.60", "side": "BUY"},
        {"proxyWallet": "0xANOTHER", "asset": "tok", "price": "0.70", "side": "BUY"},
    ]
    mid, n = _tape_midpoint(client, "cid", "tok", 1000, "0xSELF")
    assert n == 2
    assert mid == pytest.approx((0.60 + 0.70) / 2)


def test_tape_midpoint_excludes_own_wallet_case_insensitive():
    client = MagicMock()
    client.get_trades_window.return_value = [
        {"proxyWallet": "0xABC123", "asset": "tok", "price": 0.50, "side": "BUY"},
    ]
    mid, n = _tape_midpoint(client, "cid", "tok", 1000, "0xabc123")
    assert (mid, n) == (None, 0)


def test_tape_midpoint_filters_other_assets():
    client = MagicMock()
    client.get_trades_window.return_value = [
        {"proxyWallet": "0xOTHER", "asset": "tokYES", "price": 0.3, "side": "BUY"},
        {"proxyWallet": "0xOTHER", "asset": "tokNO", "price": 0.7, "side": "BUY"},
    ]
    mid, n = _tape_midpoint(client, "cid", "tokYES", 1000, "0xSELF")
    assert n == 1
    assert mid == 0.3


def test_tape_midpoint_filters_sells():
    client = MagicMock()
    client.get_trades_window.return_value = [
        {"proxyWallet": "0xOTHER", "asset": "tok", "price": 0.3, "side": "SELL"},
        {"proxyWallet": "0xOTHER", "asset": "tok", "price": 0.4, "side": "BUY"},
    ]
    mid, n = _tape_midpoint(client, "cid", "tok", 1000, "0xSELF")
    assert n == 1
    assert mid == 0.4


def test_tape_midpoint_empty_tape_returns_none():
    client = MagicMock()
    client.get_trades_window.return_value = []
    mid, n = _tape_midpoint(client, "cid", "tok", 1000, "0xSELF")
    assert (mid, n) == (None, 0)


# ---------------------------------------------------------------------------
# _parse_iso_to_ts
# ---------------------------------------------------------------------------


def test_parse_iso_bare_date_end_of_day_utc():
    ts = _parse_iso_to_ts("2026-07-31")
    # 2026-07-31 23:59:59 UTC
    assert ts == 1785542399


def test_parse_iso_full_datetime_z_suffix():
    ts = _parse_iso_to_ts("2026-04-01T12:00:00Z")
    assert ts == 1775044800


def test_parse_iso_handles_naive_as_utc():
    ts = _parse_iso_to_ts("2026-04-01T12:00:00")
    assert ts == 1775044800


def test_parse_iso_none_on_garbage():
    assert _parse_iso_to_ts("not-a-date") is None
    assert _parse_iso_to_ts("") is None
    assert _parse_iso_to_ts(None) is None


# ---------------------------------------------------------------------------
# _resolve_ts_lookup — SQLite + gamma fallback
# ---------------------------------------------------------------------------


def test_resolve_ts_sqlite_hit(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE market_metadata (condition_id TEXT PRIMARY KEY, end_date_iso TEXT)")
    conn.execute(
        "INSERT INTO market_metadata VALUES ('0xCID1', '2026-07-31')"
    )
    conn.commit()
    conn.close()

    ts = _resolve_ts_from_sqlite("0xCID1", db_path=str(db))
    assert ts == 1785542399


def test_resolve_ts_sqlite_miss_returns_none(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE market_metadata (condition_id TEXT, end_date_iso TEXT)")
    conn.close()
    assert _resolve_ts_from_sqlite("0xMISSING", db_path=str(db)) is None


def test_resolve_ts_lookup_chain_uses_gamma_on_sqlite_miss(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE market_metadata (condition_id TEXT, end_date_iso TEXT)")
    conn.close()

    gamma_calls = []

    def fake_gamma(cid: str):
        gamma_calls.append(cid)
        return 9999

    ts = _resolve_ts_lookup("0xMISS", db_path=str(db), gamma_fallback=fake_gamma)
    assert ts == 9999
    assert gamma_calls == ["0xMISS"]


def test_resolve_ts_lookup_cache_amortises_repeated_calls(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE market_metadata (condition_id TEXT, end_date_iso TEXT)")
    conn.close()

    gamma_calls = []

    def fake_gamma(cid: str):
        gamma_calls.append(cid)
        return 7777

    cache: dict[str, int | None] = {}
    t1 = _resolve_ts_lookup("0xX", db_path=str(db), gamma_fallback=fake_gamma, cache=cache)
    t2 = _resolve_ts_lookup("0xX", db_path=str(db), gamma_fallback=fake_gamma, cache=cache)
    assert t1 == t2 == 7777
    assert gamma_calls == ["0xX"]  # second call hit the cache


def test_resolve_ts_lookup_returns_none_on_both_miss(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE market_metadata (condition_id TEXT, end_date_iso TEXT)")
    conn.close()
    ts = _resolve_ts_lookup("0xGONE", db_path=str(db), gamma_fallback=lambda c: None)
    assert ts is None


def test_resolve_ts_sqlite_resilient_to_missing_db():
    # No DB at a bogus path → return None instead of crashing.
    ts = _resolve_ts_from_sqlite("0xCID", db_path="/tmp/nonexistent/__no__.db")
    assert ts is None


# ---------------------------------------------------------------------------
# _probe_fill — integration of helpers
# ---------------------------------------------------------------------------


def test_probe_fill_in_play_flag_boundary_before_threshold():
    client = MagicMock()
    client.get_trades_window.return_value = []
    trade_ts = 1_000_000
    # 1h 59m before resolution → in_play
    resolve_ts = trade_ts + IN_PLAY_THRESHOLD_SEC - 60
    fr = _probe_fill(
        client, "0xW", trade_ts, "cid", "asset", "t", 0.5,
        history=[], resolve_ts=resolve_ts,
    )
    assert fr.in_play is True
    assert fr.resolution_ts_unknown is False


def test_probe_fill_in_play_flag_just_past_threshold():
    client = MagicMock()
    client.get_trades_window.return_value = []
    trade_ts = 1_000_000
    # 2h 01m before resolution → NOT in_play
    resolve_ts = trade_ts + IN_PLAY_THRESHOLD_SEC + 60
    fr = _probe_fill(
        client, "0xW", trade_ts, "cid", "asset", "t", 0.5,
        history=[], resolve_ts=resolve_ts,
    )
    assert fr.in_play is False


def test_probe_fill_resolution_unknown_is_not_in_play():
    """Spec §9.4: unknown resolve_ts → conservative, keeps fill (in_play=False)."""
    client = MagicMock()
    client.get_trades_window.return_value = []
    fr = _probe_fill(
        client, "0xW", 1_000_000, "cid", "asset", "t", 0.5,
        history=[], resolve_ts=None,
    )
    assert fr.in_play is False
    assert fr.resolution_ts_unknown is True


def test_probe_fill_thin_market_flag():
    client = MagicMock()
    client.get_trades_window.return_value = []  # zero other trades
    fr = _probe_fill(
        client, "0xW", 1_000, "cid", "asset", "t", 0.5,
        history=[], resolve_ts=10_000,
    )
    assert fr.thin_market is True
    assert fr.tape_n == 0
    assert fr.ref_tape is None


def test_probe_fill_tape_ge_threshold_is_not_thin():
    client = MagicMock()
    client.get_trades_window.return_value = [
        {"proxyWallet": "0xA", "asset": "asset", "price": 0.4, "side": "BUY"},
        {"proxyWallet": "0xB", "asset": "asset", "price": 0.5, "side": "BUY"},
        {"proxyWallet": "0xC", "asset": "asset", "price": 0.6, "side": "BUY"},
    ]
    fr = _probe_fill(
        client, "0xW", 1_000, "cid", "asset", "t", 0.5,
        history=[], resolve_ts=10_000_000,
    )
    assert fr.tape_n == 3
    assert fr.thin_market is False


def test_probe_fill_drift_truncated_by_adaptive_window():
    """Spec §6.2: drift horizon is clamped by time_to_resolution - 300s."""
    client = MagicMock()
    client.get_trades_window.return_value = []
    trade_ts = 1_000_000
    # Resolution 2h away → 1h drift ok (3600 < 7200-300=6900), 6h drift clamped,
    # 24h drift clamped.
    resolve_ts = trade_ts + 7200
    bars = _hist(
        (trade_ts + 3600, 0.55),   # 1h bar available
        (trade_ts + 21600, 0.60),  # 6h bar — post-resolution, would be clamped
    )
    fr = _probe_fill(
        client, "0xW", trade_ts, "cid", "asset", "t", 0.50,
        history=bars, resolve_ts=resolve_ts,
    )
    # 1h drift computed
    assert fr.drift_1h == pytest.approx(0.05)
    # 6h and 24h truncated by adaptive window
    assert fr.drift_6h is None
    assert fr.drift_24h is None


def test_probe_fill_preceding_bar_plumbed():
    client = MagicMock()
    client.get_trades_window.return_value = []
    trade_ts = 10_000
    bars = _hist((trade_ts - 60, 0.44))
    fr = _probe_fill(
        client, "0xW", trade_ts, "cid", "asset", "t", 0.45,
        history=bars, resolve_ts=trade_ts + 86400,
    )
    assert fr.ref_preceding == 0.44
    assert fr.fill_price == 0.45


# ---------------------------------------------------------------------------
# Arb probe fixture is loadable (Phase C will use it for replay)
# ---------------------------------------------------------------------------


def test_arb_probe_fixture_loads_and_has_expected_shape():
    import json
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "execution_profile" / "arb_probe.json"
    data = json.loads(fixture.read_text())
    assert len(data) == 1
    payload = list(data.values())[0]
    assert set(payload.keys()) == {"summary", "probes"}
    summary = payload["summary"]
    assert summary["n_trades"] == 40
    assert summary["skew_vs_preceding_bar"]["median"] == 0.005
    assert summary["skew_vs_preceding_bar"]["n"] == 40
    # Per-fill shape check — required for Phase C replay test
    probes = payload["probes"]
    assert len(probes) == 40
    required_keys = {
        "trade_ts", "condition_id", "asset", "title", "fill_price",
        "ref_bar_preceding", "ref_tape_5min", "tape_trade_count",
        "drift_1h", "drift_6h", "drift_24h",
    }
    for p in probes:
        assert required_keys.issubset(p.keys())
