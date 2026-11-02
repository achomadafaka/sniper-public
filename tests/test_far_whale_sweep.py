"""§4 sweep — episode-unit counts per asset x threshold; freeze writes the report."""
import importlib.util
import sys
from pathlib import Path

from sniper.hyperlive import far_whale_store as S

_spec = importlib.util.spec_from_file_location(
    "far_whale_threshold_sweep",
    Path(__file__).resolve().parent.parent / "scripts" / "far_whale_threshold_sweep.py")
sweep = importlib.util.module_from_spec(_spec)
sys.modules["far_whale_threshold_sweep"] = sweep
_spec.loader.exec_module(sweep)

DAY = 86_400_000


def _snap(conn, ts, addr, notional, liq=2950.0):
    S.save_snapshots(conn, ts, [{"address": addr, "asset": "ETH",
                                 "liquidation_price": liq,
                                 "position_notional": notional, "szi": 100.0}])


def test_sweep_counts_episodes_not_rows():
    conn = S.connect(":memory:")
    S.ensure_schema(conn)
    # one whale seen 10 consecutive rounds = ONE episode-unit, not ten
    for i in range(10):
        _snap(conn, 1_000_000 + i * 180_000, "0xa", 2_000_000.0)
    # another whale, below the 1M threshold
    _snap(conn, 1_000_000, "0xb", 400_000.0)
    out = sweep.sweep_counts(conn, thresholds=[250_000.0, 1_000_000.0],
                             since_ms=0, until_ms=DAY)
    assert out["ETH"][1_000_000.0]["distinct_whales"] == 1
    assert out["ETH"][1_000_000.0]["episodes_per_day"] == 1.0
    assert out["ETH"][250_000.0]["distinct_whales"] == 2


def test_gap_splits_episode_units():
    conn = S.connect(":memory:")
    S.ensure_schema(conn)
    _snap(conn, 1_000_000, "0xa", 2_000_000.0)
    _snap(conn, 1_000_000 + 3_600_001 + 180_000, "0xa", 2_000_000.0)  # > 60min gap
    out = sweep.sweep_counts(conn, thresholds=[1_000_000.0], since_ms=0, until_ms=DAY)
    assert out["ETH"][1_000_000.0]["episodes_per_day"] == 2.0


def test_freeze_writes_report():
    conn = S.connect(":memory:")
    S.ensure_schema(conn)
    sweep.freeze(conn, {"ETH": 1_000_000.0, "SOL": 250_000.0}, now_ms=42_000)
    fz = S.load_freeze(conn)
    assert fz["thresholds"] == {"ETH": 1_000_000.0, "SOL": 250_000.0}
    assert fz["campaign_start_ts_ms"] == 42_000


def test_freeze_refuses_when_already_frozen():
    conn = S.connect(":memory:")
    S.ensure_schema(conn)
    sweep.freeze(conn, {"ETH": 1_000_000.0}, now_ms=1_000)
    import pytest
    with pytest.raises(sweep.AlreadyFrozenError):
        sweep.freeze(conn, {"ETH": 2_000_000.0}, now_ms=2_000)
    # original freeze must be untouched
    fz = S.load_freeze(conn)
    assert fz["thresholds"] == {"ETH": 1_000_000.0}
    assert fz["campaign_start_ts_ms"] == 1_000
