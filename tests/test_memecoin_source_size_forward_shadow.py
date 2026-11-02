import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_policy import build_paper_cohort, resolve_paper_arm_policy


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _event(conn, event, size, *, mint=None, created_at="2026-08-19T18:00:00+00:00"):
    event_mint = mint or f"M-{event}"
    conn.execute(
        "INSERT INTO memecoin_events "
        "(fomo_event_id,source_user_id,source_handle,mint,ticker,side,their_size_usd,"
        "created_at,received_at,network_id) VALUES (?,?,?,?,?,'buy',?,?,?,'solana')",
        (event, "wallet", "wallet", event_mint, event, size,
         created_at, created_at),
    )
    conn.execute(
        "INSERT INTO memecoin_entry_snapshots "
        "(upstream_event_id,episode_id,chain,mint,action,decided_at) "
        "VALUES (?,?,?,?,?,?)",
        (event, f"ep-{event}", "solana", event_mint, "copy", created_at),
    )
    conn.commit()


def _position(conn, arm, event, *, cohort, opened_at, pnl, censored=False):
    policy_id, policy_version, policy_json = resolve_paper_arm_policy(
        arm, network_id="solana", opened_at=opened_at,
    )
    snapshot_id = conn.execute(
        "SELECT snapshot_id FROM memecoin_entry_snapshots WHERE upstream_event_id=?",
        (event,),
    ).fetchone()[0]
    m.record_policy_assignment(
        conn, episode_id=f"ep-{event}", arm_id=arm, policy_id=policy_id,
        policy_version=policy_version, parameters_json=policy_json,
        entry_snapshot_id=snapshot_id, assigned_ts=opened_at,
    )
    pid = m.open_position(
        conn, source_user_id="wallet", source_handle="wallet", mint=f"M-{event}",
        ticker=event, entry_price=1.0, entry_notional=100.0,
        entry_fill_source="route_quote", entry_lag_s=1.0, their_size_usd=1.0,
        opened_at=opened_at, strategy=arm, origin_event_id=event, network_id="solana",
        assigned_policy_id=policy_id, assigned_policy_version=policy_version,
        assigned_policy_json=policy_json, paper_cohort_id=cohort,
        paper_cohort_json='{"arms":[]}', experiment_status="eligible",
    )
    m.insert_fill(
        conn, position_id=pid, kind="entry", price=1.0, qty=100.0, notional=100.0,
        fill_source="route_quote", fraction=1.0, created_at=opened_at,
        origin_event_id=event, network_id="solana",
    )
    closed_at = opened_at.replace("18:00:00", "18:01:00")
    m.insert_fill(
        conn, position_id=pid, kind="bracket_exit", price=1.0 + pnl / 100.0,
        qty=100.0, notional=100.0 + pnl,
        fill_source="noquote" if censored else "route_quote", fraction=1.0,
        created_at=closed_at, origin_event_id=event, network_id="solana",
    )
    m.close_position(conn, pid, closed_at, realized_pnl=pnl, exit_reason="test")
    return pid


def _set_roster(conn, cohort, contract):
    conn.execute(
        "UPDATE memecoin_positions SET paper_cohort_json=? WHERE paper_cohort_id=?",
        (contract, cohort),
    )
    conn.commit()


def _real_position(conn, event, size, *, opened_at, pnl, event_created_at=None):
    _event(conn, event, size, created_at=event_created_at or opened_at)
    pid = m.open_position(
        conn, source_user_id="wallet", source_handle="wallet", mint=f"M-{event}",
        ticker=event, entry_price=1.0, entry_notional=25.0,
        entry_fill_source="chain", entry_lag_s=1.0, their_size_usd=size or 0.0,
        opened_at=opened_at, strategy="alpha_b", origin_event_id=event,
        network_id="solana", execution_mode="real",
    )
    m.insert_fill(
        conn, position_id=pid, kind="entry", price=1.0, qty=25.0, notional=25.0,
        fill_source="chain", fraction=1.0, created_at=opened_at,
        origin_event_id=event, network_id="solana",
    )
    closed_at = opened_at.replace("18:00:00", "18:01:00")
    m.insert_fill(
        conn, position_id=pid, kind="bracket_exit", price=1.0 + pnl / 25.0,
        qty=25.0, notional=25.0 + pnl, fill_source="chain", fraction=1.0,
        created_at=closed_at, origin_event_id=event, network_id="solana",
    )
    m.close_position(conn, pid, closed_at, realized_pnl=pnl, exit_reason="test")
    return pid


def test_load_forward_origins_enforces_cutoff_and_whole_origin_censor():
    """A missing, duplicate, or censored arm must remove the complete origin."""
    from scripts.research.memecoin_source_size_forward_shadow import load_forward_origins

    conn = _conn()
    arms = ("alpha_b", "combo_f")
    cohort, contract = build_paper_cohort(arms, notional_usd=100.0)
    cutoff = "2026-08-19T17:33:35.131664+00:00"
    after = "2026-08-19T18:00:00+00:00"
    before = "2026-08-19T17:00:00+00:00"

    for event, size, opened_at in (
        ("LOW", 499.0, after),
        ("UNKNOWN", None, after),
        ("CENSORED", 700.0, after),
        ("DUPLICATE", 800.0, after),
        ("BEFORE", 900.0, before),
        ("DELAYED", 1_000.0, before),
        ("MUTATED", 1_100.0, after),
        ("BAD_COHORT", 1_200.0, after),
        ("BAD_NOTIONAL", 1_300.0, after),
    ):
        _event(conn, event, size, created_at=opened_at)
        for arm in arms:
            _position(
                conn, arm, event, cohort=cohort,
                opened_at=(after if event == "DELAYED" else opened_at), pnl=5.0,
                censored=(event == "CENSORED" and arm == "combo_f"),
            )
    _position(conn, "alpha_b", "DUPLICATE", cohort=cohort, opened_at=after, pnl=5.0)
    _set_roster(conn, cohort, contract)
    mutated_id = conn.execute(
        "SELECT id FROM memecoin_positions "
        "WHERE origin_event_id='MUTATED' AND strategy='combo_f'",
    ).fetchone()[0]
    payload = json.loads(conn.execute(
        "SELECT assigned_policy_json FROM memecoin_positions WHERE id=?",
        (mutated_id,),
    ).fetchone()[0])
    payload["params"]["stop_pct"] = 1.0
    mutated_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    mutated_policy_id = "combo_f-paper-v1@" + hashlib.sha256(
        mutated_json.encode()
    ).hexdigest()
    conn.execute(
        "UPDATE memecoin_positions SET assigned_policy_id=?,assigned_policy_json=? "
        "WHERE id=?",
        (mutated_policy_id, mutated_json, mutated_id),
    )
    conn.execute(
        "UPDATE memecoin_positions SET paper_cohort_json=? "
        "WHERE origin_event_id='BAD_COHORT'",
        (json.dumps({"arms": [{"strategy": arm} for arm in arms]}),),
    )
    conn.execute(
        "UPDATE memecoin_positions SET entry_notional=50.0 "
        "WHERE origin_event_id='BAD_NOTIONAL' AND strategy='combo_f'",
    )
    conn.commit()

    rows = load_forward_origins(
        conn, arms=arms, chain="solana", cohort_id=cohort, cutoff=cutoff,
    )

    assert [row["origin_event_id"] for row in rows] == ["LOW", "UNKNOWN"]
    assert rows[0]["source_size_usd"] == 499.0
    assert rows[0]["returns_pct"] == {"alpha_b": 5.0, "combo_f": 5.0}
    assert rows[1]["source_size_usd"] is None


def test_group_summary_counts_utc_days():
    """Two offset timestamps on one UTC day must count as one day."""
    from scripts.research.memecoin_source_size_forward_shadow import _group_summary

    rows = [
        {
            "mint": f"mint-{index}",
            "opened_at": opened_at,
            "returns_pct": {"alpha_b": 1.0},
        }
        for index, opened_at in enumerate((
            "2026-08-20T00:30:00+00:00",
            "2026-08-19T20:30:00-04:00",
        ))
    ]

    assert _group_summary(rows)["utc_days"] == 1


def test_summary_uses_the_fixed_floor_and_pre_registered_stop_rule():
    """A cohort cannot receive a verdict before both groups meet every sample floor."""
    from scripts.research.memecoin_source_size_forward_shadow import summarize_forward_origins

    start = datetime(2026, 8, 20, tzinfo=timezone.utc)
    rows = []
    for group, size, outcome in (("low", 499.0, -10.0), ("high", 500.0, 10.0)):
        for index in range(30):
            rows.append({
                "origin_event_id": f"{group}-{index}",
                "mint": f"{group}-mint-{index % 20}",
                "opened_at": (start + timedelta(days=index % 7)).isoformat(),
                "source_size_usd": size,
                "returns_pct": {"alpha_b": outcome, "combo_f": outcome / 2.0},
            })
    rows.append({
        "origin_event_id": "unknown", "mint": "unknown-mint",
        "opened_at": start.isoformat(), "source_size_usd": None,
        "returns_pct": {"alpha_b": 99.0, "combo_f": 99.0},
    })

    report = summarize_forward_origins(rows, bootstrap_iterations=200, seed=7)

    assert report["groups"]["below_500"]["origins"] == 30
    assert report["groups"]["at_least_500"]["origins"] == 30
    assert report["coverage"]["unknown_size"] == 1
    assert report["primary"]["median_delta_pct_points"] == 20.0
    assert report["primary"]["mint_bootstrap_95_pct_points"] == [20.0, 20.0]
    assert report["secondary"] == {
        "arm": "combo_f",
        "median_delta_pct_points": 10.0,
        "win_delta_pct_points": 100.0,
        "mint_bootstrap_95_pct_points": [10.0, 10.0],
    }
    assert report["status"] == "READY"
    assert report["verdict"] == "ESTABLISHED"

    collecting = summarize_forward_origins(rows[:-2], bootstrap_iterations=20, seed=7)
    assert collecting["groups"]["at_least_500"]["origins"] == 29
    assert collecting["status"] == "COLLECTING"
    assert collecting["verdict"] is None


def test_funded_actuals_are_descriptive_and_keep_unknown_size_separate():
    """Funded outcomes must not enter the paper verdict or hide unknown source sizes."""
    from scripts.research.memecoin_source_size_forward_shadow import (
        load_funded_actuals,
        summarize_funded_actuals,
    )

    conn = _conn()
    after = "2026-08-19T18:00:00+00:00"
    before = "2026-08-19T17:00:00+00:00"
    _real_position(conn, "LOW", 499.0, opened_at=after, pnl=-5.0)
    high_id = _real_position(conn, "HIGH", 500.0, opened_at=after, pnl=5.0)
    conn.execute(
        "UPDATE memecoin_positions SET their_size_usd=1.0 WHERE id=?",
        (high_id,),
    )
    conn.commit()
    _real_position(conn, "UNKNOWN", None, opened_at=after, pnl=7.0)
    _real_position(conn, "BEFORE", 900.0, opened_at=before, pnl=99.0)
    _real_position(
        conn, "DELAYED", 800.0, opened_at=after, pnl=99.0,
        event_created_at=before,
    )

    rows = load_funded_actuals(
        conn, chain="solana", cutoff="2026-08-19T17:33:35.131664+00:00",
    )
    report = summarize_funded_actuals(rows)

    assert {row["origin_event_id"] for row in rows} == {"LOW", "HIGH", "UNKNOWN"}
    assert next(row for row in rows if row["origin_event_id"] == "HIGH")[
        "source_size_usd"
    ] == 500.0
    assert report["unknown_size"] == 1
    assert report["groups"]["below_500"]["pnl_usd"] == -5.0
    assert report["groups"]["at_least_500"]["pnl_usd"] == 5.0
    assert "verdict" not in report


# Explicit legacy arithmetic diagnostics. Default publication rejection is tested
# separately by test_wallet_execution_publication and its inventory check.
import pytest as _pytest
from sniper.research.wallet_execution.publication import diagnostic_mode as _diagnostic_mode
@_pytest.fixture(autouse=True)
def _legacy_arithmetic_only():
    with _diagnostic_mode():
        yield
