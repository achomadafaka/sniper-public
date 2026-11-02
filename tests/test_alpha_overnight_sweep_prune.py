"""The overnight sweep forgets by time, not by spelling (Codex CORE-03, 2026-10-07).

`st["seen"] = sorted(seen)[-5000:]` kept the lexically greatest ids. UUID order has nothing to do
with completion time, so the newest attempt could be dropped at once (the audit's probe: 5,000
'f...' ids and one '00000000-newest') and reported again by the next run's 2 h lookback. Seen ids
now keep their completion time and expire once they fall out of that lookback; the state is
replaced atomically; an unreadable state file is said out loud, never presented as a clean sweep.
"""
import importlib.util
import io
import json
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "alpha_overnight_sweep.py"


def _load():
    spec = importlib.util.spec_from_file_location("alpha_overnight_sweep_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _root(tmp_path):
    (tmp_path / "data").mkdir()
    main = sqlite3.connect(tmp_path / "sniper_tracker.db")
    main.executescript(
        "CREATE TABLE memecoin_wallets(source_user_id TEXT, real_chains TEXT, real_approved INTEGER);"
        "INSERT INTO memecoin_wallets VALUES('u1','8453',1);"
        "CREATE TABLE memecoin_entry_attempts(attempt_id TEXT, network_id TEXT, source_user_id TEXT,"
        " source_handle TEXT, ticker TEXT, delivery_lane TEXT, terminal_status TEXT, terminal_reason TEXT,"
        " completed_at TEXT, trace_json TEXT);")
    main.commit()
    ft = sqlite3.connect(tmp_path / "data" / "memecoin_fill_timing.db")
    ft.execute("CREATE TABLE memecoin_fill_timing(attempt_id TEXT, network_id TEXT, lane TEXT, gap INTEGER,"
               " cause TEXT, cause_detail TEXT, completed_at TEXT)")
    ft.commit()
    return main


def _refusal(main, aid, at):
    main.execute("INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (aid, "8453", "u1", "h", "T", "onchain_watch", "aborted", "provider_down", at, "{}"))
    main.commit()


def _run(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    monkeypatch.setattr(mod, "STATE", str(tmp_path / "data" / "state.json"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main()
    return buf.getvalue()


def _iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_the_newest_lexically_low_id_survives_and_is_reported_once(tmp_path, monkeypatch):
    """THE regression: the newest id was evicted at once and re-reported by the lookback."""
    mod = _load()
    main = _root(tmp_path)
    state = {"attempts": _iso(30), "timing": _iso(30),
             "seen": {"f%08d" % i: _iso(20) for i in range(5000)}}
    (tmp_path / "data" / "state.json").write_text(json.dumps(state))
    _refusal(main, "00000000-newest", _iso(1))
    first = _run(mod, tmp_path, monkeypatch)
    second = _run(mod, tmp_path, monkeypatch)
    assert first.count("00000000") == 1 and "00000000" not in second, (first, second)


def test_ids_older_than_the_lookback_expire(tmp_path, monkeypatch):
    mod = _load()
    _root(tmp_path)
    state = {"attempts": _iso(5), "timing": _iso(5), "seen": {"zzz-old": _iso(300), "aaa-new": _iso(10)}}
    (tmp_path / "data" / "state.json").write_text(json.dumps(state))
    _run(mod, tmp_path, monkeypatch)
    seen = json.loads((tmp_path / "data" / "state.json").read_text())["seen"]
    assert "aaa-new" in seen and "zzz-old" not in seen


def test_an_old_list_state_still_dedupes(tmp_path, monkeypatch):
    mod = _load()
    main = _root(tmp_path)
    _refusal(main, "att00001-x", _iso(10))           # the line prints ref=<first 8 chars>
    (tmp_path / "data" / "state.json").write_text(json.dumps(
        {"attempts": _iso(5), "timing": _iso(5), "seen": ["att00001-x"]}))
    assert "att00001" not in _run(mod, tmp_path, monkeypatch)


def test_an_unreadable_state_is_said_not_hidden(tmp_path, monkeypatch):
    mod = _load()
    _root(tmp_path)
    (tmp_path / "data" / "state.json").write_text("{torn")
    out = _run(mod, tmp_path, monkeypatch)
    assert "STATE UNREADABLE" in out


def test_the_state_is_replaced_atomically(tmp_path, monkeypatch):
    import os
    mod = _load()
    _root(tmp_path)
    (tmp_path / "data" / "state.json").write_text(json.dumps({"attempts": _iso(5), "timing": _iso(5), "seen": {}}))
    real_replace, calls = os.replace, []

    def replace(src, dst):
        calls.append(dst)
        return real_replace(src, dst)
    monkeypatch.setattr(mod.os, "replace", replace)
    _run(mod, tmp_path, monkeypatch)
    assert calls == [str(tmp_path / "data" / "state.json")]
