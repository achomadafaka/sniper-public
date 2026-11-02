"""Slice D of docs/superpowers/specs/2026-09-30-memecoin-funded-executable-extremes-design.md: the
operator's history script. It backfills the extremes rows the in-runner pass never reached (closes
older than 24 hours) and clears the display-derived columns on closed funded rows, with a backup
and an undo. Dry run by default; the operator runs --confirm.
"""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m

ROOT = Path(__file__).resolve().parent.parent
SERIES = [("2026-07-09T11:00:02+00:00", 1.40), ("2026-07-09T11:00:04+00:00", 0.60)]


def _script():
    spec = importlib.util.spec_from_file_location(
        "exec_extremes_history", ROOT / "scripts" / "memecoin_exec_extremes_history.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _open(c, mint, mode="real"):
    return m.open_position(c, source_user_id="u-" + mint, source_handle="h", mint=mint, ticker="T",
                           entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                           entry_lag_s=1.0, their_size_usd=0.0, opened_at="2026-07-09T11:00:00+00:00",
                           strategy="alpha_b", execution_mode=mode, network_id="solana")


@pytest.fixture
def ledger(tmp_path):
    """A: funded, closed, series, display value.  B: funded, closed, series, no display value.
    C: funded, closed, NO series, display value.  D: funded, OPEN, display value.
    E: paper, closed, series, display value."""
    path = tmp_path / "ledger.db"
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    ids = {name: _open(c, name, "paper" if name == "E" else "real") for name in "ABCDE"}
    for name in "ABE":
        m.record_route_series(c, [
            {"position_id": ids[name], "mint": name, "network_id": "solana", "seam": "exit_decision",
             "observed_at": at, "quoted_at": at, "expected_usdc": price * 10.0, "open_qty": 10.0,
             "unit_price": price} for at, price in SERIES])
    for name, gain in (("A", 33.0), ("C", 44.0), ("D", 55.0), ("E", 66.0)):
        c.execute("UPDATE memecoin_positions SET max_gain_pct=?, time_to_peak_s=? WHERE id=?",
                  (gain, gain / 11.0, ids[name]))
    for name in "ABCE":
        c.execute("UPDATE memecoin_positions SET is_open=0, closed_at='2026-07-01T00:00:00+00:00', "
                  "realized_pnl=0 WHERE id=?", (ids[name],))
    c.commit()
    c.close()
    return path, ids


def _state(path):
    c = sqlite3.connect(path)
    try:
        return {
            "display": {r[0]: (r[1], r[2]) for r in c.execute(
                "SELECT id, max_gain_pct, time_to_peak_s FROM memecoin_positions")},
            "extremes": {r[0] for r in c.execute("SELECT position_id FROM memecoin_position_exec_extremes")},
            "backup": {r[0]: (r[1], r[2]) for r in c.execute(
                "SELECT position_id, old_max_gain_pct, old_time_to_peak_s "
                "FROM memecoin_position_display_extremes_backup")},
        }
    finally:
        c.close()


def test_a_dry_run_prints_the_counts_and_writes_nothing(ledger, capsys):
    path, ids = ledger
    before = _state(path)
    assert _script().main(["--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "closed funded positions: 3" in out
    assert "extremes rows: 0 present, 2 to write" in out
    assert "no positive-price series: 1" in out
    assert "display values: 2 to back up and clear, 0 backed up earlier" in out
    assert "DRY RUN" in out and _state(path) == before


def test_confirm_backfills_backs_up_then_clears_and_only_closed_funded_rows(ledger, capsys):
    path, ids = ledger
    assert _script().main(["--db", str(path), "--confirm", "--batch", "1"]) == 0
    out = capsys.readouterr().out
    assert "wrote 2 extremes rows" in out and "backed up 2, cleared 2" in out
    state = _state(path)
    assert state["extremes"] == {ids["A"], ids["B"]}
    assert state["backup"] == {ids["A"]: (33.0, 3.0), ids["C"]: (44.0, 4.0)}
    assert state["display"][ids["A"]] == state["display"][ids["C"]] == (None, None)
    assert state["display"][ids["D"]] == (55.0, 5.0)          # open: untouched
    assert state["display"][ids["E"]] == (66.0, 6.0)          # paper: untouched


def test_a_second_confirm_changes_nothing_and_the_backup_keeps_the_display_values(ledger, capsys):
    path, ids = ledger
    script = _script()
    assert script.main(["--db", str(path), "--confirm"]) == 0
    first = _state(path)
    capsys.readouterr()
    assert script.main(["--db", str(path), "--confirm"]) == 0
    out = capsys.readouterr().out
    assert "extremes rows: 2 present, 0 to write" in out
    assert "display values: 0 to back up and clear, 2 backed up earlier" in out
    assert "wrote 0 extremes rows" in out and "backed up 0, cleared 0" in out
    assert _state(path) == first


def test_restore_puts_every_backed_up_value_back_and_keeps_the_backup(ledger, capsys):
    path, ids = ledger
    script = _script()
    assert script.main(["--db", str(path), "--confirm"]) == 0
    cleared = _state(path)
    assert script.main(["--db", str(path), "--restore"]) == 0          # a dry run of the undo
    assert "DRY RUN" in capsys.readouterr().out and _state(path) == cleared
    assert script.main(["--db", str(path), "--restore", "--confirm"]) == 0
    assert "restored 2" in capsys.readouterr().out
    state = _state(path)
    assert state["display"][ids["A"]] == (33.0, 3.0) and state["display"][ids["C"]] == (44.0, 4.0)
    assert state["backup"] == cleared["backup"] and state["extremes"] == cleared["extremes"]
    # and the clear can run again; the first backup copy stays
    assert script.main(["--db", str(path), "--confirm"]) == 0
    assert "backed up 0, cleared 2" in capsys.readouterr().out
    assert _state(path) == cleared


def test_a_ledger_without_the_tables_is_refused_with_the_reason(tmp_path, capsys):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, execution_mode TEXT, "
              "is_open INTEGER, max_gain_pct REAL, time_to_peak_s REAL)")
    c.commit()
    c.close()
    assert _script().main(["--db", str(path), "--confirm"]) == 2
    assert "deploy the runner release that creates them first" in capsys.readouterr().out
