"""The funded-contract gate must grade live rows with the RELEASE's code, never a stale checkout.

2026-10-21: a deploy whose checkout sat on a pre-#482 branch ran its own copy of the validator with
PYTHONPATH=$RELEASE; the checkout's policy reader refused source-mirror-tp100 rows that the release
accepts, and the deploy was refused. The validator now takes --code-root, puts it first on sys.path,
and refuses (exit 3) when the sniper package it imported lives anywhere else.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sqlite3
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "validate_memecoin_funded_contracts.py"


def _run(code_root, *, pythonpath, cwd, tmp):
    db = tmp / "ledger.db"
    sqlite3.connect(db).close()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(pythonpath)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--code-root", str(code_root),
         "--state-root", str(tmp), "--db", str(db)],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


def test_a_sniper_package_outside_the_code_root_refuses(tmp_path):
    stale_root = tmp_path / "release_without_sniper"
    stale_root.mkdir()
    out = _run(stale_root, pythonpath=REPO, cwd=tmp_path, tmp=tmp_path)
    assert out.returncode == 3, out.stderr
    assert "REFUSED: sniper imported from" in out.stderr


def test_the_code_root_wins_over_another_tree_on_pythonpath(tmp_path):
    decoy = tmp_path / "decoy"
    (decoy / "sniper").mkdir(parents=True)
    (decoy / "sniper" / "__init__.py").write_text("raise SystemExit('decoy sniper imported')\n")
    out = _run(REPO, pythonpath=decoy, cwd=tmp_path, tmp=tmp_path)
    assert "decoy sniper imported" not in (out.stdout + out.stderr)
    assert "REFUSED: sniper imported from" not in out.stderr


def test_import_escape_names_both_paths(tmp_path):
    spec = importlib.util.spec_from_file_location("validator_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.import_escape(REPO / "sniper" / "__init__.py", REPO) is None
    text = mod.import_escape(REPO / "sniper" / "__init__.py", tmp_path)
    assert str(tmp_path) in text and "REFUSED" in text


def test_the_deploy_passes_the_release_as_code_root():
    body = (REPO / "scripts" / "deploy_memecoin_runtime.sh").read_text()
    call = body[body.index("validate_funded_contracts() {"):]
    call = call[:call.index("}")]
    assert '"$RELEASE/scripts/validate_memecoin_funded_contracts.py"' in call
    assert '--code-root "$RELEASE"' in call
