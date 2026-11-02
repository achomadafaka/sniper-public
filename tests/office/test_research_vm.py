import hashlib,json,os
from pathlib import Path
from types import SimpleNamespace
import pytest
from sniper.office import research_vm as V


def plan(tmp_path,monkeypatch):
    image=tmp_path/'base.qcow2';image.write_bytes(b'approved-image-fixture');image.chmod(0o444)
    raw=tmp_path/'raw.jsonl';raw.write_text('{"slot":1}\n');raw.chmod(0o444)
    exports=tmp_path/'exports';exports.mkdir()
    monkeypatch.setattr(V.os,'statvfs',lambda _:SimpleNamespace(f_blocks=8192,f_frsize=4096,f_flag=0))
    monkeypatch.setattr(V.os,'fstatvfs',lambda _:SimpleNamespace(f_blocks=8192,f_frsize=4096,f_flag=0))
    return dict(version='office-research-vm-offline-v1',enabled=True,image={'path':str(image),'bytes':image.stat().st_size,'sha512':hashlib.sha512(image.read_bytes()).hexdigest()},
                snapshots=[{'path':str(raw),'name':'tape.jsonl','sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),'kind':'daily_tape'}],
                quarantine_review_commit='a'*40,network={'mode':'offline','allowlist':[]},exports_root=str(exports),memory_mib=1024,vcpus=1)


def test_missing_unapproved_plan_and_required_material_refuse(tmp_path,monkeypatch):
    p=tmp_path/'plan.json'
    with pytest.raises((OSError,ValueError)):V.load_plan(p,'a'*64)
    doc=plan(tmp_path,monkeypatch);p.write_text(json.dumps(doc))
    with pytest.raises(ValueError):V.load_plan(p,'a'*64)
    got=V.load_plan(p,hashlib.sha256(p.read_bytes()).hexdigest());assert got==doc
    for field in ['image','snapshots','network','quarantine_review_commit']:
        with pytest.raises(ValueError):V.command({k:v for k,v in doc.items() if k!=field})
    Path(doc['image']['path']).unlink()
    with pytest.raises((OSError,ValueError)):V.command(doc)


def test_vm_command_has_no_nic_or_host_data_and_readonly_inputs(tmp_path,monkeypatch):
    args=V.command(plan(tmp_path,monkeypatch))
    assert '--unshare-net' in args and '-nic' in args and args[args.index('-nic')+1]=='none'
    assert '--cap-drop' in args and '-snapshot' in args and args[args.index('-m')+1]=='1024'
    assert any('mount_tag=research' in a and 'readonly=on' in a for a in args)
    assert args.count('--bind')==1 # only bounded export filesystem; snapshots/image are ro-bind


def test_provider_network_and_unbounded_export_filesystem_refuse(tmp_path,monkeypatch):
    p=plan(tmp_path,monkeypatch)
    p['network']={'mode':'proxy','allowlist':['rpc.example']}
    with pytest.raises(ValueError):V.command(p)
    p['network']={'mode':'offline','allowlist':[]}
    monkeypatch.setattr(V.os,'statvfs',lambda _:SimpleNamespace(f_blocks=10**9,f_frsize=4096,f_flag=0))
    with pytest.raises(ValueError):V.command(p)


def test_quarantined_input_names_and_aliases_refuse(tmp_path,monkeypatch):
    p=plan(tmp_path,monkeypatch)
    for name in ['evaluation.db','grades.jsonl','../tape.jsonl','outcomes.jsonl']:
        p['snapshots'][0]['name']=name
        with pytest.raises(ValueError):V.command(p)


def test_hostile_exports_are_not_imported_or_executable(tmp_path):
    src=tmp_path/'exports';src.mkdir();dst=tmp_path/'desk';dst.mkdir()
    good=src/'result.json';good.write_text('{"answer":1}')
    with pytest.raises(ValueError):V.export_file(src,'result.json',dst,guest_stopped=False)
    (src/'escape.txt').symlink_to('/etc/passwd')
    with pytest.raises((OSError,ValueError)):V.export_file(src,'escape.txt',dst,guest_stopped=True)
    bad=src/'code.py';bad.write_text('raise SystemExit(99)')
    with pytest.raises(ValueError):V.export_file(src,'code.py',dst,guest_stopped=True)
    good.chmod(0o700)
    with pytest.raises(ValueError):V.export_file(src,'result.json',dst,guest_stopped=True)
    good.chmod(0o600)
    r=V.export_file(src,'result.json',dst,guest_stopped=True)
    target=dst/r['name'];assert target.read_text()=='{"answer":1}' and target.stat().st_mode&0o111==0
    assert r['type']=='untrusted_vm_export'

def test_grade_table_cannot_hide_under_innocent_snapshot_filename(tmp_path,monkeypatch):
    import sqlite3
    p=plan(tmp_path,monkeypatch);db=tmp_path/'archive.db'
    c=sqlite3.connect(db);c.execute('CREATE TABLE private_grades(value)');c.commit();c.close();db.chmod(0o444)
    p['snapshots']=[dict(path=str(db),name='archive.db',kind='archive',sha256=hashlib.sha256(db.read_bytes()).hexdigest())]
    with pytest.raises(ValueError,match='quarantined_table'):V.command(p)


def test_fifo_hardlink_and_oversized_exports_refuse(tmp_path):
    src=tmp_path/'src';src.mkdir();dst=tmp_path/'dst';dst.mkdir()
    os.mkfifo(src/'fifo.txt')
    with pytest.raises(ValueError):V.export_file(src,'fifo.txt',dst,guest_stopped=True)
    a=src/'hard.txt';a.write_text('x');os.link(a,src/'other.txt')
    with pytest.raises(ValueError):V.export_file(src,'hard.txt',dst,guest_stopped=True)
    (src/'large.txt').write_bytes(b'x'*(V.EXPORT_MAX+1))
    with pytest.raises(ValueError):V.export_file(src,'large.txt',dst,guest_stopped=True)
    assert list(dst.iterdir())==[]

def test_export_filesystem_must_have_known_bound_and_start_empty(tmp_path,monkeypatch):
    p=plan(tmp_path,monkeypatch)
    monkeypatch.setattr(V.os,'statvfs',lambda _:SimpleNamespace(f_blocks=0,f_frsize=4096,f_flag=0))
    with pytest.raises(ValueError,match='unbounded'):V.command(p)
    monkeypatch.setattr(V.os,'statvfs',lambda _:SimpleNamespace(f_blocks=8192,f_frsize=4096,f_flag=0))
    (Path(p['exports_root'])/'old.txt').write_text('old guest output')
    with pytest.raises(ValueError,match='not_empty'):V.command(p)

def test_replaced_verified_inputs_refuse_without_reopening_new_paths(tmp_path,monkeypatch):
    p=plan(tmp_path,monkeypatch)
    with V.pinned_command(p) as launch:
        launch.verify()
        fds=launch.pass_fds
        replacement=tmp_path/'replacement';replacement.write_bytes(b'WRONG INODE');replacement.chmod(0o444)
        os.replace(replacement,p['image']['path'])
        with pytest.raises(ValueError,match='vm_input_replaced'):launch.verify()
    for fd in fds:
        with pytest.raises(OSError):os.fstat(fd)


def test_changed_inode_contents_or_permissions_refuse(tmp_path,monkeypatch):
    p=plan(tmp_path,monkeypatch)
    with V.pinned_command(p) as launch:
        source=Path(p['snapshots'][0]['path']);source.chmod(0o644);source.write_bytes(b'changed')
        with pytest.raises(ValueError,match='vm_input_changed'):launch.verify()


def test_actual_bwrap_uses_verified_fds_readonly_with_hidden_original_parents(tmp_path,monkeypatch):
    import subprocess
    p=plan(tmp_path,monkeypatch)
    with V.pinned_command(p) as launch:
        launch.verify();args=launch.argv
        image_fd=args[args.index('/opt/base.qcow2')-1];input_fd=args[args.index('/opt/inputs/tape.jsonl')-1]
        cmd=['/usr/bin/bwrap','--unshare-user','--unshare-pid','--ro-bind','/','/',
             '--tmpfs','/home','--tmpfs','/tmp','--tmpfs','/opt','--ro-bind-fd',image_fd,'/opt/image',
             '--ro-bind-fd',input_fd,'/opt/input','--proc','/proc','--cap-drop','ALL',
             '/usr/bin/python3','-c',
             "from pathlib import Path;import os;assert Path('/opt/image').read_bytes()==b'approved-image-fixture';assert Path('/opt/input').read_bytes()==b'{\"slot\":1}\\n';\ntry:os.chmod('/opt/image',0o600)\nexcept OSError:pass\nelse:raise AssertionError('input writable')"]
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=10,pass_fds=launch.pass_fds)
        assert r.returncode==0,r.stderr


def test_unlink_race_never_binds_replacement_bytes(tmp_path,monkeypatch):
    import subprocess
    p=plan(tmp_path,monkeypatch)
    with V.pinned_command(p) as launch:
        launch.verify();args=launch.argv;fd=args[args.index('/opt/base.qcow2')-1]
        replacement=tmp_path/'replacement';replacement.write_bytes(b'wrong');replacement.chmod(0o444);os.replace(replacement,p['image']['path'])
        cmd=['/usr/bin/bwrap','--ro-bind','/','/','--tmpfs','/opt','--ro-bind-fd',fd,'/opt/image',
             '/usr/bin/python3','-c',"from pathlib import Path;assert Path('/opt/image').read_bytes()==b'approved-image-fixture';print('verified')"]
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=5,pass_fds=launch.pass_fds)
        assert r.returncode!=0 or r.stdout.strip()=='verified',r.stderr


def test_export_directory_is_pinned_and_replacement_refuses(tmp_path,monkeypatch):
    p=plan(tmp_path,monkeypatch)
    with V.pinned_command(p) as launch:
        args=launch.argv;at=args.index('/opt/exports');assert args[at-2]=='--bind-fd'
        root=Path(p['exports_root']);root.rename(tmp_path/'original-exports');root.mkdir()
        with pytest.raises(ValueError,match='vm_input_replaced'):launch.verify()
