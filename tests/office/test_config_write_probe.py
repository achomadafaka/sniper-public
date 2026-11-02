import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sniper.office.config_write_probe import probe_write


def test_rw_root_refuses_before_any_attempt(tmp_path):
    (tmp_path/'guard').write_text('original')
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        records=[]
        assert probe_write('guard',root_fd=fd,office_uid=os.geteuid(),office_mount_ns=os.readlink('/proc/self/ns/mnt'),office_gids=sorted(set(os.getgroups()+[os.getegid()])),record_witness=records.append) is None
        assert records[0]['reason']=='root_not_readonly'
        assert records[0]['witnesses']==[]
        assert (tmp_path/'guard').read_text()=='original'
    finally:os.close(fd)


def test_native_office_uid_helper_denial_witnesses(tmp_path):
    root=tmp_path/'config';root.mkdir();(root/'guard').write_text('original')
    script=r'''
import json,os,sys
from sniper.office.config_write_probe import probe_write
root=sys.argv[1]
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
records=[]
for path in ('.','guard'):
    assert probe_write(path,root_fd=fd,office_uid=os.geteuid(),office_mount_ns=os.readlink('/proc/self/ns/mnt'),office_gids=sorted(set(os.getgroups()+[os.getegid()])),record_witness=records.append) is False
for record in records:
    assert record['pid']!=os.getpid()
    assert record['uid']==os.geteuid()
    assert {w['action'] for w in record['witnesses']}=={'open_for_write','chmod','create','rename','unlink'}
    assert all(w['denied'] is True for w in record['witnesses'])
assert probe_write('guard',root_fd=fd,office_uid=os.geteuid()+1,office_mount_ns=os.readlink('/proc/self/ns/mnt'),office_gids=[],record_witness=records.append) is None
assert records[-1]['reason']=='office_identity_mismatch'
assert probe_write('guard',root_fd=fd,office_uid=os.geteuid(),office_mount_ns='mnt:[0]',office_gids=sorted(set(os.getgroups()+[os.getegid()])),record_witness=records.append) is None
assert records[-1]['reason']=='office_namespace_mismatch'
os.close(fd)
print(json.dumps(records))
'''
    result=subprocess.run(['/usr/bin/bwrap','--die-with-parent','--unshare-net','--ro-bind','/','/',
        '--bind',str(tmp_path),str(tmp_path),'--ro-bind',str(root),str(root),
        '--proc','/proc','--dev','/dev',sys.executable,'-B','-c',script,str(root)],
        capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert len(json.loads(result.stdout))==4
    assert (root/'guard').read_text()=='original'
    assert sorted(p.name for p in root.iterdir())==['guard']


def test_timeout_is_unknown(tmp_path,monkeypatch):
    from sniper.office import config_write_probe as P
    def timeout(*args,**kwargs):
        raise subprocess.TimeoutExpired('helper',.01)
    monkeypatch.setattr(P.subprocess,'run',timeout)
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        assert P.probe_write('.',root_fd=fd,office_uid=os.geteuid(),office_mount_ns=os.readlink('/proc/self/ns/mnt'),office_gids=[],record_witness=lambda p:None) is None
        os.fstat(fd)
    finally:os.close(fd)
