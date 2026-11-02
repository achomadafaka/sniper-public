"""Runs the real sandbox arguments as the real `office` uid. Skips only where that user does not exist; the proof host has it."""
import os
from pathlib import Path
import pwd
import subprocess

import pytest

from sniper.office import seat_sandbox as B

REPO=Path(os.environ.get('SNIPER_STATE_ROOT','/home/bigpapi/Desktop/Sniper'))
PY='/opt/runtime/bin/python3'
SECRETS=[REPO/'.env',REPO/'.env.local',REPO/'data/solana_keypair.json',REPO/'data/evm_keypair.json',Path.home()/'.claude/.credentials.json',REPO/'sniper_tracker.db']
PROBE=r'''
import socket,sys
bad=[]
for p in sys.argv[1:]:
    try:open(p,'rb').read(1);bad.append('read:'+p)
    except OSError:pass
for host,port in (('127.0.0.1',54321),('127.0.0.1',5000),('1.1.1.1',443)):
    s=socket.socket();s.settimeout(1)
    try:s.connect((host,port));bad.append('net:%s:%d'%(host,port))
    except OSError:pass
for p in ('/etc/x','/usr/x','/opt/release/x','/opt/runtime/x'):
    try:open(p,'w');bad.append('write:'+p)
    except OSError:pass
print('BAD',bad) if bad else print('WALLS_OK')
'''


def _absent(why):
    # A skip cannot satisfy the Head gate: the gate run sets this and an absent wall is a failure.
    if os.environ.get('OFFICE_WALLS_REQUIRED')=='1':pytest.fail(why)
    pytest.skip(why)


def _office():
    try:pwd.getpwnam('office')
    except KeyError:_absent('no office user')
    if subprocess.run(['sudo','-n','-u','office','true'],capture_output=True,cwd='/').returncode:_absent('no sudo to office')


def _run(argv):
    return subprocess.run(argv,capture_output=True,text=True,timeout=30,cwd='/')


def _bridge(tmp):
    """Throwaway bridge and workspace readable by office. /srv itself is untouched."""
    seat=tmp/'seats/head';seat.mkdir(parents=True);(tmp/'runtime').mkdir();(tmp/'release').mkdir()
    for name in ('ca.pem','tool-proxy.sock','host.sock','model-proxy.sock','credentials.json'):(seat/name).write_text('x')
    work=tmp/'work';work.mkdir();(work/'.claude').mkdir();(work/'tmp').mkdir()
    subprocess.run(['chmod','-R','a+rwX',str(tmp)],check=True)
    return work


def _fixed(args,tmp):
    """Throwaway seat and release; the runtime stays the real published one, so every probe runs on what a seat runs on."""
    if not (B.BRIDGE/'runtime/bin/python3').exists():_absent('runtime not published')
    return [str(tmp/a[len(str(B.BRIDGE))+1:]) if a.startswith(str(B.BRIDGE)+'/') and a!=str(B.BRIDGE/'runtime') else a for a in args]


def _args(fn,tmp):
    work=_bridge(tmp)
    args=fn('head',work,bridge=tmp)
    # _base binds the real bridge's runtime and release: point them at the throwaway.
    return _fixed(args,tmp)


@pytest.fixture
def tmp(tmp_path_factory):
    root=Path('/dev/shm')/('office-walls-%d'%os.getpid())
    root.mkdir(exist_ok=True)
    os.chmod(root,0o777)
    yield root
    subprocess.run(['rm','-rf',str(root)])


@pytest.mark.parametrize('fn',[B.tool_args,B.harness_args])
def test_walls_as_office(fn,tmp):
    _office()
    out=_run(['sudo','-n','-u','office']+_args(fn,tmp)+[PY,'-c',PROBE]+[str(p) for p in SECRETS])
    assert out.stdout.strip()=='WALLS_OK',(out.stdout,out.stderr[-300:])


def test_tool_sandbox_never_binds_the_credential():
    args=B.tool_args('head',Path('/w'))
    assert not any('credential' in a for a in args)


def test_this_probe_goes_red_without_the_walls():
    """Gamma's red: same probe on plain bwrap as the owning uid must report the secrets and the local network path."""
    existing=[str(p) for p in SECRETS if p.exists()]
    assert existing
    out=_run(['/usr/bin/bwrap','--ro-bind','/','/','--proc','/proc','--dev','/dev','/usr/bin/python3','-c',PROBE]+existing)
    assert 'read:' in out.stdout and 'WALLS_OK' not in out.stdout,(out.stdout,out.stderr[-300:])


MARK='synthetic-credential-marker'
READ=r"""
import sys
try:print('GOT' if open(sys.argv[1]).read().strip()==sys.argv[2] else 'OTHER')
except OSError:print('CLOSED')
"""


def _seats(tmp):
    """Head's harness holds a known synthetic credential; Judge's seat holds another, for the other-seat path."""
    work=_bridge(tmp)
    state=tmp/'state';state.mkdir();(state/'.claude').mkdir();(state/'tmp').mkdir();(state/'session.jsonl').write_text(MARK)
    (work/'tmp').mkdir(exist_ok=True)
    (tmp/'seats/head/credentials.json').write_text(MARK)
    (tmp/'seats/judge').mkdir();(tmp/'seats/judge/credentials.json').write_text(MARK)
    subprocess.run(['chmod','-R','a+rwX',str(tmp)],check=True)
    return work,state


def _as_office(args,tmp,path):
    out=_run(['sudo','-n','-u','office']+_fixed(args,tmp)+[PY,'-c',READ,path,MARK])
    assert out.returncode==0,out.stderr[-300:]
    return out.stdout.strip()


CRED='/opt/workspace/.claude/.credentials.json'


def test_harness_reads_the_credential_and_the_tool_side_cannot(tmp):
    _office()
    work,state=_seats(tmp)
    assert _as_office(B.harness_args('head',state,bridge=tmp),tmp,CRED)=='GOT'
    tool=B.tool_args('head',work,bridge=tmp)
    for path in (CRED,'/opt/.credentials.json',str(tmp/'seats/head/credentials.json'),str(tmp/'seats/judge/credentials.json'),
                 '/srv/sniper-office/seats/head/credentials.json',str(state/'session.jsonl')):
        # tmp lives under /dev/shm, which the sandbox replaces, so the host paths must not resolve either.
        assert _as_office(tool,tmp,path)=='CLOSED',path


def test_tool_side_cannot_touch_harness_state(tmp):
    _office()
    work,state=_seats(tmp)
    assert str(state) not in B.tool_args('head',work,bridge=tmp)
    assert str(work) not in B.harness_args('head',state,bridge=tmp)


def test_published_runtime_runs_the_sdk_cli_as_office(tmp):
    """The real /srv runtime, not a copy: a seat cannot start without it."""
    if not (B.BRIDGE/'runtime/bin/python3').exists():_absent('runtime not published')
    _office()
    work,_=_seats(tmp)
    args=_fixed(B.tool_args('head',work,bridge=tmp),tmp)
    code="import claude_agent_sdk,pathlib,subprocess;d=pathlib.Path(claude_agent_sdk.__file__).parent/'_bundled';c=[p for p in d.iterdir() if p.name.startswith('claude')][0];print(subprocess.run([str(c),'--version'],capture_output=True,text=True).stdout.strip())"
    out=_run(['sudo','-n','-u','office']+args+[PY,'-c',code])
    assert 'Claude Code' in out.stdout,(out.stdout,out.stderr[-400:])


def test_tool_sandbox_has_no_host_socket():
    assert not any('host.sock' in a for a in B.tool_args('head',Path('/w')))


def test_a_workspace_symlink_to_the_credential_reads_nothing_as_office(tmp):
    """The host's note and notebook reads run this way. A link at the credential or a file in the bridge must come back empty."""
    _office()
    work,state=_seats(tmp)
    subprocess.run(['ln','-s',str(tmp/'seats/head/credentials.json'),str(work/'HANDOVER.md')],check=True)
    subprocess.run(['ln','-s','/srv/sniper-office/seats/head/credentials.json',str(work/'NOTEBOOK.md')],check=True)
    for name in ('HANDOVER.md','NOTEBOOK.md'):
        out=_run(['sudo','-n','-u','office']+_fixed(B.tool_args('head',work,bridge=tmp),tmp)+['/usr/bin/head','-c','100','/opt/workspace/'+name])
        assert MARK not in out.stdout and out.returncode!=0,(name,out.stdout)


def test_gpt_harness_has_no_credential_and_no_tool_proxy():
    from sniper.office import seat_sandbox as B
    args=B.codex_args('seat','/state',bridge='/b')
    assert not any('credentials' in a or 'auth' in a or 'tool-proxy' in a for a in args)
    assert '--unshare-net' in args and args[args.index('/b/seats/seat/model-proxy.sock')+1]=='/opt/proxy.sock'
