import pytest

from test_crew_handover_core import Host,core,descriptor


def launch_result(tmp_path,field=None,value=None):
    host=Host()
    if field is not None:
        normal=host.__getattr__('launch_candidate')
        def launch(packet):
            result=normal(packet);result['value'][field]=value;return result
        host.launch_candidate=launch
    engine=core().Coordinator(tmp_path/'control',host)
    engine.begin(descriptor(),dict(event_id='event',window_id='window'),now=1.)
    for at in range(2,10):
        record=engine.tick('office:head',now=float(at))
        if 'launch_candidate' in host.calls:return record
    pytest.fail('launch was never reached')


@pytest.mark.parametrize('field,value',[('session_id','old'),('incarnation','one')])
def test_launch_refuses_either_reused_old_identity(tmp_path,field,value):
    record=launch_result(tmp_path,field,value)
    assert record['phase']=='recovery_unknown' and record['business_allowed'] is False


def test_launch_accepts_a_fresh_session_and_incarnation(tmp_path):
    record=launch_result(tmp_path)
    assert record['phase']=='candidate_starting'
    assert record['candidate']['session_id']=='new' and record['candidate']['incarnation']=='two'
