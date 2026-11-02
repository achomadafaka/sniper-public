# tests/test_replay_no_live_fallback.py
"""Under --from-fixture, the runner must not call any live producer.
Spec: docs/superpowers/specs/2026-05-01-fixture-v2-schema.md Rule 1.
"""
from __future__ import annotations

import json
import sqlite3
import unittest.mock as mock
from pathlib import Path

import pytest

from sniper.analysis.wallet_protocol.replay import save_fetch_snapshot
from sniper.analysis.wallet_protocol.types import WalletData


@pytest.fixture
def v2_fixture_path(tmp_path):
    data = WalletData(
        wallet="0xabc",
        closed_positions=[
            {"conditionId": "0xcid0", "outcome": "Yes", "avgPrice": 0.45,
             "size": 50, "realizedPnl": 25.0,
             "endDate": "2026-04-01T00:00:00Z"},
        ],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1="speculator_bot",
        execution_profile={"status": "populated", "fills_with_drift": []},
        fetched_at=1700000000,
        price_history_lookups={},
        leafs_by_cid={"0xcid0": ["politics"]},
        nav_lookup={"politics": "politics"},
        peer_baselines={"leaf|politics": [0.6, 50]},
        replay_surface_meta={
            k: {"status": "captured", "captured_at": 1700000000,
                "source": "test", "producer": "test"}
            for k in (
                "archetype_layer1", "execution_profile",
                "price_history_lookups", "leafs_by_cid",
                "nav_lookup", "peer_baselines",
            )
        },
    )
    out = tmp_path / "v2_fixture.json"
    save_fetch_snapshot(data, out)
    return out


def test_from_fixture_does_not_call_live_producers(v2_fixture_path):
    """Trap every live producer; verify none fire under --from-fixture."""
    import scripts.run_wallet_protocol as runner

    def _trap(name):
        def _f(*a, **kw):
            raise AssertionError(
                f"live producer {name!r} was called under --from-fixture"
            )
        return _f

    with mock.patch.object(runner, "build_execution_profile",
                           side_effect=_trap("build_execution_profile")), \
         mock.patch.object(runner, "build_price_history_fn",
                           side_effect=_trap("build_price_history_fn")), \
         mock.patch.object(sqlite3, "connect",
                           side_effect=_trap("sqlite3.connect")), \
         mock.patch(
             "scripts.wallet_archetype_extractor.extract_wallet_archetype",
             side_effect=_trap("extract_wallet_archetype"),
         ):
        rc = runner.main([
            "0xabc",
            "--from-fixture", str(v2_fixture_path),
            "--seed", "1", "--n-iter", "200",
            "--json", "--no-persist",
        ])
    assert rc == 0


def test_from_fixture_uses_fixture_archetype(v2_fixture_path):
    """The runner must use data.archetype_layer1 from the fixture, not
    invoke the live extractor."""
    import scripts.run_wallet_protocol as runner

    with mock.patch(
        "scripts.wallet_archetype_extractor.extract_wallet_archetype",
        side_effect=AssertionError("extractor invoked under --from-fixture"),
    ):
        rc = runner.main([
            "0xabc",
            "--from-fixture", str(v2_fixture_path),
            "--seed", "1", "--n-iter", "200",
            "--json", "--no-persist",
        ])
    assert rc == 0
