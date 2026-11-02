"""An armed-set resubscribe is a reconnect: counted, logged with its cause, and its gap backfilled (2026-09-29).

A roster change makes the push stream drop the socket and resubscribe with the new address set;
Alpha re-armed DipWheeler on 56 at ~06:45Z. That path broke out silently: no log line and no
reconnect counted. The watcher's gap backfill is keyed on the count, so the blocks between the old
socket's last head and the new socket's first were read by nobody. On 56 the 06:45:11 -> 06:45:22
blip was ~15 blocks.

The test drives the REAL `run_push_stream` with scripted sockets. The roster changes just before a
delivered transfer, the stream resubscribes, and a buy that landed inside the gap (block 1025, on the
backfill stub) must come back through the gap backfill.
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import scripts.research.onchain_evm_wallet_watch as w  # noqa: E402
from sniper.services import memecoin_onchain_push as P  # noqa: E402
from test_onchain_evm_watch_backfill import MINT, TX as GAP_TX, WALLET, _getlogs, env  # noqa: E402,F401

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_log_silence_act(monkeypatch):
    """These sockets stamp logs on a test clock near 1000 s while the watcher's silence verdict reads
    the real wall clock, so every head reads as hours of silence. The log-silence action (2026-10-16,
    default ON on 4663) would resubscribe on each; this file pins the roster path alone."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_WS_LOG_SILENCE_ACT_CHAINS", "", raising=False)


NEW = "0x" + "12" * 20                        # the wallet the roster change arms
LIVE_TX = "0x" + "a1" * 32                    # the delivery that notices the change


def _head(n, ts):
    return json.dumps({"method": "eth_subscription", "params": {
        "subscription": "0xheads", "result": {"number": hex(n), "timestamp": hex(ts)}}})


def _reply(i, sub):
    return json.dumps({"jsonrpc": "2.0", "id": i, "result": sub})


class _Script:
    """A replayed server: iterating yields its messages (no `recv`). A callable in the script runs
    when reached (a side effect between messages), and `on_end` runs after the last message."""

    def __init__(self, msgs, on_end=None):
        self.msgs, self.on_end = msgs, on_end

    def send(self, raw):
        pass

    def __iter__(self):
        for m in self.msgs:
            if callable(m):
                m()
                continue
            yield m
        if self.on_end:
            self.on_end()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _drive(monkeypatch, env, change=None, initial=None, verified=None):
    w._configure("4663")
    monkeypatch.setattr(w, "HEARTBEAT", env["hb"])
    monkeypatch.setattr(w, "_backfill_post", _getlogs(env, [1_025]))
    monkeypatch.setattr(w, "_backfill_thread", lambda target: target())      # run inline
    roster = dict(initial) if initial else {WALLET: ("u1", "h1")}
    monkeypatch.setattr(w, "_verified_addresses", (lambda conn: verified(conn, roster)) if verified
                        else (lambda conn: dict(roster)))
    monkeypatch.setattr(w, "_require_addresses", lambda conn: dict(roster))
    done = {"stop": False, "opened": 0}
    subs = [_reply(1, "0xheads"), _reply(2, "0xtr"), _reply(3, "0xinit")]
    live = json.dumps({"method": "eth_subscription", "params": {"subscription": "0xtr", "result": {
        "transactionHash": LIVE_TX, "address": MINT, "blockNumber": hex(1_019),
        "topics": [P.TRANSFER_TOPIC, "0x" + "00" * 32, P.address_topic(WALLET)]}}})

    def arm_new():                                # the operator's re-arm, just before the delivery
        if change is None:
            roster[NEW] = ("u2", "h2")
        else:
            change(roster)

    def connect_fn(_url, **_k):
        done["opened"] += 1
        if done["opened"] == 1:                   # heads 1000..1019 at 10 blocks/s, then the flip
            return _Script(subs + [_head(1_000 + i, 1_000 + i // 10) for i in range(20)]
                           + [arm_new, live])
        # the new socket's first head is 1030: blocks 1020..1029 were read by nobody
        return _Script(subs + [_head(1_030 + i, 1_003 + i // 10) for i in range(20)],
                       on_end=lambda: done.update(stop=True))

    real = P.run_push_stream

    def stream(ws_url, **kw):
        kw.update(connect_fn=connect_fn, sleep_fn=lambda s: None,
                  should_stop=lambda: done["stop"])
        return real(ws_url, **kw)
    monkeypatch.setattr(P, "run_push_stream", stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    return done


def test_a_buy_inside_the_resubscribe_gap_is_recovered(monkeypatch, env, caplog):
    caplog.set_level(logging.WARNING, logger=P.logger.name)
    done = _drive(monkeypatch, env)
    assert done["opened"] == 2, "the roster change resubscribed once"
    assert json.loads(env["hb"].read_text())["reconnects"] == 1, "a resubscribe is a counted reconnect"
    assert any(lo <= 1_025 <= hi for lo, hi in env["pages"]), env["pages"]
    rows = [p for row in env["inserts"] for p in row if isinstance(p, str) and p.startswith("4663:")]
    assert any(GAP_TX in r for r in rows), "the buy inside the gap came back through the backfill"
    assert any("armed set changed" in r.getMessage() for r in caplog.records), \
        "the resubscribe says why, with the set sizes"


def test_arming_279_wallets_at_once_is_one_resubscribe(monkeypatch, env):
    """This morning's roster change armed 279 wallets: one resubscribe with the whole new set."""
    def arm_many(roster):
        for i in range(279):
            roster["0x%040x" % (0x5000 + i)] = ("u%d" % i, "h%d" % i)
    done = _drive(monkeypatch, env, change=arm_many)
    assert done["opened"] == 2
    assert json.loads(env["hb"].read_text())["reconnects"] == 1


def test_a_revoked_roster_resubscribes_but_backfills_nothing(monkeypatch, env):
    """An empty address list in a getLogs topic can read as a wildcard: never send one."""
    done = _drive(monkeypatch, env, change=lambda roster: roster.clear())
    assert done["opened"] == 2 and env["pages"] == [], env["pages"]
    assert any("no watched address" in line for line in env["lines"]), env["lines"][-5:]


def test_a_wallet_armed_at_t_that_buys_at_t_plus_1s_before_any_delivery_is_detected(monkeypatch, env):
    """Alpha 2026-09-29: the real hole is a newly armed wallet blind until the next delivery. The
    roster changes at block 1020 (t=2.0 s), NEW buys at block 1030 (t=3.0 s), and nothing is ever
    delivered on the old socket. The head-path roster check resubscribes within ~5 s, and the
    lane-window backfill with the NEW set reads block 1030."""
    w._configure("4663")
    monkeypatch.setattr(w, "HEARTBEAT", env["hb"])
    monkeypatch.setattr(w, "_backfill_post", _getlogs(env, [1_030]))
    monkeypatch.setattr(w, "_backfill_thread", lambda target: target())
    roster = {WALLET: ("u1", "h1")}
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: dict(roster))
    monkeypatch.setattr(w, "_require_addresses", lambda conn: dict(roster))
    clock = {"t": 0.0}
    done = {"stop": False, "opened": 0}
    subs = [_reply(1, "0xheads"), _reply(2, "0xtr"), _reply(3, "0xinit")]

    def tick(i, flip=False):
        def step():
            clock["t"] = 0.1 * i
            if flip:
                roster[NEW] = ("u2", "h2")
        return step

    def connect_fn(_url, **_k):
        done["opened"] += 1
        if done["opened"] == 1:                 # 100 heads over 10 s, never a transfer
            script = subs[:]
            for i in range(100):
                script += [tick(i, flip=(i == 20)), _head(1_000 + i, 1_000 + i // 10)]
            return _Script(script, on_end=lambda: done.update(stop=True))
        script = subs[:]
        for i in range(20):
            script += [tick(100 + i), _head(1_100 + i, 1_010 + i // 10)]
        return _Script(script, on_end=lambda: done.update(stop=True))

    real = P.run_push_stream

    def stream(ws_url, **kw):
        kw.update(connect_fn=connect_fn, sleep_fn=lambda s: None, now_fn=lambda: clock["t"],
                  should_stop=lambda: done["stop"])
        return real(ws_url, **kw)
    monkeypatch.setattr(P, "run_push_stream", stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    assert done["opened"] == 2, "a roster change resubscribes without any delivery"
    assert any(lo <= 1_030 <= hi for lo, hi in env["pages"]), env["pages"]
    rows = [p for row in env["inserts"] for p in row if isinstance(p, str) and p.startswith("4663:")]
    assert any(GAP_TX in r for r in rows), "the new wallet's buy at T+1 s is detected"


def test_a_removal_only_resubscribe_reads_the_blip_not_the_lane_window(monkeypatch, env):
    """Removing an address cannot hide a new wallet's buy, so only the blip is read (the cost bound:
    the lane window is up to 45 Alchemy pages on 4663)."""
    other = "0x" + "34" * 20
    done = _drive(monkeypatch, env, change=lambda roster: roster.pop(other),
                  initial={WALLET: ("u1", "h1"), other: ("u3", "h3")})
    assert done["opened"] == 2
    assert env["pages"] and min(lo for lo, _hi in env["pages"]) >= 1_020, env["pages"]


def test_ten_adding_resubscribes_in_50s_read_one_lane_window_plus_blips(monkeypatch, env):
    """Alpha 2026-09-29: the backfill host shares custody's pool key and rate limit, so a flapping
    roster must not re-read the 45 s window every 5 s. Per chain, at most one lane-window backfill
    per 60 s; a resubscribe inside that reads only from the end of the last backfill (the blip),
    and says so."""
    from thread_local_time import patch_local
    w._configure("4663")
    monkeypatch.setattr(w, "HEARTBEAT", env["hb"])
    monkeypatch.setattr(w, "_backfill_post", _getlogs(env, []))
    monkeypatch.setattr(w, "_backfill_thread", lambda target: target())
    roster = {WALLET: ("u1", "h1")}
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: dict(roster))
    monkeypatch.setattr(w, "_require_addresses", lambda conn: dict(roster))
    clock = {"t": 1_000.0}
    patch_local(monkeypatch, "monotonic", lambda: clock["t"])     # the watcher's cooldown clock
    done = {"stop": False, "opened": 0}
    subs = [_reply(1, "0xheads"), _reply(2, "0xtr"), _reply(3, "0xinit")]
    block = {"n": 1_000}

    def connect_fn(_url, **_k):
        done["opened"] += 1
        k = done["opened"]
        script = subs[:]
        block["n"] += 5                            # the blip: 5 blocks nobody delivered
        for _i in range(50):                        # 5 s of heads at 10 blocks/s
            n = block["n"]
            block["n"] += 1

            def step(n=n):
                clock["t"] += 0.1
            script += [step, _head(n, 1_000 + (n - 1_000) // 10)]
        if k <= 10:                                 # an address added, noticed by a delivery
            def arm(k=k):
                roster["0x%040x" % (0x7000 + k)] = ("u%d" % k, "h%d" % k)
            live = json.dumps({"method": "eth_subscription", "params": {"subscription": "0xtr",
                               "result": {"transactionHash": "0x%064x" % k, "address": MINT,
                                          "blockNumber": hex(block["n"] - 1),
                                          "topics": [P.TRANSFER_TOPIC, "0x" + "00" * 32,
                                                     P.address_topic(WALLET)]}}})
            script += [arm, live]
            return _Script(script)
        return _Script(script, on_end=lambda: done.update(stop=True))

    real = P.run_push_stream

    def stream(ws_url, **kw):
        kw.update(connect_fn=connect_fn, sleep_fn=lambda s: None, now_fn=lambda: clock["t"],
                  should_stop=lambda: done["stop"])
        return real(ws_url, **kw)
    monkeypatch.setattr(P, "run_push_stream", stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    assert done["opened"] == 11 and clock["t"] - 1_000.0 <= 60.0
    clipped = [l for l in env["lines"] if "lane-window backfill clipped" in l]
    assert len(clipped) == 9, env["lines"][-12:]
    assert len(env["pages"]) <= 45 + 9 * 2, len(env["pages"])
    assert min(lo for lo, _hi in env["pages"]) < 1_050, "the first resubscribe read the lane window"


def test_a_mixed_add_and_remove_is_an_armed_set_lane_window_read(monkeypatch, env):
    """{A, B} -> {A, C}: an address was ADDED, so the lane window is read (Gamma's surviving mutant)."""
    other = "0x" + "34" * 20
    done = _drive(monkeypatch, env, change=lambda roster: (roster.pop(other), roster.update({NEW: ("u2", "h2")})),
                  initial={WALLET: ("u1", "h1"), other: ("u3", "h3")})
    assert done["opened"] == 2
    assert min(lo for lo, _hi in env["pages"]) < 1_020, "the lane window, not the blip"


def test_the_roster_reads_on_its_own_readonly_connection_with_a_short_busy_timeout(monkeypatch):
    """Gamma L2: the head-path roster read used the writable ledger connection (busy_timeout 60 s)
    on the socket thread. It now has its own read-only connection that waits 250 ms at most."""
    executed = []

    class _Ro:
        def execute(self, sql, *a):
            executed.append(sql)
            return self
    monkeypatch.setattr(w, "get_readonly_connection", lambda path: _Ro())
    conn = w._roster_connection()
    assert isinstance(conn, _Ro) and "PRAGMA busy_timeout=250" in executed


def test_a_locked_roster_read_keeps_the_old_set_and_says_so(monkeypatch, env):
    """A read that times out on the busy ledger keeps the subscribed set and logs it; it never
    resubscribes, and every roster read uses the roster's own connection, never the writer's."""
    import sqlite3 as _sqlite3
    class _Ro:
        closed = False

        def close(self):
            self.closed = True
    ro = _Ro()
    monkeypatch.setattr(w, "_roster_connection", lambda: ro)
    seen = []

    def verified(conn, roster):
        seen.append(conn)
        if len(seen) > 1:
            raise _sqlite3.OperationalError("database is locked")
        return dict(roster)
    _drive(monkeypatch, env, verified=verified)
    assert len(seen) >= 2 and all(c is ro for c in seen), seen
    assert json.loads(env["hb"].read_text())["reconnects"] == 0, "a failed read never resubscribes"
    assert any("address read failed" in line and "keeping" in line for line in env["lines"])
    assert ro.closed, "the roster handle is closed when the watcher stops"
