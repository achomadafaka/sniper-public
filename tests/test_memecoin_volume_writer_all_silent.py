"""Both swap subscriptions silent while heads arrive (operator yes 2026-10-15 via Alpha).

4663, 2026-10-15: V4 (sub 3) and V3 (sub 4) stopped together from about 05:55:11 to 06:11:48Z while
newHeads kept arriving. The watch re-opens a silent subscription only beside MIN_OTHER_FRAMES (20)
log frames of OTHER subscriptions, and with both swap topics quiet only sparse Pons frames counted,
so it fired after 997 s ("silent 997s beside 20 other log frames"). That day sub 3 sat silent
26,055 s by about 09:40Z, and the fast lane's tape missed every source buy in the gap.

Now: when every required swap subscription has been silent for the threshold and at least
MIN_HEAD_FRAMES heads arrived meanwhile, all of them are re-opened. The threshold starts at
silence_s and doubles after each such re-open with no swap frame since (cap 8x), so a genuinely
quiet chain costs one re-open per 2-16 min, never a storm. One silent subscription keeps the old
rule; the head stall keeps its own 15 s rule."""
import pytest

from sniper.services import memecoin_volume_writer as w
from test_memecoin_volume_writer_v2 import CHAIN, _confirms, _harness, _head, _v4_swap


def _watch(silence=120.0, t0=1000.0):
    watch = w.SubscriptionWatch(silence)
    for rid in (2, 3, 4, 5, 6):
        watch.confirm(rid, t0)
    return watch


def _heads(watch, start, end, every=1.0):
    t = start
    while t <= end:
        watch.head(t)
        t += every


def test_both_swap_subscriptions_silent_with_heads_arriving_are_reopened_at_the_threshold():
    watch = _watch()
    _heads(watch, 1001.0, 1119.0)
    assert watch.silent(1119.0) == [], "not yet silent for 120 s"
    _heads(watch, 1120.0, 1120.0)
    due = watch.silent(1120.0)
    assert sorted(rid for rid, _q, _n in due) == [3, 4]
    assert watch.silent(1121.0) == [], "re-opened: the clock starts again"


def test_without_heads_an_idle_socket_still_proves_nothing():
    watch = _watch()
    assert watch.silent(5000.0) == []
    _heads(watch, 1001.0, 1019.0)                       # 19 heads: one short
    assert watch.silent(5000.0) == []


def test_one_silent_subscription_keeps_todays_rule():
    watch = _watch()
    _heads(watch, 1001.0, 1200.0)
    for i in range(10):
        watch.frame(4, 1100.0 + i)                      # V3 speaks (10 frames), V4 does not
    assert watch.silent(1200.0) == [], "V3 is alive: only the 20-other-frames rule may re-open V4"
    for i in range(10, 25):
        watch.frame(4, 1100.0 + i)
    assert [rid for rid, _q, _n in watch.silent(1200.0)] == [3]


def test_a_quiet_chain_backs_off_instead_of_storming():
    watch = _watch()
    t, opens = 1000.0, []
    while t < 1000.0 + 7200:                            # two hours: the cap must hold, not just start
        t += 1.0
        watch.head(t)
        if watch.silent(t):
            opens.append(t)
    gaps = [b - a for a, b in zip([1000.0] + opens, opens)]
    # Gamma LOW on 51de2e0a9: pin the 960 s cap (uncapped, the 5th gap is 1920 s).
    assert gaps == [120.0, 240.0, 480.0, 960.0] + [960.0] * 5, gaps


def test_a_swap_frame_resets_the_backoff():
    watch = _watch()
    _heads(watch, 1001.0, 1120.0)
    assert watch.silent(1120.0)
    _heads(watch, 1121.0, 1360.0)
    assert watch.silent(1360.0), "second re-open after 240 s"
    watch.frame(3, 1361.0)
    watch.frame(4, 1361.0)                              # the re-open worked: swaps flow again
    _heads(watch, 1362.0, 1481.0)
    assert watch.silent(1481.0), "silent again: back to the 120 s threshold"


def test_zero_still_disables():
    watch = w.SubscriptionWatch(0.0)
    watch.confirm(3, 1000.0)
    watch.confirm(4, 1000.0)
    _heads(watch, 1001.0, 3000.0)
    assert watch.silent(3000.0) == []


def test_the_writer_reopens_both_swap_subscriptions_on_heads_alone(tmp_path, monkeypatch, capsys):
    """The 05:55-06:11Z shape at socket level: heads every second, V3 and V4 silent, one Pons
    frame a minute (the sparse 'other' frames). Today's rule waits for 20 Pons frames (about 20
    min); the head rule re-opens both at 120 s, on the same socket."""
    import sqlite3
    monkeypatch.setenv("MEMECOIN_ONCHAIN_V2_SWAP_CHAINS", "")
    hello = [{"id": 1, "result": hex(int(CHAIN))}]

    def pons(i, t):
        return (t, {"params": {"subscription": "sub5", "result": {
            "address": "0x" + "22" * 20, "topics": [w.PONS_V2_CURVE_BUY], "data": "0x",
            "blockNumber": hex(i), "logIndex": "0x0", "transactionHash": f"0xp{i}",
            "blockHash": hex(i)}}})

    def reopened(rid, new_sub):
        def answer(sock):
            req = [m for m in sock.sent if m.get("method") == "eth_subscribe" and m["id"] >= 100
                   and m["params"] == dict(w.subscriptions(CHAIN))[rid]]
            assert req, sock.sent
            return {"id": req[-1]["id"], "result": new_sub}
        return answer
    frames = []
    for i in range(1, 122):
        t = 1000.0 + i
        frames.append((t, _head(i, int(t))))
        if i % 60 == 0:
            frames.append(pons(i, t))
    msgs = (hello + [(1000.0, m) for m in _confirms(range(2, 7))] + frames
            + [(1121.0, reopened(3, "sub3b")), (1121.0, reopened(4, "sub4b")),
               (1122.0, _head(122, 1122)),
               (1122.0, {"params": {"subscription": "sub3b", "result": _v4_swap(122, "0xafter")}})])
    connect_fn, sockets = _harness(monkeypatch, [msgs])
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN, "wss://unit-test", path=tmp_path / "v.db", connect_fn=connect_fn)
    out = capsys.readouterr().out
    assert len(sockets) == 1, "no reconnect: the window is kept"
    assert "volume subscription 3 silent" in out and "volume subscription 4 silent" in out, out
    assert "all swap subscriptions silent" in out, out
    with sqlite3.connect(tmp_path / "v.db") as c:
        assert c.execute("SELECT COUNT(*) FROM pool_ticks WHERE tx_sig='0xafter'").fetchone()[0] == 1
