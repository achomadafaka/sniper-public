import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.research.memecoin_pattern_study import load_lane, run_study, write_artifacts
from sniper.persistence import db_memecoin as m


START = datetime(2026, 7, 11, tzinfo=timezone.utc)
ASOF = (START + timedelta(days=2)).isoformat()


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _legacy_event(
    conn, event_id, minute, *, mint="M", cap=800_000, clock="observed_real_utc",
    ambiguous=False, status=None,
):
    source = START + timedelta(minutes=minute)
    observed_dt = source + (timedelta(hours=3) if ambiguous else timedelta())
    observed = observed_dt.isoformat()
    created = source.isoformat()
    conn.execute(
        "INSERT INTO memecoin_events(fomo_event_id,source_user_id,source_handle,mint,ticker,"
        "side,their_price,their_market_cap,their_size_usd,created_at,received_at,"
        "received_at_normalized,capture_version,clock_domain,network_id) "
        "VALUES (?,?,?,?,?,'buy',?,?,?,?,?,?,1,?,'solana')",
        (event_id, f"u-{event_id}", f"h-{event_id}", mint, "T", 1.0, cap, 100,
         created, observed, observed if clock != "legacy_unknown" else None, clock),
    )
    conn.execute(
        "INSERT INTO memecoin_signal_outcomes(fomo_event_id,source_user_id,handle,mint,ticker,"
        "buy_ts,ref_price,their_size_usd,marks_json,max_gain_pct,last_price,n_marks,status,"
        "created_at,updated_at,network_id,buy_ts_normalized,capture_version,clock_domain) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
        (event_id, f"u-{event_id}", f"h-{event_id}", mint, "T", observed, 1.0, 100,
         '{"900":{"price":1.2,"age_s":900}}', 20, 1.1, 1,
         status or ("matured" if clock != "bad_quote" else "bad_quote"),
         observed, observed, "solana",
         observed if clock not in {"legacy_unknown", "bad_quote"} else None,
         "legacy_unknown" if clock == "bad_quote" else clock),
    )


def test_legacy_loader_dedups_episode_and_counts_clock_and_status_exclusions():
    conn = _conn()
    _legacy_event(conn, "first", 0)
    _legacy_event(conn, "repeat", 5)
    _legacy_event(conn, "derived", 10, mint="D", clock="legacy_unknown")
    _legacy_event(conn, "unknown", 12, mint="U", clock="legacy_unknown", ambiguous=True)
    _legacy_event(conn, "bad", 15, mint="B", clock="bad_quote")
    for index, hours in enumerate((0, 24), start=1):
        at = (START + timedelta(hours=hours)).isoformat()
        conn.execute(
            "INSERT INTO memecoin_quote_ledger(mint,ts_minute,ts,price,liquidity,source,quality) "
            "VALUES ('M',?,?,?,?, 'fixture',NULL)",
            (at[:16], at, 1.0 + index * 0.1, 75_000),
        )
    conn.commit()

    episodes, manifest = load_lane(conn, "legacy", ASOF, capture_period_s=43200)
    assert len(episodes) == 2
    assert episodes[0]["source_observation_id"] == "first"
    assert episodes[0]["dense_complete"] is True
    assert episodes[0]["legacy_ungraded_quotes"] == 2
    assert manifest["source_rows"] == 5
    assert manifest["source_first_observed_at"] == START.isoformat()
    assert manifest["source_last_observed_at"] == (START + timedelta(hours=3, minutes=12)).isoformat()
    assert manifest["deduplicated_sightings"] == 1
    assert manifest["exclusions"]["legacy_unknown_clock"] == 1
    assert manifest["legacy_clock_derived"] == 1
    assert manifest["exclusions"]["bad_quote"] == 1


def _v2_feed(conn, event_id, minute, user, *, mint="M", curated=True, cap=900_000):
    at = (START + timedelta(minutes=minute)).isoformat()
    conn.execute(
        "INSERT INTO memecoin_feed_observations_v2(upstream_event_id,raw_type,normalized_side,"
        "source_user_id,source_handle,network_id,mint,ticker,their_price,their_market_cap,"
        "their_size_usd,observed_at,was_curated,was_real_approved,raw_json,capture_version) "
        "VALUES (?,'large_buy','buy',?,?,'solana',?,'T',1.0,?,100,?,?,?, '{}',2)",
        (event_id, user, user, mint, cap, at, curated, curated),
    )
    return at


def test_v2_loader_preserves_point_in_time_flags_and_never_pools_legacy():
    conn = _conn()
    at = _v2_feed(conn, "v2", 0, "curated", curated=True)
    _v2_feed(conn, "unknown", 1, "unknown", curated=None)
    _legacy_event(conn, "legacy", 0, mint="L")
    conn.execute(
        "INSERT INTO memecoin_signal_outcomes_v2(outcome_id,upstream_event_id,network_id,mint,"
        "signal_observed_at,ref_price,target_horizons_json,expected_marks,observed_marks,status,"
        "created_at,capture_version) VALUES ('o','v2','solana','M',?,1.0,'[]',0,0,'open',?,2)",
        (at, at),
    )
    for index, hours in enumerate((0, 24), start=1):
        quote_at = (START + timedelta(hours=hours)).isoformat()
        sweep = f"s{index}"
        conn.execute(
            "INSERT INTO memecoin_quote_sweeps_v2(sweep_id,observed_at,eligible_count,"
            "requested_count,captured_count,budget_skipped_count,capture_version) "
            "VALUES (?,?,1,1,1,0,2)", (sweep, quote_at),
        )
        conn.execute(
            "INSERT INTO memecoin_quote_observations_v2(sweep_id,network_id,mint,observed_at,"
            "price,liquidity,market_cap,volume_24h,quality,contexts,capture_version) "
            "VALUES (?,'solana','M',?,1.1,80000,900000,100000,'ok','signal',2)",
            (sweep, quote_at),
        )
    conn.commit()

    episodes, manifest = load_lane(conn, "v2", ASOF, capture_period_s=43200)
    assert manifest["lane"] == "v2"
    assert manifest["source_rows"] == 2
    assert all(episode["mint"] != "L" for episode in episodes)
    assert episodes[0]["feed_rows"][0]["was_curated"] == 1
    assert episodes[0]["dense_complete"] is True


def test_legacy_open_outcome_is_retained_only_as_partial_evidence():
    conn = _conn()
    _legacy_event(conn, "open", 0, status="open")
    for minute, price in enumerate((1.0, 1.7, 0.5, 1.8)):
        at = (START + timedelta(minutes=minute)).isoformat()
        conn.execute(
            "INSERT INTO memecoin_quote_ledger(mint,ts_minute,ts,price,liquidity,source,quality) "
            "VALUES ('M',?,?,?,?, 'fixture',NULL)",
            (at[:16], at, price, 75_000),
        )
    conn.commit()
    episodes, manifest = load_lane(conn, "legacy", ASOF, capture_period_s=60)
    assert len(episodes) == 1
    assert episodes[0]["dense_complete"] is False
    assert manifest["open_partial_outcomes"] == 1
    result = run_study(conn, "legacy", ASOF, capture_period_s=60)
    assert result["episodes"][0]["runner_then_deep_recovery_new_high"] is True
    assert result["manifest"]["overlap_patterns"] == {
        "quote_observed_episodes": 1,
        "runner_then_deep_recovery_new_high": 1,
    }


def test_sparse_path_is_descriptive_only_and_thin_study_blocks_policy_winner():
    conn = _conn()
    _legacy_event(conn, "only", 0)
    conn.commit()
    result = run_study(conn, "legacy", ASOF, capture_period_s=60, entry_bps=100, exit_bps=100)
    assert result["episodes"][0]["path_status"] == "sparse_only"
    assert result["episodes"][0]["archetype"] == "incomplete"
    assert result["episodes"][0]["observed_shape"] == "observed_insufficient"
    assert result["policy_rows"] == []
    assert result["manifest"]["claims"]["policy_ranking"] == "blocked_no_complete_paths"
    assert result["manifest"]["control_matches"] == {"eligible_recoveries": 0, "matched": 0}
    assert result["manifest"]["rankings"] == {
        "overall": {"status": "blocked_no_complete_paths", "winner": None, "policies": {}},
        "by_archetype_market_cap": {},
    }


def test_artifacts_are_deterministic_for_fixed_snapshot_and_asof(tmp_path):
    conn = _conn()
    _legacy_event(conn, "only", 0)
    conn.commit()
    first = run_study(conn, "legacy", ASOF, capture_period_s=60, entry_bps=100, exit_bps=100)
    second = run_study(conn, "legacy", ASOF, capture_period_s=60, entry_bps=100, exit_bps=100)
    assert first["manifest"]["evidence_hash"] == second["manifest"]["evidence_hash"]

    paths1 = write_artifacts(first, tmp_path / "one")
    paths2 = write_artifacts(second, tmp_path / "two")
    assert Path(paths1["episodes_csv"]).read_bytes() == Path(paths2["episodes_csv"]).read_bytes()
    assert Path(paths1["policies_csv"]).read_bytes() == Path(paths2["policies_csv"]).read_bytes()
    manifest = json.loads(Path(paths1["manifest_json"]).read_text())
    report = Path(paths1["report_md"]).read_text()
    assert manifest["lane"] == "legacy"
    assert "No policy winner" in report
    assert "Matched recovery controls: 0/0" in report


def test_load_lane_rejects_naive_asof_and_unknown_lane():
    conn = _conn()
    with pytest.raises(ValueError, match="timezone aware"):
        load_lane(conn, "legacy", "2026-07-11T00:00:00")
    with pytest.raises(ValueError, match="lane"):
        load_lane(conn, "mixed", ASOF)


import pytest as _pytest
from sniper.research.wallet_execution.publication import diagnostic_mode as _diagnostic_mode
@_pytest.fixture(autouse=True)
def _explicit_legacy_diagnostics():
    with _diagnostic_mode():
        yield
