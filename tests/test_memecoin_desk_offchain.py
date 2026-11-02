"""Desk offchain heat, per-coin collector: paid never organic, a down source is null not zero, rows only on
change, collect-only (temporary files only)."""
import importlib.util
import json
import sqlite3
from pathlib import Path

from sniper.services import memecoin_desk_offchain as O

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("offchain", ROOT / "scripts" / "memecoin_desk_offchain.py")
OW = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(OW)
NOW = 1792600000.0
DAY = "2026-10-21"


def _dex(base, items, *, healthy=True):
    d = base / "dexscreener"
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / (DAY + ".db"))
    conn.executescript("CREATE TABLE polls (id INTEGER PRIMARY KEY, endpoint TEXT, received_at REAL, status INTEGER, "
                       "items INTEGER, error TEXT, source TEXT DEFAULT 'dexscreener');"
                       "CREATE TABLE items (poll_id INTEGER, endpoint TEXT, received_at REAL, chain_id TEXT, "
                       "token_address TEXT, payload_json TEXT, source TEXT DEFAULT 'dexscreener');")
    for ep in O.PAID_ENDPOINTS:
        conn.execute("INSERT INTO polls(endpoint, received_at, status) VALUES(?,?,?)",
                     (ep, NOW - 30, 200 if healthy else 429))
    conn.executemany("INSERT INTO items(poll_id, endpoint, received_at, chain_id, token_address, payload_json) "
                     "VALUES(1,?,?,?,?,?)", items)
    conn.commit()
    conn.close()


def _x(base, links, *, beat_age=5):
    d = base / "x"
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / (DAY + ".db"))
    conn.executescript("CREATE TABLE posts (id INTEGER PRIMARY KEY, event TEXT, tweet_id TEXT, received_at REAL, "
                       "created_at_ms INTEGER, handle TEXT, author_id TEXT, post_type TEXT, payload_json TEXT);"
                       "CREATE TABLE token_meta (id INTEGER PRIMARY KEY, tweet_id TEXT, mint TEXT, chain TEXT, "
                       "symbol TEXT, received_at REAL, payload_json TEXT);")
    for tweet, handle, mint, t in links:
        conn.execute("INSERT INTO posts(event, tweet_id, received_at, handle, payload_json) VALUES('t',?,?,?,'{}')",
                     (tweet, t, handle))
        conn.execute("INSERT INTO token_meta(tweet_id, mint, chain, received_at, payload_json) "
                     "VALUES(?,?,'solana',?,'{}')", (tweet, mint, t))
    conn.commit()
    conn.close()
    (d / "producer.heartbeat").write_text(json.dumps({"ts": NOW - beat_age}))


def _rows(base):
    conn = sqlite3.connect(base / "offchain" / "offchain.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM coin_heat ORDER BY id")]
    finally:
        conn.close()


def test_paid_and_organic_never_mix_and_promoters_are_not_organic(tmp_path):
    _dex(tmp_path, [("/token-boosts/top/v1", NOW - 60, "solana", "M1", json.dumps({"totalAmount": 500})),
                    ("/ads/latest/v1", NOW - 60, "solana", "M1", "{}"),
                    ("/token-profiles/latest/v1", NOW - 7200, "solana", "M1", "{}"),        # older than the hour
                    ("/token-boosts/top/v1", NOW - 60, "ethereum", "E1", json.dumps({"totalAmount": 9}))])
    _x(tmp_path, [("t1", "alice", "M1", NOW - 100), ("t2", "Shill", "M1", NOW - 100), ("t3", "bob", "M1", NOW - 2000)])
    (tmp_path / "offchain").mkdir()
    (tmp_path / "offchain" / "promoters.json").write_text(json.dumps({"version": "promoter-list-v1", "handles": ["@shill"]}))
    stats = OW.run_pass(tmp_path, now_fn=lambda: NOW)
    assert stats["written"] == 1 and stats["mints"] == 1                          # ethereum never counted
    (row,) = _rows(tmp_path)
    assert (row["paid_boost_units"], row["paid_ads"], row["paid_profile"], row["listing_takeover"]) == (500.0, 1, 0, 0)
    assert (row["x_mentions_15m"], row["x_accounts_15m"], row["x_promoter_mentions_15m"]) == (1, 1, 1)
    assert row["promoter_list_version"] == "promoter-list-v1" and row["offchain_version"] == "desk-offchain-coin-v1"


def test_a_down_source_is_missing_never_zero(tmp_path):
    _dex(tmp_path, [("/token-boosts/top/v1", NOW - 60, "solana", "M1", json.dumps({"totalAmount": 5}))], healthy=False)
    _x(tmp_path, [("t1", "alice", "M1", NOW - 100)], beat_age=600)
    OW.run_pass(tmp_path, now_fn=lambda: NOW)
    (row,) = _rows(tmp_path)
    assert (row["paid_boost_units"], row["paid_ads"], row["dex_state"]) == (None, None, "missing")
    assert (row["x_mentions_15m"], row["x_state"]) == (None, "missing")


def test_a_healthy_source_with_nothing_is_a_known_zero_and_rows_are_written_only_on_change(tmp_path):
    _dex(tmp_path, [("/community-takeovers/latest/v1", NOW - 60, "solana", "M1", "{}")])
    _x(tmp_path, [])
    OW.run_pass(tmp_path, now_fn=lambda: NOW)
    (row,) = _rows(tmp_path)
    assert (row["paid_boost_units"], row["listing_takeover"], row["x_mentions_15m"]) == (0.0, 1, 0)
    assert OW.run_pass(tmp_path, now_fn=lambda: NOW + 1)["written"] == 0           # unchanged: not written again


def test_v0_promoter_list_is_seeded_empty_and_a_bad_version_refuses(tmp_path):
    assert OW.promoters(tmp_path) == ("promoter-list-v0", set())
    (tmp_path / "offchain" / "promoters.json").write_text(json.dumps({"version": "v2", "handles": []}))
    import pytest
    with pytest.raises(SystemExit, match="promoter-list-vN"):
        OW.promoters(tmp_path)


def test_the_crawler_intent_is_attached_only_when_known_by_the_window(tmp_path):
    from sniper.services import memecoin_desk_crawler as CR
    (tmp_path / "crawler").mkdir()
    conn = CR.connect(tmp_path / "crawler" / "crawls.db")
    conn.execute("INSERT INTO crawls(network_id, mint, known_at, input_hash, inputs_json, intent, prompt_version) "
                 "VALUES('solana','M2',?,'h','{}','warning','p')", (NOW - 50,))
    conn.execute("INSERT INTO crawls(network_id, mint, known_at, input_hash, inputs_json, intent, prompt_version) "
                 "VALUES('solana','M3',?,'h','{}','promotion','p')", (NOW + 50,))          # not known yet
    conn.commit()
    conn.close()
    OW.run_pass(tmp_path, now_fn=lambda: NOW)
    rows = {r["mint"]: r for r in _rows(tmp_path)}
    assert rows["M2"]["crawler_intent"] == "warning" and "M3" not in rows


def test_the_kill_knob_defaults_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("MEMECOIN_DESK_OFFCHAIN_ENABLED", raising=False)
    assert OW.main(["--once"]) == 0 and "is not 1" in capsys.readouterr().out
    assert not (tmp_path / "data" / "desk" / "offchain").exists()


def test_the_watchdog_starts_the_pinned_collector_and_never_signals_a_fake_pid(tmp_path):
    import os
    import shutil
    import subprocess
    import time
    rel = tmp_path / "releases" / "A" / "scripts"
    rel.mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "memecoin_desk_offchain_watchdog.sh", rel)
    shutil.copytree(ROOT / "scripts" / "lib", rel / "lib")
    (rel / "memecoin_desk_offchain.py").write_text("")
    (tmp_path / "current").symlink_to(tmp_path / "releases" / "A")
    fake_py = tmp_path / "python3"
    fake_py.write_text('#!/bin/sh\necho "$1 $2" > "%s"\n' % (tmp_path / "started"))
    fake_py.chmod(0o755)
    child = subprocess.Popen(["sleep", "30"])
    try:
        d = tmp_path / "proc" / str(child.pid)
        d.mkdir(parents=True)
        old = str(tmp_path / "releases" / "OLD" / "scripts" / "memecoin_desk_offchain.py")
        (d / "cmdline").write_bytes(b"/usr/bin/python3\0" + old.encode() + b"\0")
        (d / "environ").write_bytes(b"SNIPER_STATE_ROOT=" + str(tmp_path / "state").encode() + b"\0")
        out = subprocess.run(["bash", str(tmp_path / "current" / "scripts" / "memecoin_desk_offchain_watchdog.sh")],
                             env=dict(os.environ, SNIPER_STATE_ROOT=str(tmp_path / "state"), SNIPER_PYTHON=str(fake_py),
                                      DESK_OFFCHAIN_WATCHDOG_PROC_ROOT=str(tmp_path / "proc")),
                             capture_output=True, text=True, timeout=30)
        for _ in range(50):
            if (tmp_path / "started").exists():
                break
            time.sleep(0.05)
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        assert child.returncode is None
    finally:
        child.kill()
        child.wait()
    pinned = str((tmp_path / "releases" / "A" / "scripts").resolve() / "memecoin_desk_offchain.py")
    assert "stopping superseded desk offchain collector pid=%d" % child.pid in out.stderr
    assert (tmp_path / "started").read_text().split() == [pinned, "--loop"]
