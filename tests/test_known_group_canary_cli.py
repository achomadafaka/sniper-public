"""Operator CLI authority boundaries against an explicitly isolated temporary ledger."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as db


def cli():
    path=Path(__file__).resolve().parents[1]/'scripts/memecoin_known_group_canary.py'
    spec=importlib.util.spec_from_file_location('known_group_cli_acceptance',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def temporary_ledger(tmp_path):
    path=tmp_path/'isolated-funded-test.db'
    with sqlite3.connect(path) as conn:db.ensure_memecoin_tables(conn)
    return path


def refusal(path,cohort):
    with sqlite3.connect(path) as conn:
        conn.row_factory=sqlite3.Row
        return db.desk_cohort_refusal(conn,cohort_id=cohort,producer='H')


def test_default_policy_display_and_registration_never_enable_admission(tmp_path,capsys):
    """Spec69-80,160-177: default-OFF setup and display confer no funded authority."""
    command=cli();path=temporary_ledger(tmp_path);before=path.read_bytes()
    assert command.main(['--funded-db',str(path)])==0
    assert 'Disabled' in capsys.readouterr().out and path.read_bytes()==before
    assert command.main(['--show-policy'])==0
    shown=json.loads(capsys.readouterr().out)
    assert shown['admission_default'] is False and path.read_bytes()==before
    assert command.main(['--register','--funded-db',str(path)])==0
    registered=json.loads(capsys.readouterr().out)
    assert registered['policy_hash']==shown['policy_hash'] and registered['admission_refusal']
    cohort=registered['cohort'];assert refusal(path,cohort)
    frozen=path.read_bytes()
    assert command.main(['--funded-db',str(path)])==0
    capsys.readouterr()
    assert command.main(['--show-policy'])==0
    assert json.loads(capsys.readouterr().out)['admission_default'] is False
    assert refusal(path,cohort) and path.read_bytes()==frozen


def test_cli_enable_requires_confirmation_of_exact_policy_on_explicit_temporary_ledger(tmp_path,capsys):
    """Spec75-80,173-177: missing confirmation and wrong policy refuse before authority mutation."""
    command=cli();path=temporary_ledger(tmp_path)
    assert command.main(['--register','--funded-db',str(path)])==0
    registered=json.loads(capsys.readouterr().out);cohort=registered['cohort'];digest=registered['policy_hash']
    args=['--enable-admission','--funded-db',str(path)]
    before=path.read_bytes()
    for options in [['--policy-hash',digest],['--confirm','--policy-hash','f'*64]]:
        with pytest.raises(SystemExit) as error:command.main(args+options)
        assert error.value.code==2 and 'exact --policy-hash' in capsys.readouterr().err
        assert refusal(path,cohort) and path.read_bytes()==before
    assert command.main(args+['--confirm','--policy-hash',digest])==0
    enabled=json.loads(capsys.readouterr().out)
    assert enabled['cohort']==cohort and enabled['policy_hash']==digest and enabled['admission_refusal'] is None
    assert refusal(path,cohort) is None
