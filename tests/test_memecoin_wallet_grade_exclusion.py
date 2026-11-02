"""A system-fault close is kept off the copied wallet's funded grade (operator 2026-10-02, DAODUN
68544), while every money total still counts it."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m

UID = "u-sneakky"


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    for pid, pnl, closed, is_open, mode in ((1, 31.55, "2026-09-30T15:03:35+00:00", 0, "real"),
                                            (2, -45.74, "2026-10-02T15:17:15+00:00", 0, "real"),
                                            (3, None, None, 1, "real"),
                                            (4, -5.0, "2026-10-02T10:00:00+00:00", 0, "paper")):
        c.execute("INSERT INTO memecoin_positions (id, source_user_id, source_handle, network_id, mint, "
                  "ticker, entry_price, entry_notional, is_open, opened_at, closed_at, realized_pnl, "
                  "execution_mode, signal_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (pid, UID, "0xsneakky", "56", "0xmint%d" % pid, "T%d" % pid, 1e-5, 90.0, is_open,
                   "2026-09-30T00:00:00+00:00", closed, pnl, mode, "onchain_watch"))
    c.commit()
    return c


def test_an_excluded_close_leaves_the_demote_record_and_the_board():
    c = _db()
    assert m.wallet_real_record(c, UID, network_id="56")["realized_total"] == pytest.approx(-14.19)
    m.exclude_from_wallet_grade(c, 2, reason="no usable exit route (our fault)", actor="operator")
    rec = m.wallet_real_record(c, UID, network_id="56")
    assert rec == {"realized_total": 31.55, "consecutive_losses": 0, "closed_count": 1}
    board = m.real_stats_by_wallet(c, "bsc")[UID]
    assert board["realized_pnl"] == pytest.approx(31.55) and board["closed_count"] == 1


def test_money_totals_still_count_the_excluded_loss():
    c = _db()
    m.exclude_from_wallet_grade(c, 2, reason="our fault", actor="operator")
    total = c.execute("SELECT SUM(realized_pnl) FROM memecoin_positions WHERE is_open=0 AND "
                      + m.real_money_sql()).fetchone()[0]
    assert total == pytest.approx(31.55 - 45.74)


@pytest.mark.parametrize("pid,why", [(3, "still open"), (4, "not a funded"), (99, "no position")])
def test_only_a_closed_funded_position_can_be_excluded(pid, why):
    c = _db()
    with pytest.raises(ValueError, match=why):
        m.exclude_from_wallet_grade(c, pid, reason="x", actor="operator")


def test_a_reason_is_required():
    with pytest.raises(ValueError, match="reason"):
        m.exclude_from_wallet_grade(_db(), 2, reason="  ", actor="operator")


def test_an_exclusion_is_never_rewritten():
    c = _db()
    m.exclude_from_wallet_grade(c, 2, reason="first", actor="operator")
    with pytest.raises(ValueError, match="already excluded"):
        m.exclude_from_wallet_grade(c, 2, reason="second", actor="someone")
    assert c.execute("SELECT reason FROM memecoin_wallet_grade_exclusions").fetchone()[0] == "first"
    assert not c.in_transaction


def test_the_cli_dry_run_and_confirm_on_a_file_ledger(tmp_path, capsys):
    """The CLI's own connection (row_factory) is exercised, not only the helper (2026-10-02 slip)."""
    import importlib.util, pathlib
    db = tmp_path / "ledger.db"
    src = _db()
    # a revoke then a re-arm on 56 makes wallet_real_cohort_start read a row BY NAME (the live case)
    for i, action in ((1, "chain_revoke"), (2, "chain_arm")):
        src.execute("INSERT INTO memecoin_wallet_approvals (id, source_user_id, action, actor, created_at, "
                    "chain) VALUES (?,?,?,?,?,?)", (i, UID, action, "operator", "2026-09-29T00:00:00+00:00", "56"))
    src.commit()
    disk = sqlite3.connect(db)
    src.backup(disk)
    disk.close()
    spec = importlib.util.spec_from_file_location(
        "excl_cli", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "memecoin_wallet_grade_exclude.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(["2", "--reason", "our fault", "--db", str(db)]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert cli.main(["2", "--reason", "our fault", "--db", str(db), "--confirm"]) == 0
    assert "EXCLUDED" in capsys.readouterr().out


def test_the_cli_dry_run_writes_nothing_on_a_pre_migration_ledger(tmp_path, capsys):
    # Codex W10: the dry run created the exclusions table and committed before saying "nothing written".
    import hashlib, importlib.util, pathlib
    db = tmp_path / "ledger.db"
    src = _db()
    src.execute("DROP TABLE IF EXISTS memecoin_wallet_grade_exclusions")
    src.commit()
    disk = sqlite3.connect(db)
    src.backup(disk)
    disk.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location(
        "excl_cli_w10", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "memecoin_wallet_grade_exclude.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(["2", "--reason", "our fault", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "not created yet" in out
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not sqlite3.connect(db).execute(
        "SELECT 1 FROM sqlite_master WHERE name='memecoin_wallet_grade_exclusions'").fetchone()
