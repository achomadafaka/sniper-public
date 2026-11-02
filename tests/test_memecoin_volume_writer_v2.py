"""8453 volume tape: V2-family swaps, a refused optional subscription, and the header crash.

Spec docs/superpowers/specs/2026-09-28-memecoin-8453-tape-v2-swaps-design.md. The swap and header
frames are REAL: tests/fixtures/memecoin/base_v2_swap_frames.json, captured from the tape's own host
(base-rpc.publicnode.com, eth_subscribe) on 2026-09-28. Expected amounts are read from the frames'
data words here, independently of the decoder."""
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_onchain_volume as ov
from sniper.services import memecoin_volume_writer as w
from sniper.services.memecoin_tick_stream import decode_evm_log

FRAMES = json.load(open(Path(__file__).parent / "fixtures" / "memecoin" / "base_v2_swap_frames.json"))
CHAIN = "8453"


def _logs(family):
    return [frame["params"]["result"] for frame in FRAMES[family]]


def _words(log):
    data = log["data"][2:]
    return [int(data[i:i + 64], 16) for i in range(0, len(data), 64)]


@pytest.mark.parametrize("family", ["univ2", "aerodrome"])
def test_both_v2_families_decode_to_in_minus_out(family):
    for log in _logs(family):
        row = decode_evm_log(CHAIN, log, "2026-09-28T21:40:00+00:00")
        assert row is not None and row["kind"] == "v2_swap", (family, log["topics"][0])
        in0, in1, out0, out1 = _words(log)[:4]
        assert (int(row["amount0"]), int(row["amount1"])) == (in0 - out0, in1 - out1)
        assert row["pool"] == log["address"].lower()


@pytest.fixture
def tape(tmp_path):
    c = w.connect(tmp_path / "tape.db")
    t = w.VolumeTape(c, CHAIN)
    yield t
    c.close()


def test_log_v2_stores_the_swap_and_a_removed_log_deletes_it(tape):
    log = _logs("univ2")[0]
    tape.log_v2(log, 1000)
    rows = tape.conn.execute("SELECT kind, pool, tx_sig FROM pool_ticks WHERE chain_id=?", (CHAIN,)).fetchall()
    assert [tuple(r) for r in rows] == [("v2_swap", log["address"].lower(), log["transactionHash"])]
    resets = tape.reset_count
    tape.log_v2(dict(log, removed=True), 1001)
    assert tape.conn.execute("SELECT COUNT(*) FROM pool_ticks").fetchone()[0] == 0
    assert tape.reset_count == resets + 1


def test_the_knob_off_keeps_todays_subscriptions(monkeypatch):
    monkeypatch.delenv("MEMECOIN_ONCHAIN_V2_SWAP_CHAINS", raising=False)
    today = w.subscriptions(CHAIN)
    assert [rid for rid, _p in today] == [2, 3, 4, 5, 6]
    monkeypatch.setenv("MEMECOIN_ONCHAIN_V2_SWAP_CHAINS", "base")
    on = w.subscriptions(CHAIN)
    assert on[:5] == today and [rid for rid, _p in on[5:]] == [7, 8]
    assert {p[1]["topics"][0][0] for _rid, p in on[5:]} == {log["topics"][0] for f in ("univ2", "aerodrome")
                                                           for log in _logs(f)}
    assert [rid for rid, _p in w.subscriptions("4663")] == [2, 3, 4, 5, 6]


def test_a_refused_optional_subscription_never_kills_the_socket():
    assert w.fatal_subscription_error({"jsonrpc": "2.0", "id": 7, "error": {"code": -32602}}) is False
    assert w.fatal_subscription_error({"jsonrpc": "2.0", "id": 8, "error": {"code": -32602}}) is False
    assert w.fatal_subscription_error({"jsonrpc": "2.0", "id": 4, "error": {"code": -32602}}) is True
    assert w.fatal_subscription_error({"jsonrpc": "2.0", "error": {"code": -32000}}) is True
    assert w.fatal_subscription_error({"jsonrpc": "2.0", "id": 4, "result": "0xabc"}) is False


def test_a_header_frame_without_a_number_is_skipped_not_a_disconnect(tape):
    head = FRAMES["head"][0]["params"]["result"]
    assert tape.head(head, 1000) is True
    since, resets = tape.since, tape.reset_count
    assert tape.head({k: v for k, v in head.items() if k != "number"}, 1001) is False
    assert tape.head({}, 1002) is False
    assert tape.head(head, 1002) is True, "a duplicate is still a parsed header"
    assert (tape.since, tape.reset_count) == (since, resets)
    nxt = dict(head, number=hex(int(head["number"], 16) + 40), hash="0x" + "cd" * 32)
    for step in range(12):                      # a real jump still resets through the gap logic
        tape.head(dict(nxt, number=hex(int(nxt["number"], 16) + step), hash="0x%064x" % step), 1003 + step)
    assert tape.reset_count > resets


def test_the_resolver_registers_a_v2_pair_without_asking_for_fee(tape):
    log = _logs("aerodrome")[0]
    tape.log_v2(log, 1000)
    pool = log["address"].lower()
    t0, t1 = "0x" + "11" * 20, "0x" + "22" * 20
    asked = []

    def rpc_call(calls):
        asked.extend(calls)
        out = []
        for _method, (call, _tag) in calls:
            sel = call["data"]
            out.append({w.V3_SEL_TOKEN0: "0x" + "00" * 12 + t0[2:],
                        w.V3_SEL_TOKEN1: "0x" + "00" * 12 + t1[2:]}.get(sel, w.RpcErrorReply({"code": 3, "message": "execution reverted"})))
        return out
    assert w.resolve_v3_pools(tape.conn, CHAIN, rpc_call, 1100) == 1
    assert all(call[1][0]["data"] != w.V3_SEL_FEE for call in asked)
    protos = {tuple(r) for r in tape.conn.execute(
        "SELECT pool, protocol, mint FROM onchain_pool_protocols WHERE chain_id=?", (CHAIN,))}
    assert protos == {(pool, "v2", t0), (pool, "v2", t1)}


def _reader_conn(chain):
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE pool_ticks (id INTEGER PRIMARY KEY AUTOINCREMENT, chain_id TEXT, pool TEXT,
            kind TEXT, block_no INTEGER, tx_sig TEXT, log_index INTEGER, maker TEXT,
            amount0 TEXT, amount1 TEXT, observed_at TEXT, block_hash TEXT);
        CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT,
            quote_side INTEGER, resolved_at TEXT, token0_decimals INTEGER, token1_decimals INTEGER);
        CREATE TABLE block_times (chain_id TEXT, block_no INTEGER, block_ts INTEGER,
            fetched_at TEXT, block_hash TEXT, PRIMARY KEY (chain_id, block_no));
        CREATE TABLE onchain_pool_protocols (chain_id TEXT, pool TEXT, protocol TEXT, mint TEXT);
    """)
    pool, mint, quote = "0x" + "aa" * 20, "0x" + "bb" * 20, "0x" + "cc" * 20
    c.execute("INSERT INTO onchain_pool_protocols VALUES (?,?,?,?)", (chain, pool, "v2", mint))
    c.execute("INSERT INTO pool_meta VALUES (?,?,?,?,?,?,?,?)", (chain, pool, quote, mint, 0, "x", 18, 18))
    c.execute("INSERT INTO block_times VALUES (?,?,?,?,?)", (chain, 100, 1000 - 10, "", "0xh"))
    c.execute("INSERT INTO pool_ticks (chain_id,pool,kind,block_no,tx_sig,log_index,amount0,amount1,"
              "observed_at,block_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (chain, pool, "v2_swap", 100, "0xt", 0, str(250 * 10 ** 18), str(-10 ** 18), "", "0xh"))
    return c, mint


def test_the_reader_counts_v2_only_on_a_chain_that_subscribes_it(monkeypatch):
    """A 'v2' pool on a chain without the subscription is UNCOVERED (today's partial read); on a
    knob chain it counts only once the writer confirmed both V2 subscriptions on its socket (T3):
    a refused subscription must never read as a measured zero."""
    monkeypatch.setenv("MEMECOIN_ONCHAIN_V2_SWAP_CHAINS", "8453")
    c, mint = _reader_conn("8453")
    unconfirmed = ov.onchain_volume_m5(c, "8453", mint, 1000, price_fn=lambda _c, _t: 1.0)
    assert unconfirmed["uncovered_protocols"] == ["v2"]
    c.execute("CREATE TABLE volume_feed_optional (chain_id TEXT PRIMARY KEY, v2_confirmed_at REAL)")
    c.execute("INSERT INTO volume_feed_optional (chain_id, v2_confirmed_at) VALUES ('8453', 990)")
    out = ov.onchain_volume_m5(c, "8453", mint, 1000, price_fn=lambda _c, _t: 1.0)
    assert out["usd"] == pytest.approx(250.0) and out["uncovered_protocols"] == []
    c4, mint4 = _reader_conn("4663")
    out4 = ov.onchain_volume_m5(c4, "4663", mint4, 1000, price_fn=lambda _c, _t: 1.0)
    assert out4["uncovered_protocols"] == ["v2"]


def test_the_writer_records_a_confirmed_v2_subscription_and_a_reset_clears_it(tape):
    tape.confirm_optional({7, 8}, 1000)
    row = tape.conn.execute("SELECT v2_confirmed_at FROM volume_feed_optional WHERE chain_id=?", (CHAIN,)).fetchone()
    assert row is not None and row[0] == 1000
    tape.reset(1001, "connect")
    assert tape.conn.execute("SELECT COUNT(*) FROM volume_feed_optional").fetchone()[0] == 0
    tape.confirm_optional({7}, 1002)                          # one family is not the V2 tape
    assert tape.conn.execute("SELECT COUNT(*) FROM volume_feed_optional").fetchone()[0] == 0


def test_a_stream_of_header_less_frames_lets_the_silence_timer_fire(tmp_path, monkeypatch, capsys):
    """Beta T2: a skipped frame must not refresh head_received, or frames that carry no height would
    keep a dead head stream looking alive."""
    import itertools
    from sniper.execution import evm_wallet
    from sniper.util import clock
    from sniper.settings import memecoin as _cfg
    ticks = itertools.count(1000, 5)
    monkeypatch.setattr(clock, "real_utc_now", lambda: float(next(ticks)))
    monkeypatch.setattr(evm_wallet, "quote_usd_price", lambda *a: 2400)
    monkeypatch.setattr(_cfg, "MEMECOIN_ONCHAIN_M5_BACKFILL_ENABLED", False)
    monkeypatch.setattr(w.time, "sleep", lambda _s: None)
    messages = [{"id": 1, "result": hex(4663)}, {"id": 10, "result": "0x" + f"{6:064x}"},
                {"id": 2, "result": "heads"}, {"id": 3, "result": "logs"}]
    messages += [{"params": {"subscription": "heads", "result": {"hash": "0x1"}}} for _ in range(8)]

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def send(self, *a):
            pass

        def ping(self):
            pass

        def recv(self, **k):
            if not messages:
                raise KeyboardInterrupt
            return json.dumps(messages.pop(0))
    with pytest.raises(KeyboardInterrupt):
        w.run("4663", "wss://unit-test", path=tmp_path / "v.db", connect_fn=lambda *a, **k: Socket())
    assert "head stream stalled" in capsys.readouterr().out


# --- every subscription on every socket, and a silent one re-opened (2026-09-29, 8453 V4) ----------
# After the 01:02:00Z reconnect the 8453 V4 subscription (id 3) delivered nothing for an hour while
# V3 on the same socket carried 1,100-3,400 swaps a minute. Nothing logged whether id 3 was confirmed.

def _v4_swap(block, tx, index=0):
    words = [10 ** 18, (1 << 256) - 10 ** 18, 1, 1, 0, 3000]
    return {"address": w.MANAGERS[CHAIN], "topics": [w.V4_SWAP, "0x" + "cd" * 32, "0x" + "00" * 32],
            "data": "0x" + "".join(f"{v:064x}" for v in words), "blockNumber": hex(block),
            "logIndex": hex(index), "transactionHash": tx, "blockHash": hex(block)}


def _harness(monkeypatch, connections):
    """run() over scripted sockets. A scripted frame is an item, or (t, item) to move the clock to t
    when it is received. Each socket records what was sent to it."""
    from sniper.settings import memecoin as cfg
    from sniper.util import clock
    from sniper.execution import evm_v4_quote, evm_wallet
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_BACKFILL_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S", 120.0, raising=False)
    monkeypatch.setattr(evm_v4_quote, "_stored_pool_key", lambda *a: None)
    monkeypatch.setattr(evm_wallet, "quote_usd_price", lambda *a: 2400)
    monkeypatch.setattr(w, "_start_v3_resolver", lambda *a, **k: None)
    monkeypatch.setattr(w, "_start_hop_refresher", lambda *a, **k: None)
    monkeypatch.setattr(w, "_read_stable_decimals", lambda *a, **k: False)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    now = [1000.0]
    monkeypatch.setattr(clock, "real_utc_now", lambda: now[0])
    sockets = []

    class Socket:
        def __init__(self, msgs):
            self.msgs, self.sent = msgs, []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def send(self, body):
            self.sent.append(json.loads(body))

        def ping(self):
            pass

        def recv(self, **k):
            if not self.msgs:
                raise ConnectionError("closed")
            item = self.msgs.pop(0)
            if isinstance(item, tuple):
                now[0], item = item
            return json.dumps(item(self) if callable(item) else item)

    def connect_fn(*a, **k):
        if not connections:
            raise KeyboardInterrupt
        sockets.append(Socket(connections.pop(0)))
        return sockets[-1]
    return connect_fn, sockets


def _confirms(ids):
    return [{"id": rid, "result": f"sub{rid}"} for rid in ids]


def _head(n, ts):
    return {"params": {"subscription": "sub2", "result": {"number": hex(n), "timestamp": hex(ts),
                                                          "hash": hex(n)}}}


def test_a_reconnect_resends_every_subscription(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MEMECOIN_ONCHAIN_V2_SWAP_CHAINS", CHAIN)
    hello = [{"id": 1, "result": hex(int(CHAIN))}]
    ids = [rid for rid, _params in w.subscriptions(CHAIN)]
    assert ids == [2, 3, 4, 5, 6, 7, 8]
    connect_fn, sockets = _harness(monkeypatch, [hello + _confirms(ids) + [_head(1, 1000)],
                                                 hello + _confirms(ids) + [_head(2, 1001)]])
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN, "wss://unit-test", path=tmp_path / "v.db", connect_fn=connect_fn)
    assert len(sockets) == 2
    for sock in sockets:
        subs = [(m["id"], m["params"]) for m in sock.sent if m.get("method") == "eth_subscribe"]
        assert subs == w.subscriptions(CHAIN), "every socket carries the whole list"
    out = capsys.readouterr().out
    for rid in ids:
        assert out.count(f"volume subscription {rid} confirmed") == 2, out


def test_a_refused_optional_subscription_leaves_v4_routing_intact(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("MEMECOIN_ONCHAIN_V2_SWAP_CHAINS", CHAIN)
    hello = [{"id": 1, "result": hex(int(CHAIN))}]
    msgs = (hello + _confirms([2, 3, 4, 5, 6]) + [{"id": 7, "error": {"code": -32602}}]
            + _confirms([8]) + [_head(1, 1000),
                                {"params": {"subscription": "sub3", "result": _v4_swap(1, "0xv4")}}])
    connect_fn, _sockets = _harness(monkeypatch, [msgs])
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN, "wss://unit-test", path=tmp_path / "v.db", connect_fn=connect_fn)
    with sqlite3.connect(tmp_path / "v.db") as c:
        assert c.execute("SELECT COUNT(*) FROM pool_ticks WHERE tx_sig='0xv4'").fetchone()[0] == 1


def test_the_watch_flags_a_silent_required_subscription_only_beside_live_ones():
    watch = w.SubscriptionWatch(120.0)
    for rid in (2, 3, 4, 5):
        watch.confirm(rid, 1000.0)
    assert watch.silent(1500.0) == [], "an idle socket proves nothing"
    for i in range(25):
        watch.frame(4, 1000.0 + i)          # V3 speaks, V4 does not
    assert watch.silent(1100.0) == [], "not yet silent for 120 s"
    due = watch.silent(1121.0)
    assert [(rid, round(quiet), n) for rid, quiet, n in due] == [(3, 121, 25)]
    assert watch.silent(1122.0) == [], "re-opened: the clock starts again"
    assert w.SubscriptionWatch(0.0).silent(9999.0) == [], "0 disables"


def test_a_silent_v4_subscription_is_reopened_on_the_same_socket(tmp_path, monkeypatch, capsys):
    import sqlite3
    monkeypatch.setenv("MEMECOIN_ONCHAIN_V2_SWAP_CHAINS", CHAIN)
    hello = [{"id": 1, "result": hex(int(CHAIN))}]

    def v3(i, t):
        return (t, {"params": {"subscription": "sub4", "result": {
            "address": "0x" + "11" * 20, "topics": [w.V3_SWAP], "data": "0x", "blockNumber": hex(i),
            "logIndex": "0x0", "transactionHash": f"0xv3{i}", "blockHash": hex(i)}}})

    def reopened(sock):
        req = [m for m in sock.sent if m.get("method") == "eth_subscribe" and m["id"] >= 100]
        assert req and req[-1]["params"] == dict(w.subscriptions(CHAIN))[3], sock.sent
        return {"id": req[-1]["id"], "result": "sub3b"}
    # Heads every 5 s keep the socket alive; V3 speaks 25 times; V4 never does, until re-opened.
    frames = []
    for i in range(1, 27):
        t = 1000.0 + 5 * i
        frames += [(t, _head(i, int(t)))] + ([v3(i, t)] if i <= 25 else [])
    msgs = (hello + [(1000.0, m) for m in _confirms(range(2, 9))] + frames
            + [(1131.0, reopened), (1131.0, _head(27, 1131)),
               (1132.0, {"params": {"subscription": "sub3b", "result": _v4_swap(27, "0xafter")}})])
    connect_fn, sockets = _harness(monkeypatch, [msgs])
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN, "wss://unit-test", path=tmp_path / "v.db", connect_fn=connect_fn)
    assert len(sockets) == 1, "no reconnect: the window is kept"
    out = capsys.readouterr().out
    assert "volume subscription 3 silent" in out and "re-subscribing" in out, out
    assert "volume subscription 3 re-confirmed" in out, out
    assert "volume subscription 4 silent" not in out, out
    with sqlite3.connect(tmp_path / "v.db") as c:
        assert c.execute("SELECT COUNT(*) FROM pool_ticks WHERE tx_sig='0xafter'").fetchone()[0] == 1


def test_an_error_reply_to_the_cleanup_unsubscribe_neither_closes_nor_resets(tmp_path, monkeypatch, capsys):
    """Gamma LOW 1: the re-open unsubscribes the silent id first; whatever the provider answers to
    that (here an error) must not be read as a refused subscription."""
    import sqlite3
    monkeypatch.setenv("MEMECOIN_ONCHAIN_V2_SWAP_CHAINS", CHAIN)
    hello = [{"id": 1, "result": hex(int(CHAIN))}]

    def v3(i, t):
        return (t, {"params": {"subscription": "sub4", "result": {
            "address": "0x" + "11" * 20, "topics": [w.V3_SWAP], "data": "0x", "blockNumber": hex(i),
            "logIndex": "0x0", "transactionHash": f"0xv3{i}", "blockHash": hex(i)}}})

    def unsub_error(sock):
        req = [m for m in sock.sent if m.get("method") == "eth_unsubscribe"]
        assert req, sock.sent
        return {"id": req[-1]["id"], "error": {"code": -32000, "message": "subscription not found"}}

    def reopened(sock):
        req = [m for m in sock.sent if m.get("method") == "eth_subscribe" and m["id"] >= 100]
        return {"id": req[-1]["id"], "result": "sub3b"}
    frames = []
    for i in range(1, 27):
        t = 1000.0 + 5 * i
        frames += [(t, _head(i, int(t)))] + ([v3(i, t)] if i <= 25 else [])
    msgs = (hello + [(1000.0, m) for m in _confirms(range(2, 9))] + frames
            + [(1131.0, unsub_error), (1131.0, reopened), (1131.0, _head(27, 1131)),
               (1132.0, {"params": {"subscription": "sub3b", "result": _v4_swap(27, "0xafter")}})])
    connect_fn, sockets = _harness(monkeypatch, [msgs])
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN, "wss://unit-test", path=tmp_path / "v.db", connect_fn=connect_fn)
    out = capsys.readouterr().out
    # The script's own end closes the socket; before that, the error reply must not have.
    assert len(sockets) == 1 and "provider rejected subscription" not in out, out
    with sqlite3.connect(tmp_path / "v.db") as c:
        assert c.execute("SELECT COUNT(*) FROM pool_ticks WHERE tx_sig='0xafter'").fetchone()[0] == 1


def test_a_required_subscription_that_is_never_answered_is_reopened(tmp_path, monkeypatch, capsys):
    """Gamma LOW 2: the watch's clock starts when the subscribe is SENT, so a V4 subscribe that gets
    no reply at all is re-opened like one that went quiet."""
    monkeypatch.setenv("MEMECOIN_ONCHAIN_V2_SWAP_CHAINS", CHAIN)
    hello = [{"id": 1, "result": hex(int(CHAIN))}]

    def v3(i, t):
        return (t, {"params": {"subscription": "sub4", "result": {
            "address": "0x" + "11" * 20, "topics": [w.V3_SWAP], "data": "0x", "blockNumber": hex(i),
            "logIndex": "0x0", "transactionHash": f"0xv3{i}", "blockHash": hex(i)}}})
    frames = []
    for i in range(1, 27):
        t = 1000.0 + 5 * i
        frames += [(t, _head(i, int(t)))] + ([v3(i, t)] if i <= 25 else [])
    confirms = [(1000.0, m) for m in _confirms([2, 4, 5, 6, 7, 8])]          # no reply for id 3
    connect_fn, sockets = _harness(monkeypatch, [hello + confirms + frames])
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN, "wss://unit-test", path=tmp_path / "v.db", connect_fn=connect_fn)
    sent = [m for m in sockets[0].sent if m.get("method") == "eth_subscribe" and m["id"] >= 100]
    assert sent and sent[0]["params"] == dict(w.subscriptions(CHAIN))[3], sockets[0].sent
    assert "volume subscription 3 silent" in capsys.readouterr().out
