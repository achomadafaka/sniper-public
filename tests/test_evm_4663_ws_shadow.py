import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research"))
import evm_4663_ws_shadow as sh  # noqa: E402

TRADER = "0x" + "ab" * 20
PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeWS:
    """Acks every eth_subscribe (or errors it), then plays `script`, then stops the shadow."""

    def __init__(self, shadow, clock, script, error=None):
        self.shadow, self.clock, self.script, self.error = shadow, clock, list(script), error
        self.out, self.subs = [], []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shadow.stop.set()            # one session per drive: no reconnect wait
        return False

    def send(self, raw):
        msg = json.loads(raw)
        if self.error is not None:
            self.out.append({"jsonrpc": "2.0", "id": msg["id"], "error": self.error})
        else:
            self.subs.append("sub-%d" % msg["id"])
            self.out.append({"jsonrpc": "2.0", "id": msg["id"], "result": "sub-%d" % msg["id"]})

    def recv(self, timeout=None):
        if self.out:
            return json.dumps(self.out.pop(0))
        if self.script:
            dt, sub_n, log = self.script.pop(0)
            self.clock.t += dt
            return json.dumps({"jsonrpc": "2.0", "method": "eth_subscription",
                               "params": {"subscription": "sub-%d" % sub_n, "result": log}})
        self.shadow.stop.set()
        raise TimeoutError


def log(tx, idx, block=10, recipient=TRADER):
    return {"transactionHash": tx, "logIndex": hex(idx), "blockNumber": hex(block), "address": "0xc0in",
            "topics": [sh.filters((TRADER,), PM, INIT)["transfers"][1]["topics"][0],
                       "0x" + "00" * 32, "0x" + recipient[2:].rjust(64, "0")], "removed": False}


class Pacer:
    def __init__(self):
        self.acquired, self.paused = 0, []

    def acquire(self):
        self.acquired += 1

    def pause(self, s):
        self.paused.append(s)


def drive(scripts, error=None, j7=()):
    clock, pacer = Clock(), Pacer()
    roster = sh.Roster(lambda: [TRADER])
    shadow = sh.Shadow(db_path=":memory:", sockets={k: "wss://x" for k in scripts}, roster=roster,
                       pool_manager=PM, init_topic=INIT, pacer=pacer, clock=clock, run_id="t")
    conn = sqlite3.connect(":memory:")
    conn.executescript(sh.SCHEMA)
    conn.execute("INSERT INTO runs(run_id, started_at, stopped_at) VALUES ('t', 0, 9999)")
    for name, script in scripts.items():
        shadow.stop.clear()
        shadow.connect_fn = lambda url, open_timeout=10, s=script: FakeWS(shadow, clock, s, error)
        shadow.run_socket(name, "wss://x")
        shadow._event(name, "down", 1, "end of script")
    for event_id, tx, at in j7:
        shadow.q.put(("j7", event_id, tx, TRADER, "0xc0in", at, 1))
    while not shadow.q.empty():
        shadow.write(conn, shadow.q.get())
    return shadow, conn, pacer


def test_a_log_one_socket_never_delivered_while_up_is_a_miss_and_lags_are_measured():
    shadow, conn, _ = drive({"drpc": [(1, 1, log("0xaa", 1)), (1, 1, log("0xbb", 2))],
                             "publicnode": [(0.5, 1, log("0xaa", 1))]})
    out = "\n".join(sh.report(conn, ["drpc", "publicnode"]))
    assert "drpc       logs seen 2, MISSED while up 0" in out
    assert "publicnode logs seen 1, MISSED while up 1 {'transfers': 1}" in out


def test_a_log_while_the_socket_was_down_is_not_a_miss():
    # publicnode's whole session ends before drpc delivers 0xcc: down, never missed
    shadow, conn, _ = drive({"publicnode": [(0.5, 1, log("0xaa", 1))],
                             "drpc": [(5, 1, log("0xaa", 1)), (5, 1, log("0xcc", 3))]})
    out = "\n".join(sh.report(conn, ["drpc", "publicnode"]))
    assert "publicnode logs seen 1, MISSED while up 0 {}, socket down 1" in out


def test_a_drpc_limit_answer_pauses_the_account_and_stops_the_run():
    shadow, conn, pacer = drive({"drpc": []}, error={"code": 15, "message": "free plan rate limit"})
    assert shadow.stop.is_set() and shadow.stop_reason.startswith("drpc_limit")
    assert pacer.paused == [sh.__dict__.get("DRPC_LIMIT_PAUSE_S", 60.0)]
    kinds = [r[0] for r in conn.execute("SELECT kind FROM events WHERE socket='drpc' ORDER BY id")]
    assert "limit" in kinds and "up" not in kinds


def test_a_publicnode_refusal_is_not_a_limit_stop():
    shadow, conn, pacer = drive({"publicnode": []}, error={"code": -32000, "message": "nope"})
    assert shadow.stop_reason is None and pacer.paused == []


def test_every_drpc_subscribe_goes_through_the_pacer():
    shadow, conn, pacer = drive({"drpc": []})
    assert pacer.acquired == 3            # connect + 2 more subscribes (3 filters)


def test_a_watched_j7_buy_is_marked_per_socket():
    # publicnode is up over 1000..1002 (it delivers an unrelated log), so the J7 buy at 1001 that it
    # never delivers is a MISS; drpc delivers it at 1003, 2.0 s after J7.
    shadow, conn, _ = drive({"publicnode": [(2, 1, log("0xzz", 9))], "drpc": [(1, 1, log("0xaa", 1))]},
                            j7=[("swap:x:0xaa:%s:m" % TRADER, "0xaa", 1001.0)])
    out = "\n".join(sh.report(conn, ["drpc", "publicnode"]))
    assert "0xaa" in out and "drpc +2.0s" in out and "publicnode MISSED" in out


def test_j7_rows_are_parsed_and_cursor_advances():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE memecoin_events (id INTEGER PRIMARY KEY, fomo_event_id TEXT, mint TEXT, "
               "received_at TEXT, network_id TEXT, side TEXT, delivery_lane TEXT)")
    db.execute("INSERT INTO memecoin_events VALUES (1,'swap:a:0x01:0xt:m','M','2026-10-08T00:00:00Z','4663','buy','j7_wallets')")
    read = sh.make_j7_read(lambda: db)
    cursor, rows = read(None)
    assert cursor == 1 and rows == []                      # starts after the newest event
    db.execute("INSERT INTO memecoin_events VALUES (2,'swap:a:0xAB:0xT:m','M','2026-10-08T00:00:01Z','4663','buy','j7_wallets')")
    db.execute("INSERT INTO memecoin_events VALUES (3,'swap:a:0xcd:0xt:m','M','2026-10-08T00:00:01Z','8453','buy','j7_wallets')")
    cursor, rows = read(cursor)
    assert cursor == 2 and [r[1:3] for r in rows] == [("0xab", "0xt")]


def test_a_funded_push_fault_drops_our_publicnode_socket_only_while_it_is_up():
    clock = Clock()
    shadow = sh.Shadow(db_path=":memory:", sockets={"publicnode": "wss://x"}, roster=sh.Roster(lambda: [TRADER]),
                       pool_manager=PM, init_topic=INIT, clock=clock, run_id="t")
    line = "2026-10-08T08:00:00Z push lane: socket error; reconnecting in 2s\n"
    assert not shadow.funded_fault(line)                    # our socket is not up: nothing to drop
    shadow.up["publicnode"] = True
    assert not shadow.funded_fault("2026-10-08T08:00:00Z push lane: armed set changed (3 -> 4 addresses, +1 -0) — resubscribing")
    assert not shadow.funded_fault(line)                    # one reconnect alone proves nothing
    clock.t += 601
    assert not shadow.funded_fault(line)                    # the first one aged out of the window
    clock.t += 60
    assert shadow.funded_fault(line) and "publicnode" in shadow.dropped
    kinds = []
    while not shadow.q.empty():
        kinds.append(shadow.q.get()[3])
    assert kinds == ["funded_fault", "funded_fault", "dropped"]


def test_any_funded_refusal_drops_publicnode_at_once():
    shadow = sh.Shadow(db_path=":memory:", sockets={"publicnode": "wss://x"}, roster=sh.Roster(lambda: [TRADER]),
                       pool_manager=PM, init_topic=INIT, clock=Clock(), run_id="t")
    shadow.up["publicnode"] = True
    assert shadow.funded_fault("push lane: provider refused the subscription (-32000: Internal error); waiting 60s")


def test_a_dropped_socket_ends_its_session_and_never_reconnects(tmp_path):
    clock = Clock()
    roster = sh.Roster(lambda: [TRADER])
    shadow = sh.Shadow(db_path=":memory:", sockets={"publicnode": "wss://x"}, roster=roster,
                       pool_manager=PM, init_topic=INIT, clock=clock, run_id="t")
    sessions = []

    class Drop(FakeWS):
        def __enter__(self):
            sessions.append(1)
            return self

        def __exit__(self, *a):
            return False

        def recv(self, timeout=None):
            if self.out:
                return json.dumps(self.out.pop(0))
            shadow.funded_fault("push lane: provider refused the subscription (-32000: x); waiting 60s")
            raise TimeoutError
    shadow.connect_fn = lambda url, open_timeout=10: Drop(shadow, clock, [])
    shadow.run_socket("publicnode", "wss://x")              # returns: dropped, not reconnecting
    assert sessions == [1] and "publicnode" in shadow.dropped and shadow.up["publicnode"] is False
