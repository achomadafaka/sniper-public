import json
import pytest
from sniper.office.auth_canary import run


def transport(calls,**result):
    def call(system,user,**kw):
        calls.append((system,user,kw))
        return dict(text=json.dumps({'ok':True}),model_echo='fixture-model',**result)
    return call


def test_one_attempt_only_and_non_evidence_result():
    calls=[]
    report=run('claude','fixture-model',transport=transport(calls),clock=lambda:1000.,deadline_s=30)
    assert len(calls)==1 and report['state']=='passed'
    assert report['counts_as_evidence'] is False and report['served_model']=='fixture-model'
    assert calls[0][2]['deadline_s']==30


@pytest.mark.parametrize('error',['timeout','auth_revoked','rate_limit','existing_unknown'])
def test_failed_provider_is_never_retried_or_fallen_back(error):
    calls=[]
    report=run('codex','fixture-model',transport=transport(calls,call_error=error),clock=lambda:1000.,deadline_s=30)
    assert len(calls)==1 and report['state']=='refused'


def test_unknown_served_identity_refuses_after_single_call():
    calls=[]
    def call(*a,**kw):calls.append(1);return {'text':'{"ok":true}','model_echo':None}
    report=run('codex','fixture-model',transport=call,clock=lambda:1000.,deadline_s=30)
    assert len(calls)==1 and report['state']=='refused' and report['reason']=='served_model_unknown'


def test_unknown_clock_prevents_any_dispatch():
    def forbidden(*a,**kw):pytest.fail('clock must refuse before I/O')
    assert run('claude','fixture-model',transport=forbidden,clock=lambda:None,deadline_s=30)['state']=='refused'


def test_cli_default_refuses_before_reading_config_or_dispatching(capsys):
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[2]/'scripts/office_auth_canary.py'
    spec=importlib.util.spec_from_file_location('canary_script',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert module.main(['--config','/missing/config','--config-sha256','0'*64,
                        '--clock-file','/missing/clock','--provider','claude','--model','fixture-model'])==2
    assert json.loads(capsys.readouterr().out)['reason']=='canary_not_requested'
