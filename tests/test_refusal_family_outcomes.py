"""A refusal family with no future observation must not report a priced zero.

`refusal_family_outcomes.py` censors an individual horizon that resolves to the baseline
observation, but the aggregate then divided by an empty list and printed +0.0 for the mean and
+0 for the $100 sum. A family nobody observed after the refusal is unknown, not flat.

Offline: a temporary database and captured stdout. No production read, no provider.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys

import pytest

from scripts.research import refusal_family_outcomes as rf


def _db(path, *, quotes):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE memecoin_entry_attempts(network_id TEXT, mint TEXT, ticker TEXT,"
        " decision_started_at TEXT, terminal_reason TEXT, terminal_status TEXT);"
        "CREATE TABLE memecoin_quote_ledger(network_id TEXT, mint TEXT, ts TEXT, price REAL);"
        "CREATE TABLE memecoin_positions(network_id TEXT, realized_pnl REAL, execution_mode TEXT,"
        " is_open INTEGER, opened_at TEXT);"
    )
    stamp = dt.datetime.now(dt.timezone.utc)
    conn.execute("INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?)",
                 ("4663", "M", "M", stamp.isoformat(), "quiet hours", "refused"))
    for offset_s, price in quotes:
        conn.execute("INSERT INTO memecoin_quote_ledger VALUES(?,?,?,?)",
                     ("4663", "M", (stamp + dt.timedelta(seconds=offset_s)).isoformat(), price))
    conn.commit()
    conn.close()
    return path


def _run(monkeypatch, path):
    monkeypatch.setattr(sys, "argv", ["refusal_family_outcomes.py", "--db", str(path), "--min-n", "1"])
    assert rf.main() == 0


def test_a_baseline_only_family_reports_no_mean_and_no_sum(monkeypatch, tmp_path, capsys):
    """The only observation is the one the refusal is priced against: nothing follows it."""
    _run(monkeypatch, _db(tmp_path / "only-baseline.db", quotes=[(0, 1.0)]))
    out = capsys.readouterr().out

    assert "+0.0" not in out, "an unobserved horizon must not print as a flat return"
    row = next(line for line in out.splitlines() if line.startswith("other: quiet hours"))
    assert row.split()[-1] == "-", "the $100 sum of nothing is unknown, not zero"


def test_the_measured_count_per_horizon_is_reported(monkeypatch, tmp_path, capsys):
    """A zero with observations behind it must be distinguishable from a zero without."""
    _run(monkeypatch, _db(tmp_path / "observed.db", quotes=[(0, 1.0), (3600, 1.5)]))
    out = capsys.readouterr().out

    assert "n@60" in out, "the horizon's measured count belongs in the table"
    row = next(line for line in out.splitlines() if line.startswith("other: quiet hours"))
    assert "+50" in row, "the observed return is reported"
    assert row.split()[-1] == "+50", "and it sums into the $100 column"


def test_an_unobserved_family_and_an_observed_zero_do_not_print_the_same(monkeypatch, tmp_path,
                                                                        capsys):
    _run(monkeypatch, _db(tmp_path / "flat.db", quotes=[(0, 1.0), (3600, 1.0)]))
    observed = capsys.readouterr().out
    _run(monkeypatch, _db(tmp_path / "absent.db", quotes=[(0, 1.0)]))
    absent = capsys.readouterr().out

    def row(text):
        return next(line for line in text.splitlines() if line.startswith("other: quiet hours"))

    assert row(observed) != row(absent)
    assert row(observed).split()[-1] == "+0", "a genuinely flat hour sums to zero"
    assert row(absent).split()[-1] == "-", "an unobserved hour sums to nothing at all"


# Explicit legacy arithmetic diagnostics. Default publication rejection is tested
# separately by test_wallet_execution_publication and its inventory check.
import pytest as _pytest
from sniper.research.wallet_execution.publication import diagnostic_mode as _diagnostic_mode
@_pytest.fixture(autouse=True)
def _legacy_arithmetic_only():
    with _diagnostic_mode():
        yield
