"""All process discovery, stop and launch operations are fake in this whole-watchdog check."""
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('stop_ok', [True, False])
@pytest.mark.parametrize('duplicate_profile', [False, True])
def test_shyft_rh_replacement_waits_for_exact_old_stop_before_launch(tmp_path, stop_ok, duplicate_profile):
    release = tmp_path / '.runtime/memecoin/releases/new'
    (release / 'scripts').mkdir(parents=True)
    (release / 'scripts/memecoin_rh_feed_shadow_loop.py').write_text('# fake')
    (tmp_path / '.runtime/memecoin/current').symlink_to(release)
    (tmp_path / 'data').mkdir()
    (tmp_path / '.env').write_text('MEMECOIN_SOLANA_INFRA_PROVIDER=shyft\nMEMECOIN_RH_FAST_LANE=live\n'
        + ('MEMECOIN_SOLANA_INFRA_PROVIDER=legacy\n' if duplicate_profile else ''))
    calls = tmp_path / 'calls'
    fake = tmp_path / 'python'
    fake.write_text('''#!/usr/bin/env python3
import os, pathlib, sys
root=pathlib.Path(os.environ['SNIPER_STATE_ROOT']); a=sys.argv[1:]
if '-c' in a: print('123 '+str(root/'.runtime/memecoin/releases/old/scripts/memecoin_rh_feed_shadow_loop.py'))
elif '--process-record-script' in a:
    with (root/'calls').open('a') as f: f.write('capture\\n')
    print('{}')
elif '--stop-process-record' in a:
    with (root/'calls').open('a') as f: f.write('stop\\n')
    sys.exit(0 if os.environ['STOP_OK']=='1' else 1)
''')
    fake.chmod(0o755)
    bin_dir = tmp_path / 'bin'; bin_dir.mkdir()
    (bin_dir / 'setsid').write_text('#!/bin/bash\necho launch >> "$SNIPER_STATE_ROOT/calls"\n')
    (bin_dir / 'setsid').chmod(0o755)
    (bin_dir / 'sleep').write_text('#!/bin/bash\nexit 0\n'); (bin_dir / 'sleep').chmod(0o755)
    (bin_dir / 'ps').write_text('#!/bin/bash\nexit 0\n'); (bin_dir / 'ps').chmod(0o755)
    source = Path(__file__).resolve().parents[1] / 'scripts/memecoin_rh_feed_shadow_watchdog.sh'
    r = subprocess.run(['bash', str(source)], capture_output=True, text=True, timeout=10,
        env=os.environ | dict(SNIPER_STATE_ROOT=str(tmp_path), SNIPER_PYTHON=str(fake),
            STOP_OK=str(int(stop_ok)), PATH=str(bin_dir)+':/usr/bin:/bin'))
    assert r.returncode == (0 if stop_ok else 1), r.stderr
    lines = calls.read_text().splitlines() if calls.exists() else []
    assert lines[:2] == ['capture', 'stop']
    assert ('launch' in lines) == stop_ok


@pytest.mark.parametrize('discovery', ['healthy', 'empty', 'unavailable'])
def test_rh_discovery_uses_pinned_release_and_holds_unknown(tmp_path, discovery):
    """Use real Python imports; launcher seams never start or stop a process."""
    release = tmp_path / '.runtime/memecoin/releases/new'
    for root in (tmp_path, release):
        (root / 'scripts').mkdir(parents=True)
        (root / 'scripts/__init__.py').write_text('')
    (tmp_path / 'scripts/memecoin_runtime_verify.py').write_text(
        "raise RuntimeError('mutable state checkout imported')\n")
    match = release / 'scripts/memecoin_rh_feed_shadow_loop.py'
    match.write_text('# never executed\n')
    body = ("raise RuntimeError('release discovery unavailable')" if discovery == 'unavailable'
            else f"return {[(123, str(match))] if discovery == 'healthy' else []!r}")
    (release / 'scripts/memecoin_runtime_verify.py').write_text(
        f'def _owned_script_rows(*args):\n    {body}\n')
    (tmp_path / '.runtime/memecoin/current').symlink_to(release)
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/memecoin_rh_feed_shadow.heartbeat').write_text('{}')
    (tmp_path / '.env').write_text(
        'MEMECOIN_SOLANA_INFRA_PROVIDER=shyft\nMEMECOIN_RH_FAST_LANE=live\n')
    bin_dir = tmp_path / 'bin'; bin_dir.mkdir()
    for name, body in {'setsid': 'echo launch >> "$SNIPER_STATE_ROOT/calls"',
                       'sleep': 'exit 0'}.items():
        stub = bin_dir / name
        stub.write_text('#!/bin/bash\n' + body + '\n'); stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('SNIPER_', 'MEMECOIN_', 'PYTHON'))}
    env.update(SNIPER_STATE_ROOT=str(tmp_path), SNIPER_PYTHON=sys.executable,
               PATH=str(bin_dir) + ':/usr/bin:/bin')
    source = Path(__file__).resolve().parents[1] / 'scripts/memecoin_rh_feed_shadow_watchdog.sh'
    result = subprocess.run(['bash', str(source)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=10)
    calls = tmp_path / 'calls'
    if discovery == 'unavailable':
        assert result.returncode != 0, 'unknown discovery was treated as no owned process'
        assert not calls.exists(), 'unknown discovery launched a replacement'
    else:
        assert result.returncode == 0, result.stderr
        assert 'mutable state checkout imported' not in result.stderr
        assert calls.exists() == (discovery == 'empty')
