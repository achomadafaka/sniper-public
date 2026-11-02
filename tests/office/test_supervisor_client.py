import os
import socket
import struct
from pathlib import Path
import pytest
from sniper.office.supervisor_client import Endpoint


def fixture(tmp_path,monkeypatch,peer_uid=None):
    root=tmp_path/'launcher-sock';root.mkdir(mode=0o700)
    path=root/'launcher.sock';server=socket.socket(socket.AF_UNIX);server.bind(str(path))
    class Flags:f_flag=os.ST_RDONLY
    monkeypatch.setattr(os,'statvfs',lambda _:Flags())
    class Conn:
        def getpeername(self):return str(path)
        def getsockopt(self,*a):return struct.pack('3i',0,os.getuid() if peer_uid is None else peer_uid,os.getgid())
    return server,Endpoint(path,expected_uid=os.getuid()),Conn()


def test_kernel_uid_required_and_pid_zero_is_valid_under_private_namespace(tmp_path,monkeypatch):
    server,peer,conn=fixture(tmp_path,monkeypatch)
    try:assert peer(conn) is True
    finally:server.close()


def test_wrong_uid_refuses(tmp_path,monkeypatch):
    server,peer,conn=fixture(tmp_path,monkeypatch,peer_uid=os.getuid()+1)
    try:assert peer(conn) is False
    finally:server.close()


def test_writable_endpoint_directory_refuses_even_correct_uid(tmp_path,monkeypatch):
    server,peer,conn=fixture(tmp_path,monkeypatch)
    class Flags:f_flag=0
    monkeypatch.setattr(os,'statvfs',lambda _:Flags())
    try:assert peer(conn) is False
    finally:server.close()


def test_unprotected_directory_or_replaced_endpoint_refuses(tmp_path,monkeypatch):
    server,peer,conn=fixture(tmp_path,monkeypatch)
    try:
        Path(conn.getpeername()).parent.chmod(0o755)
        assert peer(conn) is False
        Path(conn.getpeername()).parent.chmod(0o700)
        Path(conn.getpeername()).unlink();Path(conn.getpeername()).write_text('not a socket')
        assert peer(conn) is False
    finally:server.close()


def test_actual_private_pid_jail_reads_protected_socket_but_cannot_create_one(tmp_path):
    import subprocess
    root=tmp_path/'launcher-sock';root.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(root/'launcher.sock'));server.listen(1)
        code="""import os,socket
from sniper.office.supervisor_client import Endpoint
try:
 forged=socket.socket(socket.AF_UNIX);forged.bind('/opt/peer/forged.sock')
except OSError as e:
 assert e.errno==30,e
else:raise AssertionError('supervisor could create a launcher endpoint')
with socket.socket(socket.AF_UNIX) as conn:
 conn.connect('/opt/peer/launcher.sock')
 assert Endpoint('/opt/peer/launcher.sock',expected_uid=os.getuid())(conn) is True
"""
        argv=['/usr/bin/bwrap','--unshare-user','--unshare-pid','--ro-bind','/','/','--proc','/proc',
              '--dev','/dev','--tmpfs','/home','--tmpfs','/root','--tmpfs','/run','--tmpfs','/tmp','--tmpfs','/opt',
              '--ro-bind',str(Path(__file__).resolve().parents[2]),'/opt/office',
              '--ro-bind',str(root),str(root),'--setenv','PYTHONPATH','/opt/office','--cap-drop','ALL',
              '/usr/bin/python3','-c',code]
        argv[-1]=code.replace('/opt/peer',str(root))
        result=subprocess.run(argv,capture_output=True,text=True,timeout=5)
        assert result.returncode==0,result.stdout+result.stderr


def test_factory_needs_no_credential_paths_and_requires_external_pin(tmp_path):
    import hashlib,json
    from sniper.office.supervisor_client import build
    config=tmp_path/'transport.json'
    config.write_text(json.dumps(dict(socket_path=str(tmp_path/'launcher-sock/launcher.sock'),
                                     request_root=str(tmp_path/'requests'),crew_uid=os.getuid())))
    with pytest.raises(ValueError,match='unapproved'):
        build(config,expected_sha256='0'*64,clock=lambda:1000.)
    transports=build(config,expected_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),clock=lambda:1000.)
    assert set(transports)=={'claude','codex'}
    assert set(transports['codex'].client.config)=={'socket_path','request_root'}
