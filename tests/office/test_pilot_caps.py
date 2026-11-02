from pathlib import Path
from sniper.office import pilot_caps as C


ROOT=Path(__file__).resolve().parents[2]


def test_render_uses_exact_approved_manifest_caps():
    text=C.render(ROOT/'config/office/host_guard_pilot.json')
    assert 'CPUQuota=200%' in text and 'MemoryMax=4294967296' in text
    assert 'TasksMax=256' in text and 'IOWeight=10' in text


def test_cap_readback_refuses_mismatch_unknown_and_unapplied_kernel():
    expected=dict(CPUQuotaPerSecUSec='2s',MemoryMax='4294967296',TasksMax='256',IOWeight='10',
                  ControlGroup='/user.slice/office.slice')
    manifest=ROOT/'config/office/host_guard_pilot.json'
    assert C.verify(manifest,expected,kernel=lambda _:True)['state']=='base_caps_ready'
    for key in ('CPUQuotaPerSecUSec','MemoryMax','TasksMax'):
        props=dict(expected);props[key]='infinity'
        assert C.verify(manifest,props,kernel=lambda _:True)['state']=='refused'
    assert C.verify(manifest,{},kernel=lambda _:True)['state']=='refused'
    assert C.verify(manifest,expected,kernel=lambda _:False)['state']=='refused'


def test_kernel_verifier_must_return_literal_true():
    manifest=ROOT/'config/office/host_guard_pilot.json'
    expected=dict(CPUQuotaPerSecUSec='2s',MemoryMax='4294967296',TasksMax='256',IOWeight='10',
                  ControlGroup='/user.slice/office.slice')
    assert C.verify(manifest,expected,kernel=lambda _:'yes')['state']=='refused'


def test_io_is_pending_without_child_and_actual_evidence_is_separate():
    from uuid import uuid4
    manifest=ROOT/'config/office/host_guard_pilot.json'
    properties=dict(CPUQuotaPerSecUSec='2s',MemoryMax='4294967296',TasksMax='256',IOWeight='10',ControlGroup='/user.slice/office.slice')
    report=C.verify(manifest,properties,kernel=lambda _:True)
    assert report['state']=='base_caps_ready' and report['io']['IOWeight']=='not_enforceable_user_scope'
    assert report['io']['evidence_state']=='pending_child_preexec'
    rid=str(uuid4());receipt=dict(request_id=rid,io_preexec=dict(request_id=rid,source='ioprio_get',io_class=3,before_provider_exec=True,observed_at_host=1000.))
    report=C.verify(manifest,properties,kernel=lambda _:True,child_receipts=[receipt])
    assert report['state']=='passed' and report['io']['evidence_state']=='verified'
    receipt['io_preexec']['io_class']=2
    assert C.verify(manifest,properties,kernel=lambda _:True,child_receipts=[receipt])['state']=='refused'


def test_child_receipt_must_come_from_private_launcher_state(tmp_path,monkeypatch):
    import json,pytest
    from uuid import uuid4
    monkeypatch.setattr(C.Path,'home',classmethod(lambda cls:tmp_path))
    root=tmp_path/'Desktop/Sniper/data/office-control/launcher-state';root.mkdir(parents=True,mode=0o700)
    rid=str(uuid4());path=root/(rid+'.result.json');record={'request_id':rid}
    path.write_text(json.dumps(record));path.chmod(0o600)
    assert C.read_child_receipt(path)==record
    path.chmod(0o644)
    with pytest.raises(ValueError):C.read_child_receipt(path)
    public=tmp_path/(rid+'.result.json');public.write_text(json.dumps(record))
    with pytest.raises(ValueError):C.read_child_receipt(public)
