"""Office v2 sandbox arguments. Harness: credential, no tools. Tool sandbox: tools, no credential. Neither has a network namespace route out."""
from pathlib import Path

BRIDGE=Path('/srv/sniper-office')
OFFICE_HOME=Path('/var/lib/sniper-office')
# /var/lib/sniper-office and /srv hold every seat's state: masked, then only this seat's parts bound back.
MASKS=('/home','/root','/run','/tmp','/opt','/srv',str(OFFICE_HOME),'/mnt','/media')


def _base(workspace):
    args=['/usr/bin/bwrap','--unshare-user','--unshare-pid','--unshare-net','--unshare-ipc','--unshare-uts','--die-with-parent','--new-session',
          '--ro-bind','/','/','--proc','/proc','--dev','/dev']
    for path in MASKS:args+=['--tmpfs',path]
    args+=['--ro-bind',str(BRIDGE/'runtime'),'/opt/runtime','--ro-bind',str(BRIDGE/'release'),'/opt/release']
    args+=['--bind',str(workspace),'/opt/workspace','--chdir','/opt/workspace','--cap-drop','ALL','--clearenv',
           '--setenv','HOME','/opt/workspace','--setenv','PATH','/opt/runtime/bin:/usr/bin:/bin','--setenv','TMPDIR','/opt/workspace/tmp',
           '--setenv','PYTHONHOME','/opt/runtime','--setenv','LD_LIBRARY_PATH','/opt/runtime/lib']
    return args


def tool_args(actor,workspace,*,bridge=BRIDGE):
    """No credential path and no host socket here. ca.pem and the proxy socket are this seat's only other inputs."""
    seat=Path(bridge)/'seats'/actor
    ca='/opt/ca.pem'
    args=_base(workspace)+['--ro-bind',str(seat/'ca.pem'),ca,'--ro-bind',str(seat/'tool-proxy.sock'),'/opt/proxy.sock']
    args+=['--setenv','PLAYWRIGHT_BROWSERS_PATH','/opt/runtime/browsers']
    for name in ('SSL_CERT_FILE','REQUESTS_CA_BUNDLE','CURL_CA_BUNDLE','NODE_EXTRA_CA_CERTS'):args+=['--setenv',name,ca]
    return args


def harness_args(actor,state,*,bridge=BRIDGE):
    """`state` is never the tool workspace: session files there could be rewritten from the tool side. The host passes the note in."""
    seat=Path(bridge)/'seats'/actor
    return _base(state)+['--ro-bind',str(seat/'credentials.json'),'/opt/workspace/.claude/.credentials.json',
                         '--ro-bind',str(seat/'model-proxy.sock'),'/opt/proxy.sock','--ro-bind',str(seat/'host.sock'),'/opt/host.sock']


def codex_args(actor,state,*,bridge=BRIDGE,host_sock=True):
    """No credential at all: the real token stays on the host, in the model proxy. Native shell runs in here, with these same walls."""
    seat=Path(bridge)/'seats'/actor
    return _base(state)+['--ro-bind',str(seat/'codex'),'/opt/codex','--ro-bind',str(seat/'codex-code-mode-host'),'/opt/codex-code-mode-host','--ro-bind',str(seat/'ca.pem'),'/opt/ca.pem',
                         '--ro-bind',str(seat/'model-proxy.sock'),'/opt/proxy.sock']+(['--ro-bind',str(seat/'host.sock'),'/opt/host.sock'] if host_sock else [])


def unit_args(unit,*,runtime_s,memory_max=None):
    """A wake runs as a transient unit of the office user's manager: `office-seat-*` is what the lifecycle check looks for."""
    args=['sudo','-n','-u','office','env','XDG_RUNTIME_DIR=/run/user/%d'%office_uid(),'systemd-run','--user','--wait','--pipe','--collect','--quiet',
          '--unit='+unit,'--property=KillMode=control-group','--property=TimeoutStopSec=5','--property=RuntimeMaxSec=%d'%runtime_s,
          '--property=Nice=19','--property=IOSchedulingClass=idle']
    if memory_max:args.append('--property=MemoryMax=%d'%memory_max)
    return args


def office_uid():
    import pwd
    return pwd.getpwnam('office').pw_uid
