"""Root-safe validator fixtures: all data synthetic, no live process/service/DB calls."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from sniper.office import planned_validator as V


def test_root_policy_refuses_user_owned_tree_before_loading(tmp_path):
    if os.getuid()==0:pytest.skip('unprivileged witness')
    p=tmp_path/'policy';p.write_text('{}')
    with pytest.raises(ValueError):V.root_document(p,'0'*64)


def test_pinned_data_checks_hash_and_never_executes(tmp_path):
    p=tmp_path/'payload';p.write_bytes(b'not executable data');p.chmod(0o644)
    h=hashlib.sha256(p.read_bytes()).hexdigest()
    assert V.pinned_data(p,h,owner_uid=os.getuid())==b'not executable data'
    with pytest.raises(ValueError):V.pinned_data(p,'0'*64,owner_uid=os.getuid())
    link=tmp_path/'link';link.symlink_to(p)
    with pytest.raises((ValueError,OSError)):V.pinned_data(link,h,owner_uid=os.getuid())


def fake_proc(tmp_path,pid=123):
    p=tmp_path/str(pid);p.mkdir()
    (p/'stat').write_text(str(pid)+' (worker tricky ) name) '+' '.join(['S']+['0']*18+['456']+['0']*5))
    (p/'status').write_text(f'Name: worker\nUid:\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\n')
    (p/'cmdline').write_bytes(b'/usr/bin/python3\0/srv/releases/'+b'a'*40+b'/supervisor.py\0')
    (p/'exe').symlink_to('/usr/bin/python3')
    for n in ('stat','status','cmdline'):(p/n).chmod(0o644)
    return p


def test_process_identity_binds_birth_uid_and_exact_script(tmp_path):
    p=fake_proc(tmp_path)
    got=V.process_identity(123,proc_root=tmp_path)
    assert got['birth_ticks']==456 and got['uid']==os.getuid()
    V.require_process(got,uid=os.getuid(),birth_ticks=456,script='/srv/releases/'+'a'*40+'/supervisor.py',exe='/usr/bin/python3')
    for over in [dict(birth_ticks=457),dict(uid=os.getuid()+1),dict(script='/other.py')]:
        kw=dict(uid=os.getuid(),birth_ticks=456,script='/srv/releases/'+'a'*40+'/supervisor.py',exe='/usr/bin/python3');kw.update(over)
        with pytest.raises(ValueError):V.require_process(got,**kw)


def test_unknown_pid_or_oversize_proc_is_not_absence(tmp_path):
    with pytest.raises((OSError,ValueError)):V.process_identity(999,proc_root=tmp_path)
    p=fake_proc(tmp_path);(p/'cmdline').write_bytes(b'x'*65537)
    with pytest.raises(ValueError):V.process_identity(123,proc_root=tmp_path)


def test_stopped_units_require_loaded_inactive_zero_pids_and_no_unknown():
    good=dict(LoadState='loaded',ActiveState='inactive',SubState='dead',MainPID='0',ControlPID='0')
    assert V.stopped_units({'office-supervisor.service':good},['office-supervisor.service'])
    for k,v in [('LoadState','not-found'),('ActiveState','failed'),('MainPID','123'),('ControlPID','1')]:
        with pytest.raises(ValueError):V.stopped_units({'office-supervisor.service':{**good,k:v}},['office-supervisor.service'])
    with pytest.raises(ValueError):V.stopped_units({},['office-supervisor.service'])


def dbs(tmp_path):
    store=tmp_path/'office.db';ledger=tmp_path/'ledger.db'
    c=sqlite3.connect(store)
    c.executescript('CREATE TABLE _store_info(id INTEGER,purpose TEXT); INSERT INTO _store_info VALUES(1,"office"); CREATE TABLE supervisor_lease(id INTEGER,expires_at REAL); INSERT INTO supervisor_lease VALUES(1,99); CREATE TABLE office_epoch_control(id INTEGER,payload_json TEXT); CREATE TABLE agents(id TEXT,payload_json TEXT); CREATE TABLE epochs(id TEXT,payload_json TEXT);')
    c.execute('INSERT INTO office_epoch_control VALUES(1,?)',(json.dumps(dict(active_epoch='office-pilot-discovery-2')),))
    for n in range(4):c.execute('INSERT INTO agents VALUES(?,?)',(str(n),json.dumps(dict(generation=2,state='active'))))
    for n in ['office-pilot-discovery-1','office-pilot-discovery-2']:c.execute('INSERT INTO epochs VALUES(?,?)',(n,'{}'))
    c.commit();c.close();c=sqlite3.connect(ledger)
    c.executescript('CREATE TABLE books(book_id TEXT,kind TEXT); INSERT INTO books VALUES("pilot-floor","discovery"); CREATE TABLE capital_flows(id INTEGER,book_id TEXT,amount REAL); INSERT INTO capital_flows VALUES(1,"pilot-floor",100);')
    c.close();return store,ledger


def test_database_inspection_requires_readonly_mount_no_immutable_wal_bypass(tmp_path):
    store,ledger=dbs(tmp_path)
    with pytest.raises(ValueError,match='readonly_mount'):V.office_witness(store,ledger,now_corrected=100.)


def test_expired_lease_and_frozen_roster_capital_witness(tmp_path,monkeypatch):
    store,ledger=dbs(tmp_path)
    monkeypatch.setattr(V.os,'statvfs',lambda p:SimpleNamespace(f_flag=os.ST_RDONLY))
    good=V.office_witness(store,ledger,now_corrected=100.)
    assert good['epoch']=='office-pilot-discovery-2' and good['generation']==2
    assert len(good['office_sha256'])==len(good['capital_sha256'])==64
    c=sqlite3.connect(store);c.execute('UPDATE supervisor_lease SET expires_at=101');c.commit();c.close()
    with pytest.raises(ValueError,match='lease'):V.office_witness(store,ledger,now_corrected=100.)


def test_unknown_lease_or_roster_never_validates(tmp_path,monkeypatch):
    store,ledger=dbs(tmp_path)
    monkeypatch.setattr(V.os,'statvfs',lambda p:SimpleNamespace(f_flag=os.ST_RDONLY))
    c=sqlite3.connect(store);c.execute('UPDATE agents SET payload_json=? WHERE id="0"',(json.dumps(dict(generation=3,state='active')),));c.commit();c.close()
    with pytest.raises(ValueError):V.office_witness(store,ledger,now_corrected=100.)


def test_process_identity_refuses_pid_reuse_between_reads(tmp_path, monkeypatch):
    p=fake_proc(tmp_path)
    real=V.read_regular
    def swap(path,**kw):
        raw=real(path,**kw)
        if Path(path).name=='cmdline':   # old process exits, PID reused before exe/stat recheck
            (p/'stat').write_text('123 (other) '+' '.join(['S']+['0']*18+['999']+['0']*5))
            (p/'cmdline').write_bytes(b'/usr/bin/python3\0/evil.py\0')
        return raw
    monkeypatch.setattr(V,'read_regular',swap)
    with pytest.raises(ValueError):V.process_identity(123,proc_root=tmp_path)


@pytest.mark.parametrize('argv',[
    ['/usr/bin/python3','-c','pass','/srv/approved.py'],
    ['/usr/bin/python3','-m','other','/srv/approved.py'],
    ['/usr/bin/python3','/srv/other.py','/srv/approved.py'],
    ['/usr/bin/python3','/srv/approved.py','--extra-unapproved']])
def test_script_as_data_or_extra_argv_is_not_approved_invocation(argv):
    observed=dict(uid=os.getuid(),birth_ticks=456,exe='/usr/bin/python3',argv=argv)
    with pytest.raises(ValueError):V.require_process(observed,uid=os.getuid(),birth_ticks=456,
                                                   script='/srv/approved.py',exe='/usr/bin/python3')


def test_explicit_root_approved_python_flags_are_supported_but_c_or_m_never_are():
    argv=['/usr/bin/python3','-I','-B','/srv/approved.py','--store','/srv/data/office.db']
    observed=dict(uid=os.getuid(),birth_ticks=456,exe='/usr/bin/python3',argv=argv)
    V.require_process(observed,uid=os.getuid(),birth_ticks=456,script='/srv/approved.py',
                      exe='/usr/bin/python3',expected_argv=argv)
    for flag in ['-c','-m']:
        bad=['/usr/bin/python3',flag,'/srv/approved.py']
        with pytest.raises(ValueError):V.require_process({**observed,'argv':bad},uid=os.getuid(),
            birth_ticks=456,script='/srv/approved.py',exe='/usr/bin/python3',expected_argv=bad)


def test_same_pid_birth_but_exec_or_uid_change_mid_observation_refuses(tmp_path,monkeypatch):
    p=fake_proc(tmp_path);real=V.read_regular
    def mutate(path,**kw):
        raw=real(path,**kw)
        if Path(path).name=='cmdline':(p/'cmdline').write_bytes(b'/usr/bin/python3\0/other.py\0')
        return raw
    monkeypatch.setattr(V,'read_regular',mutate)
    with pytest.raises(ValueError):V.process_identity(123,proc_root=tmp_path)


@pytest.mark.parametrize('table',["office_epoch_control","agents","epochs"])
def test_wrong_json_shape_is_typed_unknown(tmp_path,monkeypatch,table):
    store,ledger=dbs(tmp_path);monkeypatch.setattr(V.os,'statvfs',lambda p:SimpleNamespace(f_flag=os.ST_RDONLY))
    c=sqlite3.connect(store);c.execute('UPDATE '+table+' SET payload_json=?',('[]',));c.commit();c.close()
    with pytest.raises(ValueError):V.office_witness(store,ledger,now_corrected=100.)


def test_actual_owned_child_process_observation_uses_live_pidfd():
    import subprocess
    child=subprocess.Popen(['/usr/bin/sleep','2'],stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        got=V.process_identity(child.pid)
        assert got['pid']==child.pid and got['uid']==os.getuid() and got['birth_ticks']>0
        assert got['argv']==['/usr/bin/sleep','2']
    finally:
        child.kill();child.wait(timeout=2)


def test_actual_owned_child_exit_during_observation_is_unknown(monkeypatch):
    import subprocess
    child=subprocess.Popen(['/usr/bin/sleep','2'],stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    real=V.read_regular
    def exit_mid_read(path,**kw):
        raw=real(path,**kw)
        if Path(path)==Path('/proc')/str(child.pid)/'cmdline':child.kill();child.wait(timeout=2)
        return raw
    monkeypatch.setattr(V,'read_regular',exit_mid_read)
    try:
        with pytest.raises((ValueError,OSError)):V.process_identity(child.pid)
    finally:
        if child.poll()is None:child.kill();child.wait(timeout=2)
