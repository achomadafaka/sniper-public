"""Slice 4 Task 1 — per-fill probe iterator extraction.

Tests `scripts.execution_profile.iter_fill_probes` and the
`FillProbeRecord` / `FillProbeHorizon` dataclasses, plus the byte-for-byte
aggregate parity contract between pre-refactor and post-refactor
`compute_execution_profile()` output.

The parity baseline was captured against the legacy implementation prior to
the refactor and lives at `tests/fixtures/execution_profile/aggregate_baseline_v1.json`.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import is_dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.execution_profile import (
    DEFAULT_PROBE_CONFIG,
    FillProbeHorizon,
    FillProbeRecord,
    _preceding_bar,
    _probe_config_hash,
    compute_execution_profile,
    iter_fill_probes,
)

BASELINE_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "execution_profile"
    / "aggregate_baseline_v1.json"
)


# ---------------------------------------------------------------------------
# Deterministic 30-fill fixture matching the parity baseline capture.
# ---------------------------------------------------------------------------


def _baseline_inputs():
    base = 1_000_000
    bars = [
        {"timestamp": base + dt, "price": 0.50 + (0.02 if dt >= 3600 else 0.0)}
        for dt in range(-60, 3600 + 1800, 60)
    ]
    client = MagicMock()
    client.get_prices_series.return_value = bars
    client.get_trades_window.return_value = [
        {"proxyWallet": "0xOTHER", "asset": "asset", "price": 0.50, "side": "BUY"}
    ] * 5
    activity = [
        {
            "type": "TRADE",
            "side": "BUY",
            "conditionId": "cid",
            "asset": "asset",
            "timestamp": base + i * 60,
            "price": 0.50,
            "title": "Fake Market",
        }
        for i in range(30)
    ]
    return client, activity, base, bars


# ---------------------------------------------------------------------------
# DEFAULT_PROBE_CONFIG locked content
# ---------------------------------------------------------------------------


def test_default_probe_config_uses_preceding_bar_30min_lookback():
    cfg = DEFAULT_PROBE_CONFIG
    assert cfg["reference_rule"] == "preceding_bar_30min_lookback"
    assert cfg["reference_lookback_seconds"] == 1800
    assert "reference_offset_seconds" not in cfg
    assert "reference_match_window_seconds" not in cfg
    assert cfg["probe_version"] == "v1"
    assert cfg["horizons_seconds"] == [3600, 21600, 86400]
    assert cfg["min_history_seconds"] == 300
    assert cfg["tolerance_rule"] == "max(300, offset_sec // 12)"
    assert cfg["fidelity_minutes"] == 1
    assert cfg["price_source_version"] == "batch-prices-history-v1"


def test_probe_config_hash_is_canonical_json_sha256():
    expected = hashlib.sha256(
        json.dumps(
            DEFAULT_PROBE_CONFIG, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    assert _probe_config_hash(DEFAULT_PROBE_CONFIG) == expected


def test_probe_config_hash_changes_when_price_source_version_changes():
    a = dict(DEFAULT_PROBE_CONFIG)
    b = dict(DEFAULT_PROBE_CONFIG)
    b["price_source_version"] = "batch-prices-history-v2"
    assert _probe_config_hash(a) != _probe_config_hash(b)


# ---------------------------------------------------------------------------
# iter_fill_probes — per-fill record contract
# ---------------------------------------------------------------------------


def test_fill_probe_record_is_a_frozen_dataclass():
    assert is_dataclass(FillProbeRecord)
    assert is_dataclass(FillProbeHorizon)
    # Frozen by attempted mutation check
    horizon = FillProbeHorizon(
        horizon_seconds=3600,
        reference_price=0.5,
        reference_ts=1000,
        target_ts=4600,
        target_price=0.52,
        drift=0.02,
        return_pct=0.04,
        horizon_status="success",
    )
    with pytest.raises(Exception):
        horizon.drift = 999  # type: ignore[misc]


def test_iter_fill_probes_yields_one_record_per_buy_fill(monkeypatch):
    client, activity, _base, _bars = _baseline_inputs()
    monkeypatch.setattr("scripts.execution_profile.FILL_PACE_SEC", 0.0)
    monkeypatch.setattr(
        "scripts.execution_profile._resolve_ts_from_gamma", lambda cid: None
    )

    # Add some SELL noise to confirm the iterator filters.
    polluted = list(activity) + [
        {
            "type": "TRADE",
            "side": "SELL",
            "conditionId": "cid",
            "asset": "asset",
            "timestamp": activity[0]["timestamp"] + 1,
            "price": 0.55,
            "title": "Fake Market",
        }
    ]

    records = list(
        iter_fill_probes(
            "0xWALLET",
            polluted,
            client=client,
            classification={"archetype": "directional_discretionary"},
            counts={"trade_records": 30},
            returns={"resolved_position_count": 10},
            db_path="/tmp/nonexistent/__no__.db",
        )
    )
    assert len(records) == 30
    assert all(r.side == "BUY" for r in records)


def test_iter_fill_probes_horizon_records_match_default_config(monkeypatch):
    client, activity, _base, _bars = _baseline_inputs()
    monkeypatch.setattr("scripts.execution_profile.FILL_PACE_SEC", 0.0)
    monkeypatch.setattr(
        "scripts.execution_profile._resolve_ts_from_gamma", lambda cid: None
    )

    records = list(
        iter_fill_probes(
            "0xWALLET",
            activity,
            client=client,
            classification={"archetype": "directional_discretionary"},
            counts={"trade_records": 30},
            returns={"resolved_position_count": 10},
            db_path="/tmp/nonexistent/__no__.db",
        )
    )
    assert len(records) == 30
    for record in records:
        assert len(record.horizons) == 3
        horizon_seconds = [h.horizon_seconds for h in record.horizons]
        assert horizon_seconds == DEFAULT_PROBE_CONFIG["horizons_seconds"]


def test_iter_fill_probes_reference_price_matches_preceding_bar_picker(monkeypatch):
    """Regression on the contract that ledger probe reference == legacy
    preceding-bar picker, value-for-value."""
    client, activity, base, bars = _baseline_inputs()
    monkeypatch.setattr("scripts.execution_profile.FILL_PACE_SEC", 0.0)
    monkeypatch.setattr(
        "scripts.execution_profile._resolve_ts_from_gamma", lambda cid: None
    )

    records = list(
        iter_fill_probes(
            "0xWALLET",
            activity,
            client=client,
            classification={"archetype": "directional_discretionary"},
            counts={"trade_records": 30},
            returns={"resolved_position_count": 10},
            db_path="/tmp/nonexistent/__no__.db",
        )
    )
    for record in records:
        expected = _preceding_bar(bars, record.fill_ts)
        assert record.reference_price == expected


def test_iter_fill_probes_record_carries_locked_metadata(monkeypatch):
    client, activity, _base, _bars = _baseline_inputs()
    monkeypatch.setattr("scripts.execution_profile.FILL_PACE_SEC", 0.0)
    monkeypatch.setattr(
        "scripts.execution_profile._resolve_ts_from_gamma", lambda cid: None
    )

    records = list(
        iter_fill_probes(
            "0xWALLET",
            activity,
            client=client,
            classification={"archetype": "directional_discretionary"},
            counts={"trade_records": 30},
            returns={"resolved_position_count": 10},
            db_path="/tmp/nonexistent/__no__.db",
        )
    )
    cfg_hash = _probe_config_hash(DEFAULT_PROBE_CONFIG)
    horizons_json = json.dumps(
        DEFAULT_PROBE_CONFIG["horizons_seconds"],
        sort_keys=True,
        separators=(",", ":"),
    )
    for record in records:
        assert record.probe_config_hash == cfg_hash
        assert record.probe_version == DEFAULT_PROBE_CONFIG["probe_version"]
        assert record.reference_rule == DEFAULT_PROBE_CONFIG["reference_rule"]
        assert record.horizons_seconds_json == horizons_json
        assert record.condition_id == "cid"
        assert record.token_id == "asset"
        assert record.probe_status == "success"


# ---------------------------------------------------------------------------
# Aggregate parity — refactor must not change compute_execution_profile output
# ---------------------------------------------------------------------------


def test_compute_execution_profile_aggregate_unchanged_after_refactor(monkeypatch):
    client, activity, _base, _bars = _baseline_inputs()
    monkeypatch.setattr("scripts.execution_profile.FILL_PACE_SEC", 0.0)
    monkeypatch.setattr(
        "scripts.execution_profile._resolve_ts_from_gamma", lambda cid: None
    )

    out = compute_execution_profile(
        "0xWALLET",
        activity,
        client=client,
        classification={"archetype": "directional_discretionary"},
        counts={"trade_records": 30},
        returns={"resolved_position_count": 10},
        db_path="/tmp/nonexistent/__no__.db",
    )

    baseline = json.loads(BASELINE_FIXTURE.read_text())
    assert out == baseline


# ---------------------------------------------------------------------------
# Codex PR #6 review item 4 — same-second event_uid collapse regression
# ---------------------------------------------------------------------------


def test_same_second_buys_each_get_distinct_fill_event_uid(monkeypatch):
    """Three same-second BUY rows on (cid, asset) must produce three
    distinct `fill_event_uid`s in the emitted FillProbeRecord stream.

    Pre-fix, the index `event_uid_index[(cid, asset, ts)] = ev` collapsed
    all three rows to the LAST row's event_uid, then upsert_execution_probes
    deduplicated to one row → fills silently dropped.
    """
    base = 1_000_000
    bars = [
        {"timestamp": base + dt, "price": 0.50 + (0.02 if dt >= 3600 else 0.0)}
        for dt in range(-60, 3600 + 1800, 60)
    ]
    client = MagicMock()
    client.get_prices_series.return_value = bars
    client.get_trades_window.return_value = [
        {"proxyWallet": "0xOTHER", "asset": "asset", "price": 0.50, "side": "BUY"}
    ] * 5

    # Three BUYs at the EXACT same (conditionId, asset, timestamp) but with
    # distinct upstream event_uids (e.g. derived from tx_hash + log_index).
    same_ts = base + 60
    activity = [
        {
            "type": "TRADE",
            "side": "BUY",
            "conditionId": "cid",
            "asset": "asset",
            "timestamp": same_ts,
            "price": 0.50,
            "title": "Fake Market",
            "event_uid": f"data_api:0xtxhash:0:{i}:asset",
        }
        for i in range(3)
    ]

    monkeypatch.setattr("scripts.execution_profile.FILL_PACE_SEC", 0.0)
    monkeypatch.setattr(
        "scripts.execution_profile._resolve_ts_from_gamma", lambda cid: None
    )

    records = list(
        iter_fill_probes(
            "0xWALLET",
            activity,
            client=client,
            classification={"archetype": "directional_discretionary"},
            counts={"trade_records": 3},
            returns={"resolved_position_count": 1},
            db_path="/tmp/nonexistent/__no__.db",
            force=True,
        )
    )
    assert len(records) == 3
    fill_event_uids = [r.fill_event_uid for r in records]
    assert len(set(fill_event_uids)) == 3, (
        f"same-second fills collapsed: got {fill_event_uids}"
    )
    # The three event_uids must match the three input event_uids in order.
    assert fill_event_uids == [
        "data_api:0xtxhash:0:0:asset",
        "data_api:0xtxhash:0:1:asset",
        "data_api:0xtxhash:0:2:asset",
    ]


def test_same_second_buys_persist_through_upsert_execution_probes(tmp_path, monkeypatch):
    """End-to-end regression: 3 same-second BUYs survive the full
    iter_fill_probes → upsert_execution_probes path with all 3 rows
    persisted (not deduplicated to 1). Pre-fix, the same-second collapse
    + dedup-by-fill_event_uid would silently drop 2 of 3 fills."""
    import duckdb

    from sniper.analysis.wallet_protocol.ledger.probes import (
        upsert_execution_probes,
    )
    from sniper.analysis.wallet_protocol.ledger.schema import ensure_wallet_schema

    base = 1_000_000
    bars = [
        {"timestamp": base + dt, "price": 0.50 + (0.02 if dt >= 3600 else 0.0)}
        for dt in range(-60, 3600 + 1800, 60)
    ]
    client = MagicMock()
    client.get_prices_series.return_value = bars
    client.get_trades_window.return_value = [
        {"proxyWallet": "0xOTHER", "asset": "asset", "price": 0.50, "side": "BUY"}
    ] * 5

    same_ts = base + 60
    activity = [
        {
            "type": "TRADE",
            "side": "BUY",
            "conditionId": "cid",
            "asset": "asset",
            "timestamp": same_ts,
            "price": 0.50,
            "title": "Fake Market",
            "event_uid": f"data_api:0xtxhash:0:{i}:asset",
        }
        for i in range(3)
    ]

    monkeypatch.setattr("scripts.execution_profile.FILL_PACE_SEC", 0.0)
    monkeypatch.setattr(
        "scripts.execution_profile._resolve_ts_from_gamma", lambda cid: None
    )

    wallet = "0x" + "a" * 40
    records = list(
        iter_fill_probes(
            wallet,
            activity,
            client=client,
            classification={"archetype": "directional_discretionary"},
            counts={"trade_records": 3},
            returns={"resolved_position_count": 1},
            db_path="/tmp/nonexistent/__no__.db",
            force=True,
        )
    )
    assert len(records) == 3

    conn = duckdb.connect(str(tmp_path / "wallet.duckdb"))
    try:
        ensure_wallet_schema(conn)
        n_persisted = upsert_execution_probes(
            conn,
            wallet=wallet,
            capture_run_id="run-1",
            fill_probes=records,
            source="data_api",
            source_version="2026-05-02",
            captured_at=1_700_000_000,
        )
        assert n_persisted == 3
        rows = conn.execute(
            "SELECT fill_event_uid FROM wallet_execution_probes "
            "WHERE wallet = ? ORDER BY fill_event_uid",
            [wallet.lower()],
        ).fetchall()
        assert len(rows) == 3
        persisted_uids = sorted(r[0] for r in rows)
        assert persisted_uids == [
            "data_api:0xtxhash:0:0:asset",
            "data_api:0xtxhash:0:1:asset",
            "data_api:0xtxhash:0:2:asset",
        ]
    finally:
        conn.close()
