"""A watcher restart reads its whole gap, says what it anchored on, and never tears its heartbeat
(2026-10-07, Alpha P1(c)).

8453, 17:16:11Z: the watchdog stopped the watcher ("from a previous release") and relaunched it at
17:16:12Z. Roveryaps' HOTDOG buys at blocks 52302011 (17:16:09) and 52302013 (17:16:13) never
reached onchain_wallet_detections, and the new process printed no backfill line, where every other
restart that day printed one within 4 s. Its start gap was skipped: either the previous heartbeat
was unreadable (a kill mid write_text leaves torn JSON, read as no anchor, silently) or the new head
was one block past the anchor (the start check backfilled only `cur > prev + 1`, and never re-read
the anchor block, whose logs the dying process may not have finished).

Now the heartbeat is written whole (tmp + os.replace), the start names its anchor or why there is
none, and the start backfill always covers [prev, cur] inclusive. Re-reads are idempotent (INSERT OR
IGNORE on the signature; backfill rows skip the BSC lane signal).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import scripts.research.onchain_evm_wallet_watch as w  # noqa: E402
from test_onchain_evm_watch_backfill import _heads, _run, env  # noqa: E402,F401


def test_a_restart_one_block_on_still_rereads_the_anchor_block(monkeypatch, env):
    """THE regression shape: the old process saw head 1029 and died; the new first head is 1030.
    Today nothing is read, and a buy in 1029 (or 1030 before the subscription) is lost."""
    _run(monkeypatch, env, [_heads(1_030, 30)], previous_block=1_029, hits=[1_029])
    assert any(lo <= 1_029 <= hi for lo, hi in env["pages"]), env["pages"]
    assert len(env["inserts"]) == 1, "the anchor block's buy came back"


def test_the_anchor_block_itself_is_read_on_a_wider_gap(monkeypatch, env):
    _run(monkeypatch, env, [_heads(1_030, 30)], previous_block=1_000, hits=[1_000])
    assert any(lo <= 1_000 <= hi for lo, hi in env["pages"]), env["pages"]
    assert len(env["inserts"]) == 1


def test_the_start_names_its_anchor(monkeypatch, env):
    _run(monkeypatch, env, [_heads(1_030, 30)], previous_block=1_000)
    assert any("restart anchor last_block=1000" in line for line in env["lines"]), env["lines"]


@pytest.mark.parametrize("content, why", [
    (None, "heartbeat missing"),
    ('{"ts": "2026-10-07T17:16:1', "heartbeat unreadable"),          # a kill mid-write
    ('{"ts": "2026-10-07T17:16:11Z"}', "no last_block"),
])
def test_no_anchor_says_why(monkeypatch, env, content, why):
    if content is not None:
        env["hb"].write_text(content)
    _run(monkeypatch, env, [_heads(1_030, 30)])
    line = [l for l in env["lines"] if "restart anchor" in l]
    assert line and "none" in line[0] and why in line[0], env["lines"]


def test_a_kill_mid_heartbeat_write_leaves_the_last_whole_beat(monkeypatch, tmp_path):
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(w, "HEARTBEAT", hb)
    w._write_beat({"ts": "2026-10-07T17:16:09Z", "last_block": 52302011})

    def killed(src, dst):
        raise KeyboardInterrupt("SIGTERM between the write and the rename")
    monkeypatch.setattr(w.os, "replace", killed)
    with pytest.raises(KeyboardInterrupt):
        w._write_beat({"ts": "2026-10-07T17:16:11Z", "last_block": 52302012})
    assert json.loads(hb.read_text())["last_block"] == 52302011, "never a torn heartbeat"


def test_the_ack_reset_rewrites_the_heartbeat_whole():
    import inspect
    src = inspect.getsource(w.run_push)
    assert "HEARTBEAT.write_text(" not in src, "every heartbeat write goes through the atomic writer"


def test_the_start_sweeps_a_dead_writers_temp_but_keeps_a_live_ones(monkeypatch, tmp_path):
    """Gamma LOW: a kill between the temp write and the rename leaves heartbeat.<pid>.tmp behind."""
    import os
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(w, "HEARTBEAT", hb)
    dead = tmp_path / ("hb.json.%d.tmp" % 2_000_000_000)
    live = tmp_path / ("hb.json.%d.tmp" % os.getpid())
    other = tmp_path / "other.json.1.tmp"
    for f in (dead, live, other):
        f.write_text("{")
    w._restart_anchor()
    assert not dead.exists() and live.exists() and other.exists()


def test_a_temp_that_cannot_be_removed_never_stops_the_start(monkeypatch, tmp_path):
    """Gamma nit: an unlink error (permission, a vanished file) must not fail the watcher start."""
    from pathlib import Path
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(w, "HEARTBEAT", hb)
    hb.write_text('{"last_block": 1000}')
    (tmp_path / ("hb.json.%d.tmp" % 2_000_000_000)).write_text("{")

    def refuse(self, missing_ok=False):
        raise PermissionError("read-only data dir")
    monkeypatch.setattr(Path, "unlink", refuse)
    assert w._restart_anchor() == (1000, None)
