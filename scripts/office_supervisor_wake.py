#!/usr/bin/env python3
"""Host-side systemd wake: an unready Office never enters the kill slice."""
import argparse
import json
from pathlib import Path
import sys
import subprocess

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.office_watchdog import external_guard


def ready(path, *, now=None):
    try:
        path=Path(path)
        if path.is_symlink():return False
        data=json.loads(path.read_text())
        return (isinstance(data,dict) and data.get('guard_policy_version')=='office-host-guard-pilot-v3'
                and external_guard(path,now=now) is None)
    except (OSError,ValueError,TypeError):return False


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--guard',type=Path,required=True)
    if not ready(parser.parse_args(argv).guard):return 0
    try:
        return subprocess.run(['/usr/bin/systemctl','--user','start','office-supervisor.service'],
                              timeout=10,check=False).returncode
    except (OSError,subprocess.TimeoutExpired) as exc:
        print(json.dumps({'state':'wake_failed','reason':type(exc).__name__}),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
