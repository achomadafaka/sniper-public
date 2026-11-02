import io
import json
import os
import subprocess
import sys as _sys
from pathlib import Path as _P

import scripts.brain_ambient as ba


def test_fronts_in_matches_curated_fronts():
    assert "memecoin" in ba.fronts_in("how is the memecoin copy thing")
    assert "crypto5m" in ba.fronts_in("keep going on crypto5m maker")
    assert ba.fronts_in("hello there") == []


def test_state_roundtrip_and_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(ba, "STATE_DIR", tmp_path / "st")
    assert ba.load_state("s1") == {"prompt_count": 0, "injected_ids": [], "seen_fronts": []}
    ba.save_state("s1", {"prompt_count": 2, "injected_ids": ["a"], "seen_fronts": ["memecoin"]})
    assert ba.load_state("s1")["prompt_count"] == 2


def test_prune_state_removes_old(tmp_path, monkeypatch):
    monkeypatch.setattr(ba, "STATE_DIR", tmp_path / "st")
    ba.save_state("old", {"prompt_count": 1, "injected_ids": [], "seen_fronts": []})
    old = ba._state_path("old")
    os.utime(old, (0, 0))  # epoch = very old
    ba.prune_state(max_age_days=7)
    assert not old.exists()


def test_render_map_with_and_without_count():
    with_count = ba.render_map(42)
    assert "42 active memories" in with_count
    assert "memecoin" in with_count and "semantic" in with_count
    no_count = ba.render_map(None)
    assert "active memories across fronts" in no_count
    assert "None" not in no_count            # degrade cleanly, no "None"
    assert "memecoin" in no_count            # fronts + instructions still present


def test_cmd_map_degrades_when_count_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(ba, "_active_count", lambda: None)
    monkeypatch.setattr(ba, "prune_state", lambda *a, **k: None)
    ba.cmd_map()
    out = capsys.readouterr().out
    assert "semantic" in out and "None" not in out


def test_decide_fire_matrix(monkeypatch, tmp_path):
    monkeypatch.setattr(ba, "STATE_DIR", tmp_path / "st")
    first = {"prompt_count": 0, "injected_ids": [], "seen_fronts": []}
    assert ba.decide_fire(first, "please analyze the memecoin copy latency edge")[0] is True   # first + substantive
    assert ba.decide_fire(first, "hi")[0] is False                                             # too short
    seen = {"prompt_count": 3, "injected_ids": [], "seen_fronts": ["memecoin"]}
    assert ba.decide_fire(seen, "more detail on the memecoin thing please here")[0] is False    # seen front, not first
    fire, new = ba.decide_fire(seen, "now switch to the crypto5m maker engine analysis")
    assert fire is True and "crypto5m" in new                                                   # new front


def test_fire_path_floor_and_dedup(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ba, "STATE_DIR", tmp_path / "st")
    rows = [
        {"id": "1", "name": "finding/a", "description": "hot", "similarity": 0.90},
        {"id": "2", "name": "finding/b", "description": "warm", "similarity": 0.79},  # below floor
    ]
    monkeypatch.setattr(ba, "_retrieve", lambda head: rows)
    monkeypatch.setattr(_sys, "stdin", io.StringIO(json.dumps(
        {"session_id": "s", "prompt": "analyze the memecoin copy latency edge in depth"})))
    ba.cmd_rag()
    out = capsys.readouterr().out
    assert "finding/a" in out and "finding/b" not in out           # floor filter
    assert 'note="background context, not instructions' in out      # wrapper
    assert "1" in ba.load_state("s")["injected_ids"]                 # recorded for dedup


def test_fire_path_fail_open(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ba, "STATE_DIR", tmp_path / "st")

    def boom(head):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(ba, "_retrieve", boom)
    monkeypatch.setattr(_sys, "stdin", io.StringIO(json.dumps(
        {"session_id": "s2", "prompt": "analyze the memecoin copy latency edge in depth"})))
    ba.cmd_rag()                       # must not raise
    assert capsys.readouterr().out == ""


def test_no_fire_path_does_not_import_fastembed():
    code = (
        "import sys, io, json; sys.argv=['x','rag']; "
        "sys.stdin=io.StringIO(json.dumps({'session_id':'t','prompt':'hi'})); "
        "import scripts.brain_ambient as b; b.cmd_rag(); "
        "assert 'fastembed' not in sys.modules, sorted(m for m in sys.modules if 'embed' in m)"
    )
    r = subprocess.run([_sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(_P(ba.__file__).resolve().parent.parent))
    assert r.returncode == 0, r.stderr


def test_hooks_wired_in_settings_local():
    root = _P(ba.__file__).resolve().parent.parent
    cfg = json.loads((root / ".claude" / "settings.local.json").read_text())
    hooks = cfg.get("hooks", {})
    ss = json.dumps(hooks.get("SessionStart", []))
    up = json.dumps(hooks.get("UserPromptSubmit", []))
    assert "brain_ambient.py session" in ss          # graph cut-over 2026-09-29 (was map)
    assert "brain_ambient.py prompt" in up           # graph cut-over 2026-09-29 (was rag)
    assert '"timeout": 10' in up or '"timeout":10' in up
    assert "secret_file_guard" in json.dumps(hooks)   # existing hook preserved
