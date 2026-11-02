"""W5 — replay determinism acceptance tests.

Covers:

* ``sniper.analysis.wallet_protocol.replay`` round-trip + report comparison.
* ``scripts/record_fetch_snapshot.py`` — produces a JSON-safe fixture from
  a stubbed fetch (no live API calls).
* ``scripts/run_wallet_protocol.py`` — ``--from-fixture`` skips live fetch;
  ``--no-persist`` overrides ``--persist`` with exit code 2.
* Fixture-driven double-run produces bit-identical verdicts (no live network,
  no DB writes when ``--no-persist`` or no ``--persist``).
* ``scripts/replay_wallet_run.py`` — replays a persisted run by ``run_id``,
  exits 0 on match, non-zero on divergence beyond 1e-9.

Spec ref: docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md
§7.4. Plan ref: docs/superpowers/plans/2026-04-30-analyzer-completion-phase-1.md W5.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

from sniper.analysis.wallet_protocol.capital_ledger import CapitalLedger
from sniper.analysis.wallet_protocol.replay import (
    FIXTURE_SCHEMA_VERSION,
    deserialize_wallet_data,
    load_fetch_snapshot,
    reports_match,
    save_fetch_snapshot,
    serialize_wallet_data,
)
from sniper.analysis.wallet_protocol.types import WalletData


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _synthetic_wallet_data(wallet: str = "0xfeed") -> WalletData:
    """Small WalletData. Stage-1 will reject insufficient_sample (n_closed < 30),
    so analyze_wallet's verdict is deterministic without needing big stats."""
    closed = [
        {
            "conditionId": f"0xc{i:02d}",
            "outcome": "Yes",
            "size": 100.0,
            "avgPrice": 0.5,
            "realizedPnl": (i % 3 - 1) * 5.0,
            "endDate": "2026-01-01T00:00:00Z",
        }
        for i in range(5)
    ]
    open_positions = [
        {
            "conditionId": f"0xo{i:02d}",
            "outcome": "Yes",
            "size": 50.0,
            "avgPrice": 0.4,
            "currentPrice": 0.42,
            "endDate": "2027-01-01T00:00:00Z",
        }
        for i in range(2)
    ]
    activity_rows = [
        {
            "conditionId": "0xc00",
            "side": "BUY",
            "price": 0.5,
            "size": 100.0,
            "timestamp": 1700000000 + i * 60,
            "transactionHash": f"0xt{i:04d}",
            "type": "TRADE",
        }
        for i in range(10)
    ]
    # v2 schema (bumped 2026-05-01 by phase-2B-capture Task 1) requires
    # replay_surface_meta to be a non-null dict. Mark every surface as
    # "skipped" so the helper produces a valid v2 fixture without having
    # to wire up real replay closures here.
    skipped_meta = {
        k: {"status": "skipped", "captured_at": 1_700_000_000,
            "source": "skip", "producer": "test_replay_determinism"}
        for k in (
            "archetype_layer1", "execution_profile", "price_history_lookups",
            "leafs_by_cid", "nav_lookup", "peer_baselines",
        )
    }
    return WalletData(
        wallet=wallet,
        closed_positions=closed,
        open_positions=open_positions,
        activity_rows=activity_rows,
        activity_cap_hit=False,
        archetype_layer1="directional_discretionary",
        execution_profile=None,
        fetched_at=1_700_000_000,
        capital_ledger=CapitalLedger(
            wallet=wallet,
            n_splits=2,
            n_merges=1,
            n_redemptions=0,
            total_deposits_usdc=1000.0,
            total_withdrawals_usdc=200.0,
            total_redemption_payout_usdc=0.0,
            net_capital_funded_usdc=800.0,
            first_event_ts=1_699_900_000,
            last_event_ts=1_700_000_000,
            events=[],
        ),
        portfolio_value_usdc=42.0,
        fetch_quality_flags=["FLAG_A", "FLAG_B"],
        subgraph_fallback_used=False,
        fetch_termination={
            "closed_positions": "partial_page",
            "open_positions": "empty_page",
            "activity": "api_refused",
        },
        replay_surface_meta=skipped_meta,
    )


# ─────────────────────────────────────────────────────────────────────────────
# replay.py — round-trip + report comparison
# ─────────────────────────────────────────────────────────────────────────────


def test_serialize_deserialize_wallet_data_round_trip():
    data = _synthetic_wallet_data()
    payload = serialize_wallet_data(data)
    assert payload["schema_version"] == FIXTURE_SCHEMA_VERSION
    assert payload["wallet"] == data.wallet
    # JSON-safe: must serialize cleanly with stdlib.
    s = json.dumps(payload)
    payload2 = json.loads(s)
    restored = deserialize_wallet_data(payload2)
    assert restored.wallet == data.wallet.lower()
    assert restored.closed_positions == data.closed_positions
    assert restored.open_positions == data.open_positions
    assert restored.activity_rows == data.activity_rows
    assert restored.activity_cap_hit == data.activity_cap_hit
    assert restored.archetype_layer1 == data.archetype_layer1
    assert restored.execution_profile == data.execution_profile
    assert restored.fetched_at == data.fetched_at
    assert restored.portfolio_value_usdc == data.portfolio_value_usdc
    assert restored.fetch_quality_flags == data.fetch_quality_flags
    assert restored.subgraph_fallback_used == data.subgraph_fallback_used
    assert restored.fetch_termination == data.fetch_termination
    # CapitalLedger preserved field-for-field.
    assert restored.capital_ledger is not None
    assert (
        restored.capital_ledger.net_capital_funded_usdc
        == data.capital_ledger.net_capital_funded_usdc
    )
    assert restored.capital_ledger.n_splits == data.capital_ledger.n_splits


def test_save_load_fetch_snapshot_round_trip(tmp_path):
    data = _synthetic_wallet_data()
    out = tmp_path / "fixture.json"
    save_fetch_snapshot(data, out)
    assert out.exists()
    restored = load_fetch_snapshot(out)
    assert restored.wallet == data.wallet.lower()
    assert len(restored.closed_positions) == len(data.closed_positions)
    assert len(restored.activity_rows) == len(data.activity_rows)


def test_deserialize_rejects_unknown_schema_version():
    payload = {"schema_version": 999, "wallet": "0x1"}
    with pytest.raises(ValueError, match="schema_version"):
        deserialize_wallet_data(payload)


def test_deserialize_rejects_missing_wallet():
    payload = {"schema_version": FIXTURE_SCHEMA_VERSION}
    with pytest.raises(ValueError, match="wallet"):
        deserialize_wallet_data(payload)


def test_reports_match_identity():
    a = {
        "overall_verdict": "data_insufficient",
        "primary_edge_type": None,
        "gate_result": "reject_insufficient_sample",
        "n_closed_positions": 5,
        "net_pnl": 1.0,
        "dw_roi_point": 0.123,
    }
    matched, diffs = reports_match(a, dict(a))
    assert matched
    assert diffs == []


def test_reports_match_within_float_tolerance():
    a = {"net_pnl": 1.0, "dw_roi_point": 0.123456789}
    b = {"net_pnl": 1.0 + 1e-12, "dw_roi_point": 0.123456789 + 1e-12}
    matched, diffs = reports_match(a, b, tol=1e-9)
    assert matched, diffs


def test_reports_match_flags_numeric_divergence():
    a = {"net_pnl": 1.0}
    b = {"net_pnl": 1.0 + 1e-3}
    matched, diffs = reports_match(a, b, tol=1e-9)
    assert not matched
    assert any("net_pnl" in d for d in diffs)


def test_reports_match_flags_categorical_divergence():
    a = {"overall_verdict": "data_insufficient", "primary_edge_type": None}
    b = {"overall_verdict": "edge_detected", "primary_edge_type": None}
    matched, diffs = reports_match(a, b)
    assert not matched
    assert any("overall_verdict" in d for d in diffs)


def test_reports_match_per_hypothesis_floats():
    a = {
        "held_analyst_branch": {
            "hypotheses": [
                {"name": "H1", "verdict": "supported",
                 "test_statistic": 1.0, "p_value_raw": 0.01},
            ],
            "tag_grains": [],
        },
    }
    b = {
        "held_analyst_branch": {
            "hypotheses": [
                {"name": "H1", "verdict": "supported",
                 "test_statistic": 1.0 + 1e-12, "p_value_raw": 0.01},
            ],
            "tag_grains": [],
        },
    }
    matched, _ = reports_match(a, b, tol=1e-9)
    assert matched

    c = dict(b)
    c["held_analyst_branch"] = {
        "hypotheses": [
            {"name": "H1", "verdict": "refuted",
             "test_statistic": 1.0, "p_value_raw": 0.01},
        ],
        "tag_grains": [],
    }
    matched_c, diffs_c = reports_match(a, c)
    assert not matched_c
    assert any("H1.verdict" in d for d in diffs_c)


# ─────────────────────────────────────────────────────────────────────────────
# scripts/record_fetch_snapshot.py
# ─────────────────────────────────────────────────────────────────────────────


def test_record_fetch_snapshot_writes_valid_fixture(tmp_path, monkeypatch):
    import scripts.record_fetch_snapshot as mod
    captured = _synthetic_wallet_data()

    def _fake_fetch(wallet, *args, **kwargs):
        return captured

    monkeypatch.setattr(mod, "fetch_wallet_data", _fake_fetch)

    out = tmp_path / "snap.json"
    rc = mod.main([captured.wallet, "--out", str(out)])
    assert rc == 0
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["schema_version"] == FIXTURE_SCHEMA_VERSION
    assert payload["wallet"] == captured.wallet.lower()
    # round-trip cleanly via load_fetch_snapshot
    restored = load_fetch_snapshot(out)
    assert restored.wallet == captured.wallet.lower()


# ─────────────────────────────────────────────────────────────────────────────
# scripts/run_wallet_protocol.py — --from-fixture / --no-persist
# ─────────────────────────────────────────────────────────────────────────────


def test_run_wallet_protocol_from_fixture_skips_live_fetch(tmp_path, monkeypatch):
    """--from-fixture must not call fetch_wallet_data and must not hit DB."""
    fixture_path = tmp_path / "fixture.json"
    save_fetch_snapshot(_synthetic_wallet_data("0xreplay01"), fixture_path)

    import scripts.run_wallet_protocol as mod

    def _fail(*a, **kw):
        raise AssertionError("fetch_wallet_data must not be called in fixture mode")

    monkeypatch.setattr(mod, "fetch_wallet_data", _fail)

    db_path = tmp_path / "sandbox.db"
    from db import init_db
    c = sqlite3.connect(str(db_path))
    init_db(c)
    c.close()
    monkeypatch.setenv("SNIPER_DB_PATH", str(db_path))

    rc = mod.main([
        "0xreplay01",
        "--from-fixture", str(fixture_path),
        "--no-tags", "--no-peers", "--no-exec", "--no-history", "--no-archetype",
    ])
    assert rc == 0

    conn = sqlite3.connect(str(db_path))
    try:
        rows = list(conn.execute("SELECT COUNT(*) FROM analyzer_runs"))
        # No --persist passed → no analyzer_runs row.
        assert rows[0][0] == 0
    finally:
        conn.close()


def test_run_wallet_protocol_no_persist_overrides_persist(tmp_path, monkeypatch):
    """--persist + --no-persist together must exit 2 and write nothing."""
    fixture_path = tmp_path / "fixture.json"
    save_fetch_snapshot(_synthetic_wallet_data("0xreplay02"), fixture_path)

    import scripts.run_wallet_protocol as mod
    monkeypatch.setattr(mod, "fetch_wallet_data",
                        lambda *a, **kw: pytest.fail("must not fetch"))

    db_path = tmp_path / "sandbox.db"
    from db import init_db
    c = sqlite3.connect(str(db_path))
    init_db(c)
    c.close()
    monkeypatch.setenv("SNIPER_DB_PATH", str(db_path))

    rc = mod.main([
        "0xreplay02",
        "--from-fixture", str(fixture_path),
        "--persist", "--no-persist",
        "--no-tags", "--no-peers", "--no-exec", "--no-history", "--no-archetype",
    ])
    assert rc == 2

    conn = sqlite3.connect(str(db_path))
    try:
        rows = list(conn.execute("SELECT COUNT(*) FROM analyzer_runs"))
        assert rows[0][0] == 0
    finally:
        conn.close()


def test_run_wallet_protocol_fixture_double_run_is_deterministic(
    tmp_path, monkeypatch
):
    """Same fixture + same seed → bit-identical verdict + numeric details."""
    fixture_path = tmp_path / "fixture.json"
    save_fetch_snapshot(_synthetic_wallet_data("0xreplay03"), fixture_path)

    import scripts.run_wallet_protocol as mod

    def _no_fetch(*a, **kw):
        raise AssertionError("fetch must not be called in fixture mode")

    monkeypatch.setattr(mod, "fetch_wallet_data", _no_fetch)

    out_a = tmp_path / "a.json"
    out_b = tmp_path / "b.json"

    rc_a = _run_collect_json(mod, [
        "0xreplay03",
        "--from-fixture", str(fixture_path),
        "--seed", "1", "--n-iter", "256",
        "--no-tags", "--no-peers", "--no-exec", "--no-history", "--no-archetype",
        "--json",
    ], out_a)
    rc_b = _run_collect_json(mod, [
        "0xreplay03",
        "--from-fixture", str(fixture_path),
        "--seed", "1", "--n-iter", "256",
        "--no-tags", "--no-peers", "--no-exec", "--no-history", "--no-archetype",
        "--json",
    ], out_b)
    assert rc_a == 0 and rc_b == 0

    a = json.loads(out_a.read_text())
    b = json.loads(out_b.read_text())
    matched, diffs = reports_match(a, b, tol=1e-9)
    assert matched, f"non-deterministic replay: {diffs}"


def _run_collect_json(mod, argv, out_path):
    """Helper: run main() and capture --json stdout into out_path."""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main(argv)
    out_path.write_text(buf.getvalue())
    return rc


# ─────────────────────────────────────────────────────────────────────────────
# scripts/replay_wallet_run.py
# ─────────────────────────────────────────────────────────────────────────────


def _persist_synthetic_run(db_path: Path, *, run_id: str, wallet: str,
                           args_json: str) -> None:
    """Insert a single success=1 analyzer_runs row for replay tests."""
    from db import init_db
    c = sqlite3.connect(str(db_path))
    init_db(c)
    now = int(time.time())
    c.execute(
        """
        INSERT INTO analyzer_runs (
            run_id, analysis_version, wallet, started_ts, finished_ts,
            duration_ms, success, invocation_source, args_json,
            fetch_quality_flags_json, activity_rows_fetched, activity_cap_hit,
            subgraph_fallback_used
        ) VALUES (?, 'wpv2.0', ?, ?, ?, 0, 1, 'cli', ?, '[]', 10, 0, 0)
        """,
        (run_id, wallet, now - 1, now, args_json),
    )
    c.commit()
    c.close()


def test_replay_wallet_run_matches_fixture_run(tmp_path, monkeypatch):
    """run_id with from_fixture in args_json → exit 0 when reports match."""
    fixture_path = tmp_path / "fixture.json"
    save_fetch_snapshot(_synthetic_wallet_data("0xreplay04"), fixture_path)

    db_path = tmp_path / "sandbox.db"
    run_id = "test-run-replay-04"
    args_json = json.dumps({
        "wallet": "0xreplay04",
        "from_fixture": str(fixture_path),
        "seed": 1,
        "n_iter": 256,
        "no_tags": True, "no_peers": True, "no_exec": True,
        "no_history": True, "no_archetype": True,
        "capital_ledger": False,
    })
    _persist_synthetic_run(db_path, run_id=run_id, wallet="0xreplay04",
                           args_json=args_json)

    import scripts.replay_wallet_run as mod
    import scripts.run_wallet_protocol as runner_mod

    monkeypatch.setattr(runner_mod, "fetch_wallet_data",
                        lambda *a, **kw: pytest.fail("no fetch"))

    rc = mod.main([run_id, "--db", str(db_path)])
    assert rc == 0


def test_replay_wallet_run_detects_divergence(tmp_path, monkeypatch):
    """If the recorded report differs beyond tol from the replayed report,
    replay_wallet_run.py exits non-zero."""
    fixture_path = tmp_path / "fixture.json"
    save_fetch_snapshot(_synthetic_wallet_data("0xreplay05"), fixture_path)

    db_path = tmp_path / "sandbox.db"
    run_id = "test-run-replay-05"
    args_json = json.dumps({
        "wallet": "0xreplay05",
        "from_fixture": str(fixture_path),
        "seed": 1,
        "n_iter": 256,
        "no_tags": True, "no_peers": True, "no_exec": True,
        "no_history": True, "no_archetype": True,
        "capital_ledger": False,
    })
    _persist_synthetic_run(db_path, run_id=run_id, wallet="0xreplay05",
                           args_json=args_json)

    import scripts.replay_wallet_run as mod
    import scripts.run_wallet_protocol as runner_mod

    monkeypatch.setattr(runner_mod, "fetch_wallet_data",
                        lambda *a, **kw: pytest.fail("no fetch"))

    # Inject a divergence by patching reports_match to always return False.
    monkeypatch.setattr(mod, "reports_match",
                        lambda a, b, **kw: (False, ["overall_verdict: synthetic"]))

    rc = mod.main([run_id, "--db", str(db_path)])
    assert rc != 0


def test_replay_wallet_run_missing_run_id(tmp_path):
    db_path = tmp_path / "sandbox.db"
    from db import init_db
    c = sqlite3.connect(str(db_path))
    init_db(c)
    c.close()
    import scripts.replay_wallet_run as mod
    rc = mod.main(["nonexistent-run-id", "--db", str(db_path)])
    assert rc != 0


# ─────────────────────────────────────────────────────────────────────────────
# Smoke: shipped fixture file under tests/replay/fixtures/ loads cleanly.
# ─────────────────────────────────────────────────────────────────────────────


def test_shipped_swissmiss_fixture_loads():
    fixture = REPO_ROOT / "tests" / "replay" / "fixtures" / "swissmiss_fetch_snapshot.json"
    if not fixture.exists():
        pytest.skip(f"fixture not shipped: {fixture}")
    data = load_fetch_snapshot(fixture)
    assert data.wallet.startswith("0x")
    assert isinstance(data.closed_positions, list)
    assert isinstance(data.activity_rows, list)


@pytest.fixture(autouse=True)
def _polymarket_is_sinkholed(monkeypatch):
    """This host resolves *.polymarket.com to an unreachable address (2026-07-29 sinkhole, moved to an
    unreachable IPv6 on 2026-09-22), so a live activity fetch hangs past the test budget instead of
    failing fast. These tests exercise flags and stamps, never the data: fail the fetch at once, as
    a refused connection did before."""
    from data import polymarket_activity_client as _pac

    def _no_network(path, **_kw):
        raise _pac.PolymarketActivityError("request failed after retries: sinkholed in tests")
    monkeypatch.setattr(_pac, "_get_json", _no_network)
