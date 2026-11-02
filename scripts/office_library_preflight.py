#!/usr/bin/env python3
"""Audit only the approved user-site path, then import SDK/options in a networkless credential-free jail."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sniper.office.library_audit import audit

CODE="""import claude_agent_sdk,pydantic,jsonschema
from sniper.office.model_child import claude_options
claude_options(dict(model='fixture-model',system='fixture',schema={'type':'object'}))
print('SDK_OPTIONS_AND_IMPORTS_OK')
"""


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-root',type=Path,required=True)
    args=parser.parse_args(argv)
    release=args.release_root.resolve(strict=True)
    prefix=Path.home()/'.pyenv/versions/3.13.0'
    site=Path.home()/'.local/lib/python3.13/site-packages'
    receipt=audit(site)
    with tempfile.TemporaryDirectory(prefix='office-import-') as work:
        Path(work,'home').mkdir()
        command=['/usr/bin/bwrap','--unshare-user','--unshare-pid','--unshare-net','--ro-bind','/','/',
            '--proc','/proc','--dev','/dev','--tmpfs','/home','--tmpfs','/root','--tmpfs','/run','--tmpfs','/tmp','--tmpfs','/opt',
            '--ro-bind',str(prefix),'/opt/python','--ro-bind',str(site),'/opt/site',
            '--ro-bind',str(release),'/opt/office','--bind',work,'/opt/scratch','--cap-drop','ALL','--clearenv',
            '--setenv','HOME','/opt/scratch/home','--setenv','PYTHONPATH','/opt/office:/opt/site',
            '--setenv','LD_LIBRARY_PATH','/opt/python/lib','/opt/python/bin/python3','-s','-c',CODE]
        result=subprocess.run(command,capture_output=True,text=True,timeout=5)
        passed=result.returncode==0 and result.stdout.strip()=='SDK_OPTIONS_AND_IMPORTS_OK'
        report={'version':'office-library-preflight-v1','state':'passed' if passed else 'refused',
                'audit':receipt,'argv':command,'returncode':result.returncode,
                'output':result.stdout.strip() if passed else None,'network':'disabled','credentials':'absent',
                'source_hashes':{name:hashlib.sha256((release/name).read_bytes()).hexdigest()
                                 for name in ('sniper/office/model_child.py','scripts/office_model_child.py')},
                'counts_as_evidence':False}
    print(json.dumps(report,allow_nan=False))
    return 0 if passed else 2


if __name__=='__main__':
    raise SystemExit(main())
