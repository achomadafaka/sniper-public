"""Deploy-time release ancestry. No git calls belong in a web request or funded path."""
import argparse
import json
from pathlib import Path
import re
import subprocess
from sniper.util.atomic import atomic_write_json

NON_FUNDED_PREFIXES=('sniper/office/','config/office/','deploy/office/','scripts/office_','docs/','tests/')
NON_FUNDED_FILES=frozenset(('AGENTS.md','CLAUDE.md','MEMORY.md','README.md','STATUS.md',
    'static/office-v2.js','static/office-floor.js','static/office-floor.css',
    'sniper/routes/api_office.py','sniper/web/templates/office.html'))


def _sha(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{40}',value) is not None


def _unknown(runner=None,web=None):
    return dict(version=1,state='unknown',runner_sha=runner if _sha(runner) else None,web_sha=web if _sha(web) else None,
                runner_is_ancestor=None,web_is_ancestor=None,funded_paths_changed=None)


def build(repo,runner,web):
    result=_unknown(runner,web)
    if not _sha(runner) or not _sha(web):return result
    try:
        def git(*args):
            return subprocess.run(['git','-C',str(repo),*args],stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,timeout=3)
        ancestors=[git('merge-base','--is-ancestor',a,b) for a,b in ((runner,web),(web,runner))]
        diff=git('diff','--no-ext-diff','--no-renames','--name-only','-z',runner,web,'--')
        if any(r.returncode not in (0,1) for r in ancestors) or diff.returncode!=0:return result
        paths=[p for p in diff.stdout.decode('utf-8').split('\0') if p]
        funded=[p for p in paths if p not in NON_FUNDED_FILES and not p.startswith(NON_FUNDED_PREFIXES)]
        result.update(state='known',runner_is_ancestor=ancestors[0].returncode==0,
                      web_is_ancestor=ancestors[1].returncode==0,funded_paths_changed=bool(funded),
                      changed_paths_n=len(paths),funded_paths=funded[:10])
    except (OSError,subprocess.TimeoutExpired,UnicodeError):pass
    return result


def read(path):
    try:
        with Path(path).open('rb') as f:raw=f.read(16385)
        if len(raw)>16384:return _unknown()
        value=json.loads(raw)
        if (not isinstance(value,dict) or value.get('version')!=1 or value.get('state')!='known'
            or not all(_sha(value.get(k)) for k in ('runner_sha','web_sha'))
            or not all(type(value.get(k)) is bool for k in ('runner_is_ancestor','web_is_ancestor','funded_paths_changed'))):
            return _unknown()
        if value['runner_sha']!=value['web_sha'] and value['runner_is_ancestor'] and value['web_is_ancestor']:
            return _unknown()
        return value
    except (OSError,ValueError,UnicodeError,RecursionError):return _unknown()


def write_for_deploy(repo,state,web,output):
    runner=None
    try:
        with (Path(state)/'data/memecoin_runner.heartbeat').open('rb') as f:raw=f.read(262145)
        if len(raw)<=262144:
            value=json.loads(raw)
            if isinstance(value,dict) and isinstance(value.get('lifi'),dict):runner=value['lifi'].get('release_sha')
    except (OSError,ValueError,UnicodeError,RecursionError):pass
    atomic_write_json(output,build(repo,runner,web),mode=0o644)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source-root','state-root','web-sha','output'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    write_for_deploy(args.source_root,args.state_root,args.web_sha,args.output)
