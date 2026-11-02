"""Offline alias repair must never change wallet authority or accept an identity collision."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg

UID = "b7e0a1f4-266d-5b9a-befd-df821836e2c5"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_WALLETS", ())
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_ONLY_WALLETS", ())
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE memecoin_wallets (
            id INTEGER PRIMARY KEY, source_user_id TEXT UNIQUE, handle TEXT, label TEXT,
            paused INTEGER, added_at TEXT, source TEXT, discovery_source TEXT,
            excluded INTEGER, real_approved INTEGER, real_chains TEXT, future_metadata TEXT);
        INSERT INTO memecoin_wallets VALUES (
            288, 'b7e0a1f4-266d-5b9a-befd-df821836e2c5', 'yayayayayey', 'operator label',
            1, '2026-08-12T18:13:40Z', 'follow', 'large_buy', 1, 1,
            '4663,5042,solana', 'keep future fields too');
    """)
    yield c
    c.close()


def _row(conn):
    return dict(conn.execute("SELECT * FROM memecoin_wallets WHERE source_user_id=?", (UID,)).fetchone())


def _rename(conn, **kw):
    return m.rename_wallet_handle(conn, UID, expected_handle=kw.get("old", "yayayayayey"),
                                  new_handle=kw.get("new", "yansanity"))


def test_exact_uid_rename_preserves_every_state_and_metadata_column(conn):
    before = _row(conn)
    assert _rename(conn) == {"status": "renamed", "source_user_id": UID,
                             "old_handle": "yayayayayey", "handle": "yansanity"}
    assert _row(conn) == dict(before, handle="yansanity")
    assert not conn.in_transaction


def test_repeat_is_unchanged_and_never_inserts_a_duplicate_uid(conn):
    _rename(conn)
    before = _row(conn)
    assert _rename(conn)["status"] == "unchanged"
    assert _row(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM memecoin_wallets").fetchone()[0] == 1


@pytest.mark.parametrize("old,new", [("", "yansanity"), (None, "yansanity"),
                                     ("yayayayayey", ""), ("yayayayayey", "  "),
                                     ("yayayayayey", None)])
def test_blank_input_refuses_without_any_change(conn, old, new):
    before = _row(conn)
    assert _rename(conn, old=old, new=new) == {"status": "refused", "reason": "blank_identity"}
    assert _row(conn) == before


def test_unexpected_old_name_refuses_instead_of_overwriting_another_refresh(conn):
    conn.execute("UPDATE memecoin_wallets SET handle='newer_alias' WHERE source_user_id=?", (UID,))
    conn.commit()
    before = _row(conn)
    assert _rename(conn) == {"status": "refused", "reason": "unexpected_handle"}
    assert _row(conn) == before


def test_case_insensitive_collision_with_any_other_uid_refuses(conn):
    conn.execute("INSERT INTO memecoin_wallets(source_user_id,handle,paused,excluded) "
                 "VALUES ('other-uid',' YaNsAnItY ',1,1)")
    conn.commit()
    before = _row(conn)
    assert _rename(conn) == {"status": "refused", "reason": "handle_collision"}
    assert _row(conn) == before


def test_missing_uid_cannot_add_or_arm_a_wallet(conn):
    assert m.rename_wallet_handle(conn, "missing", expected_handle="old", new_handle="new") == {
        "status": "refused", "reason": "wallet_not_found"}
    assert conn.execute("SELECT COUNT(*) FROM memecoin_wallets").fetchone()[0] == 1


def test_caller_transaction_is_never_committed_or_rolled_back(conn):
    conn.execute("UPDATE memecoin_wallets SET label='uncommitted unrelated work'")
    with pytest.raises(ValueError, match="transaction_active"):
        _rename(conn)
    assert conn.in_transaction
    conn.rollback()
    assert _row(conn)["label"] == "operator label"


def test_writer_contention_refuses_without_changing_the_target(conn, tmp_path):
    path = tmp_path / "locked.db"
    with sqlite3.connect(path) as target:
        conn.backup(target)
    holder = sqlite3.connect(path)
    contender = sqlite3.connect(path, timeout=0.2)
    contender.row_factory = sqlite3.Row
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _rename(contender)
        assert not contender.in_transaction
        assert _row(contender) == _row(conn)
    finally:
        holder.rollback()
        holder.close()
        contender.close()


@pytest.mark.parametrize("setting,entry", [
    ("MEMECOIN_FOLLOW3050_WALLETS", "yayayayayey:solana"),
    ("MEMECOIN_FOLLOW3050_WALLETS", "yansanity:4663"),
    ("MEMECOIN_REAL_TP_ONLY_WALLETS", "YANSANITY:base"),
    ("MEMECOIN_REAL_TP_ONLY_WALLETS", "yayayayayey:solana"),
])
def test_handle_bound_exit_policy_requires_separate_alias_policy_work(conn, monkeypatch, setting, entry):
    monkeypatch.setattr(cfg, setting, (entry,))
    before = _row(conn)
    assert _rename(conn) == {"status": "refused", "reason": "handle_policy_bound"}
    assert _row(conn) == before


def test_preview_validates_on_readonly_connection_without_changing_a_handle(conn, tmp_path):
    path = tmp_path / "wallets.db"
    with sqlite3.connect(path) as target:
        conn.backup(target)
    from sniper.persistence.db_core import get_readonly_connection
    with get_readonly_connection(str(path)) as ro:
        before = _row(ro)
        result = m.rename_wallet_handle(ro, UID, expected_handle="yayayayayey",
                                       new_handle="yansanity", dry_run=True)
        assert result["status"] == "planned"
        assert _row(ro) == before


def _script_fixture(conn, tmp_path, *, evidence_uid=UID):
    conn.executescript("""
        CREATE TABLE memecoin_feed_observations_v2 (
            id INTEGER PRIMARY KEY, upstream_event_id TEXT, source_user_id TEXT,
            source_handle TEXT, normalized_side TEXT, source_ts TEXT, raw_json TEXT);
    """)
    conn.execute("INSERT INTO memecoin_feed_observations_v2 VALUES (1,'swap:source',?,?,"
                 "'buy','2026-09-15T13:35:13Z',?)",
                 (UID, "yansanity", json.dumps({"signal_source": "follow_activity"})))
    conn.commit()
    path = tmp_path / "wallets.db"
    with sqlite3.connect(path) as target:
        conn.backup(target)
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"alias_change": {
        "source_user_id": evidence_uid, "expected_handle": "yayayayayey",
        "new_handle": "yansanity", "observation_ids": [1]}}))
    return path, evidence


def _run_script(path, evidence, *args):
    script = Path(__file__).resolve().parents[1] / "scripts/research/repair_wallet_alias.py"
    return subprocess.run([sys.executable, str(script), "--db", str(path),
                           "--evidence", str(evidence), *args], text=True,
                          capture_output=True, timeout=15)


def test_operator_command_defaults_to_readonly_plan(conn, tmp_path):
    path, evidence = _script_fixture(conn, tmp_path)
    result = _run_script(path, evidence)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["change"]["status"] == "planned"
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT handle FROM memecoin_wallets").fetchone()[0] == "yayayayayey"


def test_operator_command_apply_requires_live_same_uid_provenance_and_keeps_receipt(conn, tmp_path):
    path, evidence = _script_fixture(conn, tmp_path)
    receipt = tmp_path / "receipt.json"
    result = _run_script(path, evidence, "--apply", "--receipt", str(receipt))
    assert result.returncode == 0, result.stderr
    saved = json.loads(receipt.read_text())
    assert saved["change"]["status"] == "renamed"
    assert saved["after"] == dict(saved["before"], handle="yansanity")
    assert saved["provenance"]["observation_ids"] == [1]


def test_wrong_uid_provenance_refuses_without_a_write(conn, tmp_path):
    path, evidence = _script_fixture(conn, tmp_path, evidence_uid="another-uid")
    result = _run_script(path, evidence, "--apply", "--receipt", str(tmp_path / "receipt.json"))
    assert result.returncode == 1
    assert json.loads(result.stdout)["change"]["reason"] == "alias_evidence_mismatch"
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT handle FROM memecoin_wallets").fetchone()[0] == "yayayayayey"


def test_receipt_cannot_replace_database_or_evidence(conn, tmp_path):
    path, evidence = _script_fixture(conn, tmp_path)
    for protected in (path, evidence):
        result = _run_script(path, evidence, "--apply", "--receipt", str(protected))
        assert result.returncode == 1
        assert json.loads(result.stdout)["change"]["reason"] == "receipt_path_conflict"
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT handle FROM memecoin_wallets").fetchone()[0] == "yayayayayey"


def test_receipt_failure_after_commit_reports_actual_rename(conn, tmp_path, monkeypatch, capsys):
    path, evidence = _script_fixture(conn, tmp_path)
    import importlib.util
    from sniper.util import atomic
    script = Path(__file__).resolve().parents[1] / "scripts/research/repair_wallet_alias.py"
    spec = importlib.util.spec_from_file_location("repair_wallet_alias", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    real_write = atomic.atomic_write_json
    writes = []

    def fail_final_receipt(*args, **kw):
        writes.append(args[0])
        if len(writes) == 2:
            raise OSError("receipt storage unavailable")
        return real_write(*args, **kw)

    monkeypatch.setattr(atomic, "atomic_write_json", fail_final_receipt)
    result = module.main(["--db", str(path), "--evidence", str(evidence), "--apply",
                          "--receipt", str(tmp_path / "receipt.json")])
    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["change"]["status"] == "renamed"
    assert report["application_state"] == "receipt_write_failed_after_commit"
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT handle FROM memecoin_wallets").fetchone()[0] == "yansanity"


def test_read_failure_after_commit_cannot_report_refused(conn, tmp_path, monkeypatch, capsys):
    path, evidence = _script_fixture(conn, tmp_path)
    import importlib.util
    script = Path(__file__).resolve().parents[1] / "scripts/research/repair_wallet_alias.py"
    spec = importlib.util.spec_from_file_location("repair_wallet_alias_post_read", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    real_connect = sqlite3.connect

    class FailAfterRead(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith("SELECT * FROM memecoin_wallets"):
                raise sqlite3.OperationalError("post-commit read unavailable")
            return super().execute(sql, parameters)

    def connect(*args, **kw):
        if args and str(args[0]).endswith("?mode=rw"):
            kw["factory"] = FailAfterRead
        return real_connect(*args, **kw)

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    result = module.main(["--db", str(path), "--evidence", str(evidence), "--apply",
                          "--receipt", str(tmp_path / "receipt.json")])
    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["change"]["status"] == "renamed"
    assert report["application_state"] == "verification_failed_after_commit"
    with real_connect(path) as check:
        assert check.execute("SELECT handle FROM memecoin_wallets").fetchone()[0] == "yansanity"
