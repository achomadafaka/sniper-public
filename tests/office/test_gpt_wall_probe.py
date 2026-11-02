"""Runs the real-sandbox wall proof when asked: about three minutes, needs sudo to uid office and the pinned CLI. OFFICE_WALL_PROBE_CODEX=<path to codex binary>."""
import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(not os.environ.get('OFFICE_WALL_PROBE_CODEX'),reason='wall probe not requested')
def test_gpt_sandbox_walls():
    done=subprocess.run([sys.executable,'scripts/office_gpt_wall_probe.py','--codex-binary',os.environ['OFFICE_WALL_PROBE_CODEX']],capture_output=True,text=True,timeout=560)
    assert done.returncode==0,done.stdout[-4000:]


def test_a_wrong_user_fails_the_uid_check():
    sys.path.insert(0,'scripts')
    import office_gpt_wall_probe as W
    assert W.uid_is_office(dict(uid=995),995)
    assert not W.uid_is_office(dict(uid=1000),995) and not W.uid_is_office(dict(uid=None),995) and not W.uid_is_office({},995)


def _guard(name,calls):
    import ast
    tree=ast.parse(open('scripts/office_gpt_wall_probe.py').read())
    nodes=[k.value for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='update' for k in n.keywords if k.arg==name]
    assert len(nodes)==1
    return eval(compile(ast.Expression(nodes[0]),'guard','eval'),{'calls':calls})


def test_cli_tool_guard_needs_a_successful_call_stamped_by_the_cli():
    ok=dict(name='sleep',origin='from-cli',is_error=False,text='ok')
    assert _guard('real_seat_tool_called_from_cli',[ok])
    assert not _guard('real_seat_tool_called_from_cli',[dict(ok,origin='from-shell')])
    assert not _guard('real_seat_tool_called_from_cli',[dict(ok,is_error=True)])


def test_order_tool_guard_needs_both_refused_as_unknown():
    refused=[dict(name=n,is_error=True,text='unknown_tool') for n in ('paper_order','book_halt')]
    assert _guard('shell_on_host_sock_gets_no_paper_tools',refused)
    assert not _guard('shell_on_host_sock_gets_no_paper_tools',refused[:1])
    assert not _guard('shell_on_host_sock_gets_no_paper_tools',[dict(refused[0],is_error=False,text='entered'),refused[1]])


def test_head_guards_need_each_call_refused_for_the_role():
    refused=[dict(name='paper_order',is_error=True,text='{"reason": "role_cannot_order"}'),dict(name='paper_exit',is_error=True,text='{"reason": "role_cannot_exit"}')]
    assert _guard('head_cannot_order',refused) and _guard('head_cannot_exit',refused)
    assert not _guard('head_cannot_order',[]) and not _guard('head_cannot_exit',[])
    assert not _guard('head_cannot_order',[dict(refused[0],is_error=False)])
    assert not _guard('head_cannot_exit',[dict(refused[1],text='{"reason": "other"}')])


def test_probe_clock_sample_is_what_the_order_path_reads(tmp_path):
    import json,time
    sys.path.insert(0,'scripts')
    import office_gpt_wall_probe as W
    from sniper.services.memecoin_desk_intake import clock_sample
    path=tmp_path/'clock';path.write_text(json.dumps(W.clock_doc()))
    assert clock_sample(path,now=time.time())[0]==0.0


def test_judge_guards_refuse_a_call_that_stops_at_the_role():
    assert not _guard('judge_order_passes_the_role_check_only',[dict(name='paper_order',is_error=True,text='role_cannot_order')])
    assert not _guard('judge_order_passes_the_role_check_only',[])
    assert _guard('judge_order_passes_the_role_check_only',[dict(name='paper_order',is_error=True,text='sheet_stale')])
    assert not _guard('judge_order_passes_the_role_check_only',[dict(name='paper_order',is_error=False,text='submitted')])
    assert not _guard('judge_exit_bound_to_own_book',[dict(name='paper_exit',is_error=False,text='order_scope_mismatch')])
