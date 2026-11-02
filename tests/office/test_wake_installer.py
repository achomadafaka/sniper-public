"""Installer-only wake topology fixtures; no real systemd or service actions."""
import json
from office.test_pilot_activate import env

WAKE_SERVICE='office-supervisor-wake.service'
WAKE_TIMER='office-supervisor-wake.timer'


def calls(env):
    return [json.loads(line) for line in env['log'].read_text().splitlines()]


def test_plan_installs_wake_pair_without_starting(env):
    result=env['run']()
    assert result.returncode==0,result.stdout+result.stderr
    files=json.loads(result.stdout)['files']
    assert str(env['units']/WAKE_SERVICE) in files
    assert str(env['units']/WAKE_TIMER) in files
    assert not env['state'].exists()
    assert not env['release'].exists()


def test_apply_enables_timer_not_static_service_then_rollback(env):
    result=env['run']('--apply')
    assert result.returncode==3,result.stdout+result.stderr
    assert (env['units']/WAKE_SERVICE).is_file()
    assert (env['units']/WAKE_TIMER).is_file()
    assert (env['release']/'scripts/office_supervisor_wake.py').is_file()
    enabled=json.loads(env['state'].read_text())
    assert enabled[WAKE_TIMER] is True
    assert WAKE_SERVICE not in enabled
    assert not any(word in ('start','restart','stop','--now') for call in calls(env) for word in call)
    result=env['run']('--rollback','--apply')
    assert result.returncode==0,result.stdout+result.stderr
    assert not (env['units']/WAKE_SERVICE).exists()
    assert not (env['units']/WAKE_TIMER).exists()
    assert json.loads(env['state'].read_text())[WAKE_TIMER] is False


def test_foreign_wake_timer_refuses_before_mutation(env):
    dest=env['units']/WAKE_TIMER
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text('foreign timer')
    result=env['run']('--apply')
    assert result.returncode==2
    assert dest.read_text()=='foreign timer'
    assert not env['release'].exists()


def test_enable_failure_rolls_back_new_timer_and_preserves_prior_enable(env):
    env['state'].write_text(json.dumps({'office-monitor.service':True}))
    env['child_env']['FIXTURE_FAIL_ENABLE']='1'
    result=env['run']('--apply')
    assert result.returncode==2,result.stdout+result.stderr
    assert not (env['units']/WAKE_TIMER).exists()
    enabled=json.loads(env['state'].read_text())
    assert enabled['office-monitor.service'] is True
    assert enabled.get(WAKE_TIMER) is False


import os
import shutil
import sys
import pytest


@pytest.mark.parametrize('missing', ['deploy/office/'+WAKE_SERVICE,'deploy/office/'+WAKE_TIMER,'scripts/office_supervisor_wake.py'])
def test_unreadable_pinned_wake_source_refuses_before_mutation(env,missing):
    # Inject an exact Git-object read refusal; never alters production source/objects.
    real_git=shutil.which('git')
    wrapper=env['log'].parent/'bin'/'git'
    wrapper.write_text('#!'+sys.executable+'\nimport os,sys\n'
        +'if any(a.endswith(":"+'+repr(missing)+') for a in sys.argv[1:]): raise SystemExit(1)\n'
        +'os.execv('+repr(real_git)+', ['+repr(real_git)+',*sys.argv[1:]])\n')
    wrapper.chmod(0o755)
    result=env['run']('--apply')
    assert result.returncode==2,result.stdout+result.stderr
    assert not env['release'].exists()
    assert not env['state'].exists()
