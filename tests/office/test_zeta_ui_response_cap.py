import json
from sniper.office.report import read_state

def test_nested_guard_diagnostic_respects_whole_response_cap(tmp_path):
    root=tmp_path/'data'/'office';root.mkdir(parents=True)
    guard=root.parent/'office-control'/'guard';guard.mkdir(parents=True)
    payload=dict(state='paused',reason='manual',at=100,termination={'state':'failed','error':'\U0001f600'*180000})
    raw=json.dumps(payload,ensure_ascii=False).encode()
    assert len(raw)<1048576
    (guard/'guard.json').write_bytes(raw)
    state=read_state(root,now=100,host_now=100)
    assert len(json.dumps(state).encode())<=2097152


def test_nested_guard_diagnostics_reject_nonfinite_numbers(tmp_path):
    root=tmp_path/'data'/'office';root.mkdir(parents=True)
    guard=root.parent/'office-control'/'guard';guard.mkdir(parents=True)
    (guard/'guard.json').write_text(json.dumps(dict(state='paused',reason='manual',at=100,
        termination={'state':'failed','at':float('nan')},population={'state':'unknown','at':float('inf')})))
    state=read_state(root,now=100,host_now=100)
    json.dumps(state,allow_nan=False)
