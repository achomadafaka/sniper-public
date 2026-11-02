import os
import subprocess
from test_deploy_prune_skips_referenced import _function

def test_large_crontab_reference_survives_pipefail(tmp_path):
 releases=tmp_path/'releases'
 for i in range(6):
  d=releases/f'r{i}';d.mkdir(parents=True);os.utime(d,(1000000-i*100,)*2)
 shim=tmp_path/'bin';shim.mkdir()
 (shim/'crontab').write_text('#!/usr/bin/python3\nimport sys\nsys.stdout.write('+repr('* * * * * '+str(releases)+'/r5/scripts/x.sh\n'+'# filler\n'*100000)+')\n')
 (shim/'crontab').chmod(0o755)
 (shim/'git').write_text('#!/bin/sh\n[ "$3" = worktree ] && [ "$4" = remove ] && rm -rf "${6:-$5}"\nexit 0\n');(shim/'git').chmod(0o755)
 script=f'set -euo pipefail\nRELEASES={releases}; RELEASE={releases}/r0; OLD_RELEASE={releases}/r1; SOURCE_ROOT={tmp_path}; MEMECOIN_RELEASE_KEEP=3\n'+_function()+'\nprune_old_releases\n'
 subprocess.run(['bash','-c',script],check=True,env=dict(os.environ,PATH=f'{shim}:/usr/bin:/bin'))
 assert (releases/'r5').exists()
