"""Operator CLI for the eval-1 open on synthetic DBs: inspect, dry-run, apply (Office + ledger stage), refusals."""
import hashlib
import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path

from sniper.office import eval_transition as X, pilot_seed as P
from .test_eval_transition import disc2, BP  # noqa: F401
from .test_epoch_transition import prepared  # noqa: F401
from .test_pilot_seed import env  # noqa: F401

REPO = Path(__file__).resolve().parents[2]


def _release(tmp_path):
    release = tmp_path / 'release'
    shutil.copytree(REPO / 'config/office', release / 'config/office')
    office = release / 'config/office'
    profile = office / 'prompts' / X.NEW
    approval = json.loads((profile / 'sha256.json').read_text())
    pins = {name: hashlib.sha256((office / name).read_bytes()).hexdigest() for name in P.EVAL1_FILES}
    approval.update(approval_status='approved_alpha', source_pins=pins)
    (profile / 'sha256.json').write_text(json.dumps(approval))
    return release, office / 'pilot_models_eval1.json'


def _desk(tmp_path):
    from sniper.persistence import db_memecoin_desk as D
    conn = D.connect(tmp_path / 'desk.db')
    D.register_cohort(conn, cohort_id='eval1-core', producer='U', prompt_version='fixture',
                      policy={'hard_filter': {'intake': {'require_collected': ['chart', 'holders']}}}, created_at='2026-10-29T00:00:00Z')
    conn.close()
    return tmp_path / 'desk.db'


def test_cli_inspect_dry_run_apply_and_ledger_stage(disc2, tmp_path, capsys, monkeypatch):
    conn, ledger, m, rec = disc2
    monkeypatch.setattr(X, 'PREREG', 'docs/audits/2026-10-29-office-paper-eval-1-preregistration.md')
    release, models = _release(tmp_path)
    spec = importlib.util.spec_from_file_location('fixture_eval_cli', REPO / 'scripts/office_eval_transition.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli); monkeypatch.setattr(cli, 'ROOT', release)
    eval_ledger = tmp_path / 'eval-ledger.db'
    argv = ['--store', str(tmp_path / 'office.db'), '--ledger', str(eval_ledger), '--desk', str(_desk(tmp_path)),
            '--clock-file', str(tmp_path / 'unused'), '--models', str(models),
            '--models-sha256', hashlib.sha256(models.read_bytes()).hexdigest(), '--cohort-id', 'eval1-core']
    clock = lambda: 2000.
    before = (tmp_path / 'office.db').read_bytes()
    assert cli.main(argv + ['--inspect'], clock=clock) == 0
    draft = json.loads(capsys.readouterr().out)['record']
    assert draft['near_pass_cohort']['id'] == 'eval1-core' and draft['postmortem']['model'] == 'claude-sonnet-5-5'
    draft['approvals'] = {'alpha': 'fixture-only', 'gamma': 'fixture-only'}
    record = tmp_path / 'record.json'; record.write_text(json.dumps(draft))
    bound = argv + ['--record', str(record), '--record-sha256', hashlib.sha256(record.read_bytes()).hexdigest()]
    assert cli.main(bound, clock=clock) == 0 and json.loads(capsys.readouterr().out)['state'] == 'dry_run'
    assert (tmp_path / 'office.db').read_bytes() == before and not eval_ledger.exists()
    assert cli.main(bound + ['--apply'], clock=clock) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['state'] == 'transitioned' and out['ledger'] == 'bound'
    assert cli.main(bound + ['--apply'], clock=clock) == 0
    assert json.loads(capsys.readouterr().out) == {'state': 'already_applied', 'ledger': 'bound'}
    assert cli.main(bound + ['--rollback'], clock=clock) == 0 and json.loads(capsys.readouterr().out)['state'] == 'dry_run_rollback'
    assert cli.main(argv + ['--apply'], clock=clock) == 2 and 'eval_approved_record_pin_required' in capsys.readouterr().err


def test_cli_refuses_an_unapproved_prompt_profile(disc2, tmp_path, capsys, monkeypatch):
    release, models = _release(tmp_path)
    profile = release / 'config/office/prompts' / X.NEW / 'sha256.json'
    doc = json.loads(profile.read_text()); doc['approval_status'] = 'pending_review'; profile.write_text(json.dumps(doc))
    spec = importlib.util.spec_from_file_location('fixture_eval_cli2', REPO / 'scripts/office_eval_transition.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli); monkeypatch.setattr(cli, 'ROOT', release)
    argv = ['--store', str(tmp_path / 'office.db'), '--ledger', str(tmp_path / 'l.db'), '--desk', str(_desk(tmp_path)),
            '--clock-file', str(tmp_path / 'x'), '--models', str(models), '--models-sha256',
            hashlib.sha256(models.read_bytes()).hexdigest(), '--cohort-id', 'eval1-core', '--inspect']
    assert cli.main(argv, clock=lambda: 2000.) == 2 and 'pilot_prompt_profile_unapproved' in capsys.readouterr().err
