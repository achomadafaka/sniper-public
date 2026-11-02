import hashlib
import json
from pathlib import Path
import pytest
from sniper.office import pilot_models as P


PATH=Path(__file__).resolve().parents[2]/'config/office/pilot_models.json'


def loaded():return P.load(PATH,hashlib.sha256(PATH.read_bytes()).hexdigest())


def test_unknown_pin_extra_seat_and_cross_family_model_refuse(tmp_path):
    with pytest.raises(ValueError):P.load(PATH,'0'*64)
    original=json.loads(PATH.read_bytes())
    for change in ('extra','family'):
        doc=json.loads(json.dumps(original))
        if change=='extra':doc['seats']['fifth']=dict(provider='claude',model='claude-opus-5-5')
        else:doc['seats']['judge_gpt']['model']='claude-opus-5-5'
        path=tmp_path/change;path.write_text(json.dumps(doc))
        with pytest.raises(ValueError):P.load(path,hashlib.sha256(path.read_bytes()).hexdigest())


def test_requested_model_is_pinned_and_unknown_served_model_cannot_succeed():
    seen=[]
    def transport(*a,**kw):seen.append(kw);return dict(text='{}',model_echo=None)
    envelope=P.call(loaded(),'judge_gpt','system','user',deadline_s=30,schema_obj={'type':'object'},
                    clock=lambda:1000.,transports={'codex':transport})
    assert len(seen)==1 and seen[0]['model']=='gpt-6-luna'
    assert envelope.outcome=='invalid' and envelope.error_reason=='served_model_unknown' and envelope.text is None


@pytest.mark.parametrize('discovery_only,expected', [(False,'invalid'),(True,'success')])
def test_requested_pinned_identity_is_discovery_only(discovery_only,expected):
    result=P.call(loaded(),'judge_gpt','system','user',deadline_s=30,schema_obj={'type':'object'},
        clock=lambda:1000.,transports={'codex':lambda *a,**k:dict(text='{}',model_echo=None,served_model_basis='requested_pinned')},
        discovery_only=discovery_only)
    assert result.outcome==expected
    assert result.served_model==('gpt-6-luna' if discovery_only else None)
    assert result.served_model_basis=='requested_pinned'


def test_discovery_does_not_guess_identity_without_host_pin_proof():
    result=P.call(loaded(),'judge_gpt','system','user',deadline_s=30,schema_obj={'type':'object'},
        clock=lambda:1000.,transports={'codex':lambda *a,**k:dict(text='{}',model_echo=None)},discovery_only=True)
    assert result.outcome=='invalid' and result.error_reason=='served_model_unknown'


@pytest.mark.parametrize('discovery_only',[False,True])
def test_requested_pin_never_overrides_conflicting_real_echo(discovery_only):
    result=P.call(loaded(),'judge_gpt','system','user',deadline_s=30,schema_obj={'type':'object'},
        clock=lambda:1000.,transports={'codex':lambda *a,**k:dict(text='{}',model_echo='gpt-other',served_model_basis='requested_pinned')},
        discovery_only=discovery_only)
    assert result.outcome=='invalid' and result.error_reason=='model_mismatch'
    assert result.served_model=='gpt-other' and result.served_model_basis=='echoed'
