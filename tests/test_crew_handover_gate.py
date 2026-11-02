import importlib
import pytest


def gate():return importlib.import_module('tools.crew.handover_gate')


@pytest.mark.parametrize('tool,args', [('shell',{'cmd':'python writer.py'}),('functions.exec',{'code':'await tools.writer()'}),('writer',{'path':'wrong'}),('writer',{'path':'note','extra':'shell'})])
def test_pending_note_denies_every_nonexact_tool_including_nested_code(tool,args):
    state=dict(phase='note_pending',session_id='old',incarnation='one',failed_note_turns=0)
    allowed=[dict(tool='writer',arguments={'path':'note'})]
    assert gate().allow_tool(state,session_id='old',incarnation='one',tool=tool,arguments=args,allowlist=allowed) is False


def test_exact_writer_only_and_two_failures_park():
    state=dict(phase='note_pending',session_id='old',incarnation='one',failed_note_turns=0)
    allowed=[dict(tool='writer',arguments={'path':'note'})]
    assert gate().allow_tool(state,session_id='old',incarnation='one',tool='writer',arguments={'path':'note'},allowlist=allowed)
    assert not gate().allow_tool(state,session_id='wrong',incarnation='one',tool='writer',arguments={'path':'note'},allowlist=allowed)
    state=gate().note_turn_finished(state,valid=False)
    assert state['phase']=='note_pending'
    state=gate().note_turn_finished(state,valid=False)
    assert state['phase']=='parked' and state['needs_page'] is True
    assert not gate().allow_tool(state,session_id='old',incarnation='one',tool='writer',arguments={'path':'note'},allowlist=allowed)
