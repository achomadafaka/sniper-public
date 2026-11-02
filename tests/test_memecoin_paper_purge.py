"""Phase 1 of spec 2026-07-30: the destructive paper-ledger purge."""
import sqlite3

import pytest

import sniper.persistence.db_memecoin as m
from scripts.purge_memecoin_paper_ledger import purge_paper_ledger


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _seed(c):
    c.execute("INSERT INTO memecoin_wallets(source_user_id,handle,real_approved) VALUES('u1','h1',1)")
    for pid, mode in ((1, "paper"), (2, "paper"), (3, "real")):
        c.execute(
            "INSERT INTO memecoin_positions(id,source_user_id,mint,network_id,execution_mode,"
            "is_open,strategy) VALUES(?,'u1','mint','solana',?,0,'control')", (pid, mode))
        c.execute("INSERT INTO memecoin_fills(position_id,fill_source,kind)"
                  " VALUES(?,'dexscreener','buy')", (pid,))
    c.commit()


def test_purge_deletes_paper_keeps_real_and_roster():
    c = _conn()
    _seed(c)
    out = purge_paper_ledger(c)
    assert out["positions"] == 2
    assert out["fills"] == 2
    assert [r[0] for r in c.execute("SELECT execution_mode FROM memecoin_positions")] == ["real"]
    assert c.execute("SELECT COUNT(*) FROM memecoin_wallets").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM memecoin_fills").fetchone()[0] == 1


def test_purge_preserves_autoincrement_sequence():
    """Resetting sqlite_sequence would restart ids below the V6 cursor and kill admission."""
    c = _conn()
    _seed(c)
    before = c.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='memecoin_positions'").fetchone()[0]
    out = purge_paper_ledger(c)
    after = c.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='memecoin_positions'").fetchone()[0]
    assert after == before == out["seq_after"]


def test_purge_keeps_event_level_records():
    """Entry snapshots are keyed by event and log skips too — they are the signal decision
    record, the same class as memecoin_events, and are not paper results."""
    c = _conn()
    _seed(c)
    c.execute("INSERT INTO memecoin_entry_snapshots(upstream_event_id,mint,action)"
              " VALUES('e1','mint','skipped_chase')")
    c.commit()
    purge_paper_ledger(c)
    assert c.execute("SELECT COUNT(*) FROM memecoin_entry_snapshots").fetchone()[0] == 1


def test_dry_run_deletes_nothing():
    c = _conn()
    _seed(c)
    out = purge_paper_ledger(c, dry_run=True)
    assert out["positions"] == 2
    assert c.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 3


def test_purge_refuses_when_a_real_position_is_open():
    c = _conn()
    _seed(c)
    c.execute("UPDATE memecoin_positions SET is_open=1 WHERE execution_mode='real'")
    c.commit()
    with pytest.raises(RuntimeError, match="real position"):
        purge_paper_ledger(c)
    assert c.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 3


def test_purge_sweeps_every_dependent_table():
    """memecoin_pnl_repairs was missed and left 17 rows pointing at deleted positions."""
    c = _conn()
    _seed(c)
    # Not created by ensure_memecoin_tables — it exists only where a repair has run.
    c.execute("CREATE TABLE memecoin_pnl_repairs (id INTEGER PRIMARY KEY, position_id INTEGER,"
              " reason TEXT, created_at TEXT)")
    c.execute("INSERT INTO memecoin_pnl_repairs(position_id,reason,created_at)"
              " VALUES(1,'t','2026-07-30T00:00:00+00:00')")
    c.commit()
    purge_paper_ledger(c)
    assert c.execute("SELECT COUNT(*) FROM memecoin_pnl_repairs WHERE position_id NOT IN "
                     "(SELECT id FROM memecoin_positions)").fetchone()[0] == 0


def test_failed_postcondition_rolls_the_purge_back(monkeypatch):
    """Asserts ran AFTER commit, so a tripped one could not undo anything."""
    import scripts.purge_memecoin_paper_ledger as mod

    c = _conn()
    _seed(c)
    monkeypatch.setattr(mod, "_orphan_count",
                        lambda conn: 0 if conn is None else 99)
    with pytest.raises(RuntimeError, match="orphaned"):
        purge_paper_ledger(c)
    assert c.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 3
