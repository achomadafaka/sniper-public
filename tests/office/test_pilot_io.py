import json
from uuid import uuid4
from sniper.office import child_launcher as C


def proof(rid,**changes):
    value=dict(request_id=rid,source='ioprio_get',io_class=3,before_provider_exec=True,observed_at_host=1000.)
    value.update(changes);return value


def test_only_first_identity_bound_kernel_idle_marker_is_accepted():
    rid=str(uuid4())
    good='OFFICE_IO_PREEXEC '+json.dumps(proof(rid))
    assert C.extract_io_proof(('systemd status\n'+good+'\n').encode(),rid)==proof(rid)
    assert C.extract_io_proof(good.encode(),str(uuid4())) is None
    bad='OFFICE_IO_PREEXEC '+json.dumps(proof(rid,io_class=2))
    assert C.extract_io_proof((bad+'\n'+good).encode(),rid) is None
    assert C.extract_io_proof(b'no marker',rid) is None


def test_unknown_malformed_or_non_kernel_marker_refuses():
    rid=str(uuid4())
    for changes in (dict(io_class=True),dict(source='model'),dict(before_provider_exec=False),
                    dict(observed_at_host=None)):
        assert C.extract_io_proof(('OFFICE_IO_PREEXEC '+json.dumps(proof(rid,**changes))).encode(),rid) is None
    assert C.extract_io_proof(b'OFFICE_IO_PREEXEC {',rid) is None


def test_preexec_gate_must_query_kernel_before_emitting_proof_or_exec():
    code=C.CHILD_GATE
    assert '/usr/bin/ionice' in code and 'ioprio_not_idle' in code
    assert code.index('/usr/bin/ionice') < code.index('OFFICE_IO_PREEXEC') < code.index('os.execv')


def test_base_caps_do_not_claim_an_undelegated_io_controller(tmp_path):
    from scripts.office_watchdog import caps_applied
    group=tmp_path/'office.slice';group.mkdir()
    for name,value in {'cpu.max':'200000 100000','memory.max':'4294967296','pids.max':'256'}.items():
        (group/name).write_text(value)
    assert caps_applied(group)


def test_actual_non_idle_gate_refuses_before_provider_exec(tmp_path):
    import subprocess,time
    guard=tmp_path/'guard';guard.mkdir();(guard/'guard.json').write_text(json.dumps(dict(state='ready',reason=None,at=time.time(),guard_policy_version='office-host-guard-pilot-v3')))
    command=['/usr/bin/ionice','-c','2','/usr/bin/bwrap','--unshare-user','--unshare-pid','--unshare-net',
             '--ro-bind','/','/','--proc','/proc','--tmpfs','/opt','--ro-bind',str(guard),'/opt/guard',
             '--cap-drop','ALL','/usr/bin/python3','-c',C.CHILD_GATE,str(uuid4()),'/usr/bin/printf','PROVIDER_EXEC_RAN']
    result=subprocess.run(command,capture_output=True,text=True,timeout=5)
    assert result.returncode!=0 and 'ioprio_not_idle' in result.stderr and 'PROVIDER_EXEC_RAN' not in result.stdout


def test_old_health_cannot_satisfy_new_guard_policy():
    from scripts.office_watchdog import PilotGuard
    old=dict(ts=100.,funded_heartbeat_age_s=.1,cpu_psi_some_avg10=.01,io_psi_some_avg10=.01,
             mem_available_bytes=8*1024**3,caps_applied=True,roster_valid=True,resources_verified=True)
    assert PilotGuard().check(old,now=100.)=='guard_policy_unknown'
