"""Run the setup shell with inert Docker/curl commands; never touch a real stack."""
import json
import os
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/brain-local/setup.sh"


def _setup(tmp_path, *, restore_rc=0, list_rc=0, memories="42"):
    conf = tmp_path / ".config/sniper-brain"
    conf.mkdir(parents=True)
    (conf / "brain.env").write_text("offline")
    bins = tmp_path / "bin"
    bins.mkdir()
    docker = bins / "docker"
    docker.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
a = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as f:
    f.write(json.dumps(a) + '\\n')
if a[0] == 'compose':
    if 'ps' in a: print('offline-db')
elif a[0] == 'cp':
    if a[-1].endswith('restore.list'):
        pathlib.Path(os.environ['TOC_COPY']).write_text(pathlib.Path(a[1]).read_text())
elif 'pg_restore' in a:
    if '--list' in a:
        print('1; 2615 2200 SCHEMA - public pg_database_owner')
        print('2; 1259 100 TABLE public memories postgres')
        print('3; 0 100 TABLE DATA public memories postgres')
        print('4; 0 0 ACL - SCHEMA public postgres')
        print('5; 0 0 ACL public TABLE memories postgres')
        print('6; 0 0 ACL public FUNCTION brain_search(text) postgres')
        sys.exit(int(os.environ['LIST_RC']))
    sys.exit(int(os.environ['RESTORE_RC']))
elif 'psql' in a and any('SELECT' in x for x in a):
    print(os.environ['MEMORIES'])
""")
    docker.chmod(0o755)
    curl = bins / "curl"
    curl.write_text("#!/bin/sh\nprintf '%s\\n' GATEWAY_HEALTH\n")
    curl.chmod(0o755)
    dump = tmp_path / "brain.dump"
    dump.write_bytes(b"inert archive")
    calls = tmp_path / "calls.jsonl"
    toc = tmp_path / "toc"
    env = dict(os.environ, HOME=str(tmp_path), PATH=str(bins) + os.pathsep + os.environ["PATH"],
               CALLS=str(calls), TOC_COPY=str(toc), LIST_RC=str(list_rc),
               RESTORE_RC=str(restore_rc), MEMORIES=memories)
    result = subprocess.run(["bash", str(SCRIPT), str(dump)], env=env,
                            text=True, capture_output=True, timeout=10)
    return result, [json.loads(line) for line in calls.read_text().splitlines()], toc


@pytest.mark.parametrize("restore_rc,list_rc", [(2, 0), (0, 2)])
def test_failed_restore_stops_before_grants_and_gateway(tmp_path, restore_rc, list_rc):
    result, calls, _ = _setup(tmp_path, restore_rc=restore_rc, list_rc=list_rc)
    assert result.returncode != 0
    assert not any("psql" in call for call in calls)
    assert "GATEWAY_HEALTH" not in result.stdout


@pytest.mark.parametrize("memories", ["0", "", "garbage"])
def test_missing_restored_brain_data_cannot_report_health(tmp_path, memories):
    result, _, _ = _setup(tmp_path, memories=memories)
    assert result.returncode != 0
    assert "GATEWAY_HEALTH" not in result.stdout


def test_restore_is_atomic_and_preserves_object_acl(tmp_path):
    result, calls, toc = _setup(tmp_path)
    assert result.returncode == 0, result.stderr
    (restore,) = [c for c in calls if "pg_restore" in c and "--list" not in c]
    assert {"--single-transaction", "--exit-on-error", "--use-list=/tmp/restore.list"} <= set(restore)
    assert "--no-acl" not in restore
    assert toc.read_text().splitlines() == ["2; 1259 100 TABLE public memories postgres",
                                         "3; 0 100 TABLE DATA public memories postgres",
                                         "5; 0 0 ACL public TABLE memories postgres",
                                         "6; 0 0 ACL public FUNCTION brain_search(text) postgres"]
    assert any("psql" in c and any("public.memories" in x for x in c) for c in calls)
    assert "GATEWAY_HEALTH" in result.stdout
