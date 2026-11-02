"""The first real-close-anchored origin is chosen before paper outcome is known."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/research/refusal_reentry_cohort.py"


def test_late_close_and_later_paper_winner_cannot_replace_first_origin(tmp_path):
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE memecoin_entry_attempts (attempt_id TEXT, origin_event_id TEXT, "
        "network_id TEXT, mint TEXT, decision_started_at TEXT, terminal_status TEXT, "
        "terminal_reason TEXT, completed_at TEXT);"
        "CREATE TABLE memecoin_positions (id INTEGER, network_id TEXT, mint TEXT, "
        "origin_event_id TEXT, strategy TEXT, execution_mode TEXT, is_open INTEGER, "
        "opened_at TEXT, opened_recorded_at TEXT, closed_at TEXT, "
        "closed_recorded_at TEXT, realized_pnl REAL);"
    )
    day = "2026-09-25T"
    def stamp(hm):
        return day + hm + ":00+00:00"

    for pid, net, mint, recorded in (
        (1, "solana", "A", "00:01"), (2, "solana", "B", "00:15"),
        (3, "solana", "D", "00:01"), (4, "solana", "E", "00:01"),
        (6, "solana", "F", "00:01"),
        (7, "solana", "G", "00:01"),
        (5, "4663", "A", "00:01"),
    ):
        conn.execute("INSERT INTO memecoin_positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, net, mint, None, "alpha_b", "real", 0, stamp("00:00"),
                      stamp("00:00"), stamp("00:00"), stamp(recorded), -1.0))
    for aid, net, mint, origin, when in (
        ("a1", "solana", "A", "o1", "00:10"),
        ("a1-retry", "solana", "A", "o1", "00:11"),
        ("a2", "solana", "A", "o2", "00:20"),
        ("b1", "solana", "B", "o3", "00:10"),
        ("b2", "solana", "B", "o4", "00:20"),
        ("d1", "solana", "D", "o5", "00:10"),
        ("e1", "solana", "E", "o6", "00:10"),
        ("f1", "solana", "F", "o8", "00:10"),
        ("g1", "solana", "G", "o9", "00:10"),
        ("other-chain", "4663", "A", "o7", "00:10"),
    ):
        conn.execute("INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?,?,?)",
                     (aid, origin, net, mint, stamp(when), "refused", "not_approved",
                      stamp("02:30") if aid == "g1" else stamp(when)))
    for pid, mint, origin, is_open, pnl in (
        (10, "A", "o2", 0, 100.0),  # Cannot substitute for missing first origin o1.
        (11, "B", "o4", 0, 0.0),    # A measured zero is priced.
        (12, "D", "o5", 1, 10.0),   # Partial PnL on an open row is not priced.
        (13, "E", "o6", 0, None),   # A closed row with no PnL is unpriced.
        (14, "F", "o8", 0, 50.0),   # This row was recorded after the outcome cutoff.
    ):
        conn.execute("INSERT INTO memecoin_positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, "solana", mint, origin, "funded_clone", "paper", is_open,
                      stamp("00:21"), stamp("02:30") if mint == "F" else stamp("00:22"),
                      stamp("00:30") if not is_open else None,
                      (stamp("02:31") if mint == "F" else stamp("00:31")) if not is_open else None,
                      pnl))
    conn.executescript(
        "ALTER TABLE memecoin_positions ADD COLUMN exit_reason TEXT;"
        "ALTER TABLE memecoin_positions ADD COLUMN last_quote_price REAL;"
        "ALTER TABLE memecoin_positions ADD COLUMN entry_fill_source TEXT;"
        "CREATE TABLE memecoin_fills (position_id INTEGER, kind TEXT, fill_source TEXT, qty REAL);"
    )
    for pid in range(10, 15):
        conn.execute("INSERT INTO memecoin_fills VALUES(?,?,?,?)", (pid, "entry", "jupiter", 1))
        conn.execute("INSERT INTO memecoin_fills VALUES(?,?,?,?)", (pid, "mirror_exit", "jupiter", 1))
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--diagnostic-only", "--db", str(db), "--network-id", "solana",
         "--since", stamp("00:00"), "--until", stamp("02:00")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    expected = {
        "not_approved_attempts": 8, "clock_eligible_origins": 6, "first_coin_origins": 5,
        "paper_missing": 2, "paper_open": 1, "paper_unpriced": 1, "paper_priced": 1,
    }
    assert {key: report["counts"][key] for key in expected} == expected
    assert report["counts"]["late_close_censored"] == 1
    assert report["counts"]["late_attempt_censored"] == 1
    assert report["read_status"] == "not_estimable_n_lt_30"
    assert report["paper_pnl_sum_usd"] is None
    assert [(r["mint"], r["attempt_id"], r["paper_status"]) for r in report["rows"]] == [
        ("A", "a1", "missing"), ("B", "b2", "priced"),
        ("D", "d1", "open"), ("E", "e1", "unpriced"), ("F", "f1", "missing"),
    ]
    assert report["rows"][1]["prior_real_position_id"] == 2
    assert report["rows"][1]["paper_pnl_usd"] is None


@pytest.mark.parametrize("outcome,priced,censored", [
    ("noquote", 0, 30), ("partial_noquote", 0, 30), ("measured_zero", 30, 0),
    ("missing_fill", 0, 30), ("fomo_entry", 0, 30),
])
def test_only_complete_measured_outcomes_open_read_gate(outcome, priced, censored):
    from scripts.research.refusal_reentry_cohort import build_report
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    def stamp(t):
        return f"2026-09-25T{t}:00+00:00"
    for i in range(30):
        mint, origin = f"M{i}", f"O{i}"
        conn.execute("INSERT INTO memecoin_entry_attempts "
                     "(attempt_id, origin_event_id, network_id, mint, decision_started_at, "
                     "completed_at, terminal_status, terminal_reason) VALUES (?,?,?,?,?,?,?,?)",
                     (f"a{i}", origin, "solana", mint, stamp("00:10"), stamp("00:10"),
                      "refused", "not_approved"))
        conn.execute("INSERT INTO memecoin_positions "
                     "(id, network_id, mint, execution_mode, is_open, closed_at, closed_recorded_at) "
                     "VALUES (?,?,?,'real',0,?,?)", (i+1, "solana", mint, stamp("00:00"), stamp("00:01")))
        pid = i + 101
        conn.execute("INSERT INTO memecoin_positions "
                     "(id, network_id, mint, origin_event_id, strategy, execution_mode, is_open, "
                     "opened_at, opened_recorded_at, closed_at, closed_recorded_at, realized_pnl, "
                     "exit_reason, entry_fill_source) VALUES (?,?,?,?,?,'paper',0,?,?,?,?,0,?,?)",
                     (pid, "solana", mint, origin, "funded_clone", stamp("00:11"), stamp("00:11"),
                      stamp("00:30"), stamp("00:31"), "mirror_exit",
                      "fomo" if outcome == "fomo_entry" else "jupiter"))
        if outcome == "missing_fill":
            continue
        conn.execute("INSERT INTO memecoin_fills(position_id,kind,fill_source,qty) VALUES(?,?,?,?)",
                     (pid, "entry", "jupiter", 1))
        conn.execute("INSERT INTO memecoin_fills(position_id,kind,fill_source,qty) VALUES(?,?,?,?)",
                     (pid, "mirror_exit", "noquote" if "noquote" in outcome else "jupiter", 1))
        if outcome == "partial_noquote":
            conn.execute("INSERT INTO memecoin_fills(position_id,kind,fill_source,qty) VALUES(?,?,?,?)",
                         (pid, "derisk", "jupiter", 0.5))
    report = build_report(conn, network_id="solana", since=stamp("00:00"), until=stamp("02:00"))
    assert report["counts"]["paper_priced"] == priced
    assert report["counts"]["paper_censored"] == censored
    assert report["paper_pnl_median_usd"] == (0 if priced else None)
    assert report["read_status"] == ("diagnostic_paper_model" if priced else "not_estimable_n_lt_30")
    conn.close()


# Explicit legacy arithmetic diagnostics. Default publication rejection is tested
# separately by test_wallet_execution_publication and its inventory check.
import pytest as _pytest
from sniper.research.wallet_execution.publication import diagnostic_mode as _diagnostic_mode
@_pytest.fixture(autouse=True)
def _legacy_arithmetic_only():
    with _diagnostic_mode():
        yield
