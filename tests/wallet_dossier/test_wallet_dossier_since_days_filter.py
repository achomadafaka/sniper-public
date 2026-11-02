"""Tests for the --since-days time-window filter on the dossier builder.

Covers `filter_fixture_by_since_days()` and the meta surface that records
the window.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sniper.analysis.wallet_dossier.builder import (
    build_dossier,
    filter_fixture_by_since_days,
)


def _fixture_with_dated_activity(fetched_at: int) -> dict:
    """Minimal WalletData with timestamps spanning ~120 days back from fetched_at.

    Five positions: entry timestamps at fetched_at - {5d, 15d, 35d, 70d, 110d}.
    Two redemptions at fetched_at - {7d, 90d}.
    Activity rows mirror position timestamps + a few interleaved sells.
    """
    SEC = 86400
    cond = lambda i: "0x" + str(i) * 64
    positions = [
        {"conditionId": cond(1), "asset": "1001", "outcome": "Yes",
         "title": "P1", "avgPrice": 0.5, "totalBought": 10.0,
         "realizedPnl": 1.0, "timestamp": fetched_at - 5 * SEC},
        {"conditionId": cond(2), "asset": "2001", "outcome": "Yes",
         "title": "P2", "avgPrice": 0.5, "totalBought": 10.0,
         "realizedPnl": 1.0, "timestamp": fetched_at - 15 * SEC},
        {"conditionId": cond(3), "asset": "3001", "outcome": "Yes",
         "title": "P3", "avgPrice": 0.5, "totalBought": 10.0,
         "realizedPnl": 1.0, "timestamp": fetched_at - 35 * SEC},
        {"conditionId": cond(4), "asset": "4001", "outcome": "Yes",
         "title": "P4", "avgPrice": 0.5, "totalBought": 10.0,
         "realizedPnl": 1.0, "timestamp": fetched_at - 70 * SEC},
        {"conditionId": cond(5), "asset": "5001", "outcome": "Yes",
         "title": "P5", "avgPrice": 0.5, "totalBought": 10.0,
         "realizedPnl": 1.0, "timestamp": fetched_at - 110 * SEC},
    ]
    activity = [
        {"conditionId": cond(1), "asset": "1001", "type": "TRADE",
         "side": "BUY", "size": 10, "price": 0.5,
         "timestamp": fetched_at - 5 * SEC},
        {"conditionId": cond(2), "asset": "2001", "type": "TRADE",
         "side": "BUY", "size": 10, "price": 0.5,
         "timestamp": fetched_at - 15 * SEC},
        {"conditionId": cond(3), "asset": "3001", "type": "TRADE",
         "side": "BUY", "size": 10, "price": 0.5,
         "timestamp": fetched_at - 35 * SEC},
        {"conditionId": cond(4), "asset": "4001", "type": "TRADE",
         "side": "BUY", "size": 10, "price": 0.5,
         "timestamp": fetched_at - 70 * SEC},
        {"conditionId": cond(5), "asset": "5001", "type": "TRADE",
         "side": "BUY", "size": 10, "price": 0.5,
         "timestamp": fetched_at - 110 * SEC},
    ]
    redemptions = [
        {"condition": cond(1), "indexSets": ["1"], "payout": "5",
         "timestamp": str(fetched_at - 7 * SEC)},
        {"condition": cond(4), "indexSets": ["1"], "payout": "0",
         "timestamp": str(fetched_at - 90 * SEC)},
    ]
    return {
        "wallet": "0xdeadbeef" + "0" * 32,
        "fetched_at": fetched_at,
        "schema_version": 2,
        "closed_positions": positions,
        "open_positions": [],
        "activity_rows": activity,
        "redemptions": redemptions,
        "leafs_by_cid": {},
        "nav_lookup": {},
    }


def test_filter_drops_pre_window_positions_and_activity():
    fetched_at = 2_000_000_000
    fixture = _fixture_with_dated_activity(fetched_at)
    filtered = filter_fixture_by_since_days(fixture, 30)
    # Positions: 5d + 15d in window; 35d/70d/110d dropped.
    assert len(filtered["closed_positions"]) == 2
    kept_ts = sorted(p["timestamp"] for p in filtered["closed_positions"])
    assert kept_ts == [fetched_at - 15 * 86400, fetched_at - 5 * 86400]
    # Activity_rows: same two ts retained.
    assert len(filtered["activity_rows"]) == 2
    # Redemptions: 7d in window kept, 90d dropped.
    assert len(filtered["redemptions"]) == 1
    assert int(filtered["redemptions"][0]["timestamp"]) == fetched_at - 7 * 86400
    # Metadata stamp emitted.
    meta = filtered["_since_days_filter"]
    assert meta["since_days"] == 30
    assert meta["cutoff_ts"] == fetched_at - 30 * 86400
    assert meta["fetched_at"] == fetched_at


def test_filter_preserves_open_positions_and_leafs():
    fetched_at = 2_000_000_000
    fixture = _fixture_with_dated_activity(fetched_at)
    fixture["open_positions"] = [
        {"conditionId": "0xopen", "asset": "9999",
         "timestamp": fetched_at - 200 * 86400}
    ]
    fixture["leafs_by_cid"] = {"0xfoo": ["tag1"]}
    filtered = filter_fixture_by_since_days(fixture, 30)
    assert filtered["open_positions"] == fixture["open_positions"]
    assert filtered["leafs_by_cid"] == {"0xfoo": ["tag1"]}


def test_filter_rejects_non_positive_since_days():
    fetched_at = 2_000_000_000
    fixture = _fixture_with_dated_activity(fetched_at)
    with pytest.raises(ValueError, match="positive"):
        filter_fixture_by_since_days(fixture, 0)
    with pytest.raises(ValueError, match="positive"):
        filter_fixture_by_since_days(fixture, -5)


def test_filter_requires_fetched_at():
    fixture = _fixture_with_dated_activity(2_000_000_000)
    fixture.pop("fetched_at")
    with pytest.raises(ValueError, match="fetched_at"):
        filter_fixture_by_since_days(fixture, 30)


def test_build_dossier_with_since_days_records_meta(tmp_path: Path):
    fetched_at = 2_000_000_000
    fixture = _fixture_with_dated_activity(fetched_at)
    fix_path = tmp_path / "fixture.json"
    fix_path.write_text(json.dumps(fixture))
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({
        "protocol_version": "v2",
        "wallet": fixture["wallet"],
    }))

    tables = build_dossier(
        wallet=fixture["wallet"],
        fixture_path=fix_path,
        protocol_report_path=report_path,
        since_days=30,
    )

    # since_days_filter meta row emitted with the right values.
    meta_keys = {row["key"]: row["value"] for row in tables.dossier_meta}
    assert "since_days_filter" in meta_keys
    val = meta_keys["since_days_filter"]
    assert "since_days=30" in val
    assert f"fetched_at={fetched_at}" in val
    assert f"cutoff_ts={fetched_at - 30 * 86400}" in val


def test_build_dossier_without_since_days_omits_meta(tmp_path: Path):
    fetched_at = 2_000_000_000
    fixture = _fixture_with_dated_activity(fetched_at)
    fix_path = tmp_path / "fixture.json"
    fix_path.write_text(json.dumps(fixture))
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({
        "protocol_version": "v2",
        "wallet": fixture["wallet"],
    }))

    tables = build_dossier(
        wallet=fixture["wallet"],
        fixture_path=fix_path,
        protocol_report_path=report_path,
    )
    meta_keys = {row["key"] for row in tables.dossier_meta}
    assert "since_days_filter" not in meta_keys


def test_filter_with_no_redemptions_field():
    """Fixtures without a `redemptions` field (e.g. 2026-05-13 calibration set)
    must not have one synthesized by the filter."""
    fetched_at = 2_000_000_000
    fixture = _fixture_with_dated_activity(fetched_at)
    fixture.pop("redemptions")
    filtered = filter_fixture_by_since_days(fixture, 30)
    assert "redemptions" not in filtered


def test_filter_position_kept_with_truncated_fill_history():
    """Position entered IN window, but had earlier activity rows that
    happen to be OUTSIDE window. Document the partial-window behavior:
    position is kept (entry in window) but only in-window activity rows
    survive. Codex 2026-05-27 flagged this as an unexercised edge case.

    Spec choice: kept positions may have partial fill history. Episode
    derivation downstream sees only the in-window fills — caller is
    responsible for interpreting recent-window dossiers as "what this
    wallet did in the last N days," not "complete history of these
    positions."
    """
    fetched_at = 2_000_000_000
    SEC = 86400
    fixture = {
        "wallet": "0xdeadbeef" + "0" * 32,
        "fetched_at": fetched_at,
        "schema_version": 2,
        "closed_positions": [
            {
                "conditionId": "0xabc",
                "asset": "1001",
                "outcome": "Yes",
                "title": "Match",
                "avgPrice": 0.5,
                "totalBought": 100.0,
                "realizedPnl": 1.0,
                # Entry IS in window (5 days back)
                "timestamp": fetched_at - 5 * SEC,
            }
        ],
        "open_positions": [],
        "activity_rows": [
            # In-window BUY at 5 days back
            {
                "conditionId": "0xabc", "asset": "1001", "type": "TRADE",
                "side": "BUY", "size": 10, "price": 0.5,
                "timestamp": fetched_at - 5 * SEC,
            },
            # Pre-window BUY at 60 days back (same position, scaled in)
            {
                "conditionId": "0xabc", "asset": "1001", "type": "TRADE",
                "side": "BUY", "size": 5, "price": 0.4,
                "timestamp": fetched_at - 60 * SEC,
            },
        ],
        "leafs_by_cid": {},
        "nav_lookup": {},
    }
    filtered = filter_fixture_by_since_days(fixture, 30)

    # Closed position survived (entry timestamp is in window)
    assert len(filtered["closed_positions"]) == 1
    # Only the in-window activity row survives — pre-window fill dropped
    assert len(filtered["activity_rows"]) == 1
    assert filtered["activity_rows"][0]["timestamp"] == fetched_at - 5 * SEC
    # Documented metadata stamp
    assert filtered["_since_days_filter"]["since_days"] == 30
