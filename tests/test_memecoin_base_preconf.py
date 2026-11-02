"""Base 8453 Flashblocks detection, live (spec docs/superpowers/specs/2026-10-09-memecoin-base-flashblocks-
detection-design.md). A preconfirmed read differs from its sealed copy only in block hashes (tx null, receipt
and logs 0x00..0, measured on three Relay txs); the fixtures' armed buys are real sealed reads."""
import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.services import memecoin_base_preconf as P
from sniper.services import memecoin_evm_onchain_watch as M

HERE = Path(__file__).parent
FX = json.loads((HERE / "fixtures" / "base_preconf_relay_buys.json").read_text())
ZERO = "0x" + "0" * 64
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def preconf(copy):
    """The preconfirmed view of a sealed tx and receipt, exactly as measured."""
    tx = dict(copy["tx"], blockHash=None)
    receipt = dict(copy["receipt"], blockHash=ZERO,
                   logs=[dict(log, blockHash=ZERO) for log in copy["receipt"]["logs"]])
    return tx, receipt


def test_a_preconf_read_differs_from_its_sealed_copy_only_in_block_hashes():
    c = FX["captured"][0]
    assert preconf(c["sealed"]) == (c["preconf"]["tx"], c["preconf"]["receipt"])


def _decode(tx, receipt, wallet, chain="8453", **kw):
    return M.relayer_purchase_from_receipt(receipt, tx, wallet, chain=chain, contracts_verified=True,
                                           any_route=True, **kw)


@pytest.mark.parametrize("i", [0, 1])
def test_the_relay_decoder_reads_a_preconfirmed_base_buy(i):
    b = FX["armed"][i]
    tx, receipt = preconf(b["sealed"])
    sealed = _decode(b["sealed"]["tx"], b["sealed"]["receipt"], b["wallet"])
    assert sealed is not None and sealed["mint"] == b["mint"]
    assert _decode(tx, receipt, b["wallet"]) is None                    # today: the hashes differ
    got = _decode(tx, receipt, b["wallet"], preconfirmed=True)
    assert got == dict(sealed, block_hash=ZERO)                          # same facts, 'not sealed yet'


def test_a_sealed_read_is_unchanged_with_the_flag():
    b = FX["armed"][0]
    sealed = _decode(b["sealed"]["tx"], b["sealed"]["receipt"], b["wallet"])
    assert _decode(b["sealed"]["tx"], b["sealed"]["receipt"], b["wallet"], preconfirmed=True) == sealed


@pytest.mark.parametrize("case", ["other_chain", "block_number", "tx_index", "two_real_hashes", "log_real_hash"])
def test_the_preconf_branch_keeps_every_other_identity_check(case):
    b = FX["armed"][0]
    tx, receipt = preconf(b["sealed"])
    chain = "8453"
    if case == "other_chain":
        chain = "4663"
    elif case == "block_number":
        tx = dict(tx, blockNumber=hex(int(tx["blockNumber"], 16) + 1))
    elif case == "tx_index":
        tx = dict(tx, transactionIndex=hex(int(tx["transactionIndex"], 16) + 1))
    elif case == "two_real_hashes":                    # both sealed, into DIFFERENT blocks
        tx, receipt = dict(tx, blockHash="0x" + "ab" * 32), dict(receipt, blockHash="0x" + "cd" * 32)
    elif case == "log_real_hash":                      # a log from another block than the receipt
        logs = list(receipt["logs"])
        logs[0] = dict(logs[0], blockHash="0x" + "ef" * 32)
        receipt = dict(receipt, logs=logs)
    assert _decode(tx, receipt, b["wallet"], chain=chain, preconfirmed=True) is None


# ---- the watcher's read of a preconfirmed transfer ----

RELAY = json.load(gzip.open(HERE / "fixtures" / "relay_purchases_2026-09-28.json.gz", "rt"))


def _detect(b, *, preconf_read=True, outcome=None, extra=None):
    import scripts.research.onchain_evm_wallet_watch as w
    from eth_abi import decode
    tx, receipt = preconf(b["sealed"]) if preconf_read else (b["sealed"]["tx"], b["sealed"]["receipt"])

    def read(method, params):
        if method == "eth_chainId":
            return hex(8453)
        if method == "eth_getCode":
            return RELAY["code_8453"].get(params[0], "0xdeadbeef")
        if method == "eth_getTransactionByHash":
            return tx
        if method == "eth_getTransactionReceipt":
            return receipt
        raise AssertionError(method)
    payment = decode(M.PERMIT2_TYPES, bytes.fromhex(tx["input"][10:]))[1][0][0][0].lower()
    log = next(lg for lg in receipt["logs"] if lg["topics"] and lg["topics"][0] == TRANSFER
               and lg["address"].lower() == b["mint"] and lg["topics"][2][-40:] == b["wallet"][2:])
    w._configure("8453")
    try:
        out = outcome if outcome is not None else {}
        row = w.push_detection("http://unused", log, {b["wallet"]: ("uid", b["handle"])},
                               decimals_cache={b["mint"]: 18, payment: 6}, quote_usd=2480.0,
                               now="2026-10-09T15:00:00+00:00", read_fn=read, outcome=out,
                               payment_price_fn=lambda token: 1.0 if token == payment else None,
                               **(extra or {}))
    finally:
        w._configure("4663")
    return row, out


def test_the_watcher_detects_a_preconfirmed_buy_without_waiting_for_its_block(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_RETRY_S", 2.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_MAX_RETRIES", 5, raising=False)
    slept = []
    b = FX["armed"][0]
    row, outcome = _detect(b, extra={"preconf": True, "sleep_fn": slept.append})
    assert row is not None and outcome["reason"] == "detected" and outcome["retries"] == 0 and not slept
    sealed_row, _ = _detect(b, preconf_read=False)
    assert row["buy"]["source_size_usd"] == sealed_row["buy"]["source_size_usd"]
    assert row["buy"]["source_initiation"]["block_hash"] == ZERO


def test_without_the_flag_a_preconfirmed_read_is_not_a_buy():
    row, outcome = _detect(FX["armed"][0])
    assert row is None


# ---- confirm, move and retract (the watcher's own connection; one short write each) ----

SCHEMA = ("CREATE TABLE onchain_wallet_detections (id INTEGER PRIMARY KEY, signature TEXT UNIQUE, "
          "network_id TEXT, source_slot INTEGER, source_valuation_json TEXT, source_observation_key TEXT, "
          "source_observation_state TEXT)")
TX = "0x" + "aa" * 32


def _db(state="preconfirmed", slot=100, tx=TX):
    conn = sqlite3.connect(":memory:")
    conn.execute(SCHEMA)
    valuation = {"source_initiation": {"block_hash": ZERO, "block_number": slot},
                 "preconf": {"first_at": 1000.0, "block_number": slot}}
    conn.execute("INSERT INTO onchain_wallet_detections (signature, network_id, source_slot, "
                 "source_valuation_json, source_observation_key, source_observation_state) VALUES (?,?,?,?,?,?)",
                 ("8453:%s:0x%s" % (tx, "bb" * 20), "8453", slot, json.dumps(valuation), tx, state))
    return conn


def _row(conn):
    state, slot, raw = conn.execute("SELECT source_observation_state, source_slot, source_valuation_json "
                                    "FROM onchain_wallet_detections").fetchone()
    return state, slot, json.loads(raw)


def test_the_sealed_log_confirms_the_row_with_its_hash_and_the_lead():
    conn = _db()
    sealed = "0x" + "cd" * 32
    assert P.confirm(conn, TX, block=100, block_hash=sealed, sealed_at=1001.25) == 1
    state, slot, v = _row(conn)
    assert (state, slot) == ("canonical", 100)
    assert v["preconf"]["block_hash"] == sealed and v["preconf"]["lead_s"] == pytest.approx(1.25)
    assert v["source_initiation"]["block_hash"] == sealed and not v["preconf"].get("moved")
    assert P.confirm(conn, TX, block=100, block_hash=sealed, sealed_at=1002.0) == 0     # once only


def test_a_buy_sealed_in_a_later_block_moves_the_source_slot():
    conn = _db()
    assert P.confirm(conn, TX, block=101, block_hash="0x" + "cd" * 32, sealed_at=1003.0) == 1
    state, slot, v = _row(conn)
    assert (state, slot, v["preconf"]["moved"]) == ("canonical", 101, True)


@pytest.mark.parametrize("bad", [None, "", ZERO])
def test_a_confirmation_needs_a_real_block_hash(bad):
    conn = _db()
    assert P.confirm(conn, TX, block=100, block_hash=bad, sealed_at=1001.0) == 0
    assert _row(conn)[0] == "preconfirmed"


def test_retract_marks_only_a_preconfirmed_row():
    conn = _db()
    assert P.retract(conn, TX) == 1 and _row(conn)[0] == "retracted"
    conn = _db(state="canonical")
    assert P.retract(conn, TX) == 0 and _row(conn)[0] == "canonical"


def test_another_txs_row_is_never_touched():
    conn = _db(tx="0x" + "ab" * 32)
    assert P.confirm(conn, TX, block=100, block_hash="0x" + "cd" * 32, sealed_at=1.0) == 0
    assert P.retract(conn, TX) == 0


@pytest.mark.parametrize("answer, want", [
    ("null", "null"),                           # no receipt yet: the caller retracts on a second null
    ("same", "canonical"),
    ("later", "canonical"),                     # moved to S+1
    ("zero_hash", None),                        # a node still showing it preconfirmed: ask again
    ("error", None)])                           # a failed read: ask again, never a phantom
def test_resolve_after_the_block_sealed(answer, want):
    conn = _db()

    def read(method, params):
        assert (method, params) == ("eth_getTransactionReceipt", [TX])
        if answer == "error":
            raise OSError("timeout")
        if answer == "null":
            return None
        number = 100 if answer != "later" else 101
        return {"transactionHash": TX, "blockNumber": hex(number), "status": "0x1",
                "blockHash": ZERO if answer == "zero_hash" else "0x" + "cd" * 32}
    assert P.resolve(conn, TX, read_fn=read, now=1004.0) == want
    assert _row(conn)[0] == (want if want in ("canonical", "retracted") else "preconfirmed")


def test_a_reverted_canonical_receipt_is_a_phantom_buy_too():
    conn = _db()
    read = lambda m, p: {"transactionHash": TX, "blockNumber": hex(100), "status": "0x0",  # noqa: E731
                         "blockHash": "0x" + "cd" * 32}
    assert P.resolve(conn, TX, read_fn=read, now=1004.0) == "retracted"


# ---- the pending filter and the knob ----

def test_the_pending_filter_is_the_watchers_transfer_filter_on_pending_logs():
    wallet = "0x" + "12" * 20
    assert P.pending_filter([wallet]) == ["pendingLogs", {"topics": [TRANSFER, None, ["0x" + "0" * 24 + "12" * 20]]}]
    assert P.pending_filter([]) is None and P.pending_filter(["nonsense"]) is None   # never a wildcard


@pytest.mark.parametrize("raw, want", [("", frozenset()), ("8453", frozenset({"8453"})),
                                       (" 8453 ,", frozenset({"8453"})), ("8453,4663", frozenset()),
                                       ("base", frozenset())])
def test_the_knob_lists_base_only_and_malformed_is_off(monkeypatch, raw, want):
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS", raw)
    assert cfg.memecoin_onchain_evm_preconf_chains() == want


# ---- the preconf socket (own thread; a fake connection) ----

WALLET = "0x" + "12" * 20


class FakeWs:
    """Answers chainId and subscribe requests; then plays `frames` (dict or 'timeout')."""

    def __init__(self, frames=(), chain="0x2105", refuse=None):
        self.frames, self.chain, self.refuse = list(frames), chain, refuse
        self.sent, self.closed, self._replies = [], False, []

    def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if msg["method"] == "eth_chainId":
            self._replies.append({"jsonrpc": "2.0", "id": msg["id"], "result": self.chain})
        elif self.refuse and msg["params"][0] == self.refuse:
            self._replies.append({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "no"}})
        else:
            self._replies.append({"jsonrpc": "2.0", "id": msg["id"], "result": "0xsub%s" % msg["params"][0]})

    def recv(self, timeout=None):
        if self._replies:
            return json.dumps(self._replies.pop(0))
        if not self.frames:
            raise TimeoutError
        frame = self.frames.pop(0)
        if frame == "timeout":
            raise TimeoutError
        if isinstance(frame, Exception):
            raise frame
        return json.dumps(frame)

    def close(self):
        self.closed = True


def head(n):
    return {"jsonrpc": "2.0", "method": "eth_subscription",
            "params": {"subscription": "0xsubnewHeads", "result": {"number": hex(n)}}}


def pending(tx, block=100):
    return {"jsonrpc": "2.0", "method": "eth_subscription",
            "params": {"subscription": "0xsubpendingLogs",
                       "result": {"transactionHash": tx, "blockNumber": hex(block), "blockHash": ZERO}}}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s) if hasattr(self, "slept") else setattr(self, "slept", [s])
        self.t += s


def _run(sockets, *, roster=(WALLET,), stop_after=None, force=None, step=1.0):
    clock, events, made = Clock(), [], []
    sockets = list(sockets)
    rosters = list(roster) if roster and isinstance(roster[0], (list, tuple)) else None

    def connect(url):
        assert url == P.PRECONF_WS
        made.append(sockets[0])
        return sockets.pop(0)

    def recv_clock(ws):
        real = ws.recv

        def recv(timeout=None):
            clock.t += step
            return real(timeout)
        ws.recv = recv
    for ws in sockets:
        recv_clock(ws)
    budget = [stop_after or 60]

    def should_stop():
        budget[0] -= 1
        return budget[0] < 0 or (not sockets and all(not w.frames and not w._replies for w in made))

    def addresses():
        if rosters:
            return rosters.pop(0) if len(rosters) > 1 else rosters[0]
        return list(roster)
    P.run_preconf_stream(on_log=lambda log: events.append(("log", log["transactionHash"])),
                         on_head=lambda n: events.append(("head", n)), addresses_fn=addresses,
                         connect_fn=connect, should_stop=should_stop, now_fn=clock, sleep_fn=clock.sleep,
                         say=lambda line: events.append(("say", line)), force_reconnect=force,
                         roster_check_s=5.0)
    return events, made, getattr(clock, "slept", [])


def test_the_stream_checks_base_then_subscribes_heads_and_the_pending_filter():
    ws = FakeWs([head(100), pending("0x" + "aa" * 32), head(101)])
    events, made, _ = _run([ws])
    methods = [(m["method"], m["params"][0] if m["params"] else None) for m in ws.sent]
    assert methods == [("eth_chainId", None), ("eth_subscribe", "newHeads"), ("eth_subscribe", "pendingLogs")]
    assert ws.sent[2]["params"] == P.pending_filter([WALLET])
    assert [e for e in events if e[0] != "say"] == [("head", 100), ("log", "0x" + "aa" * 32), ("head", 101)]


def test_a_wrong_chain_stops_the_preconf_socket_for_good():
    ws = FakeWs([head(1)], chain="0x1")
    events, made, _ = _run([ws, FakeWs()])
    assert len(made) == 1 and ws.closed and [m["method"] for m in ws.sent] == ["eth_chainId"]
    assert any("chain" in e[1] for e in events if e[0] == "say")


def test_an_empty_roster_subscribes_nothing():
    events, made, _ = _run([FakeWs()], roster=(), stop_after=3)
    assert made == []


def test_a_refused_subscription_waits_a_minute_or_more():
    events, made, slept = _run([FakeWs(refuse="pendingLogs"), FakeWs([head(5)])])
    assert len(made) == 2 and made[0].closed and slept and slept[0] >= 60.0


def test_a_socket_error_waits_a_minute_or_more():
    events, made, slept = _run([FakeWs([head(5), OSError("reset")]), FakeWs([head(6)])])
    assert len(made) == 2 and slept[0] >= 60.0 and ("head", 6) in events


def test_a_deaf_socket_is_replaced():
    deaf = FakeWs([head(5)] + ["timeout"] * 12)           # 12 s without a head
    events, made, _ = _run([deaf, FakeWs([head(7)])])
    assert len(made) == 2 and deaf.closed and ("head", 7) in events


def test_a_forced_reconnect_replaces_the_socket():
    import threading
    force = threading.Event()
    first = FakeWs([head(5), "timeout", "timeout"])
    original = first.recv

    def recv(timeout=None):
        out = original(timeout)
        force.set()
        return out
    first.recv = recv
    events, made, _ = _run([first, FakeWs([head(9)])], force=force)
    assert len(made) == 2 and first.closed and not force.is_set()


def test_a_roster_change_resubscribes_with_the_new_filter():
    other = "0x" + "34" * 20
    first = FakeWs([head(5)] + ["timeout"] * 6)
    second = FakeWs([head(6)])
    events, made, _ = _run([first, second], roster=([WALLET], [WALLET], [WALLET, other]))
    assert len(made) == 2 and second.sent[2]["params"] == P.pending_filter([WALLET, other])


# ---- the tracker: what to do once a preconfirmed transaction's block has sealed ----

def test_a_preconfirmed_tx_is_due_once_its_block_has_sealed():
    t = P.Tracker()
    t.add(TX, block=100, first_at=1.0)
    assert t.due(100) == [] and t.due(101) == [TX]
    assert t.sealed(TX) is not None and t.due(101) == [] and t.sealed(TX) is None


@pytest.mark.parametrize("answers, want", [
    (["canonical"], ["confirmed"]),
    (["retracted"], ["page:retracted"]),
    (["null"], []),                                           # one null: ask again at the next head
    (["null", "null"], ["retract", "page:retracted"]),
    ([None] * 4, []),
    ([None] * 5, ["unresolved", "page:preconf_unresolved"])])
def test_settle(answers, want):
    t = P.Tracker()
    t.add(TX, block=100, first_at=1.0)
    out = []
    for i, answer in enumerate(answers):
        P.settle(t, 101 + i, resolve=lambda tx, a=answer: a,
                 retract=lambda tx: out.append("retract") or 1,
                 unresolved=lambda tx: out.append("unresolved") or 1,
                 page=lambda kind, tx: out.append("page:" + kind),
                 confirmed=lambda tx: out.append("confirmed"))
    assert out == want
    assert (t.due(200) == []) == bool(want)


# ---- in the 8453 watcher (run_push driven by fake sealed and preconf streams) ----

class Ledger:
    """A fake funded-ledger handle: records INSERT columns+values and UPDATEs; every UPDATE hits a row."""

    def __init__(self, log):
        self.log, self.rowcount = log, 1

    def execute(self, sql, params=()):
        text = sql.lstrip()
        if text.upper().startswith("INSERT"):
            cols = text.split("(", 1)[1].split(")", 1)[0].split(",")
            self.log.append(("insert", dict(zip([c.strip() for c in cols], params))))
        elif text.upper().startswith("UPDATE"):
            self.log.append(("update", text, params))
        return self

    def commit(self):
        self.log.append(("commit",))

    def close(self):
        pass


SEALED_HASH = "0x" + "cd" * 32
BUY_TX = "0x" + "77" * 32


@pytest.fixture
def watch8453(monkeypatch, tmp_path):
    import scripts.research.onchain_evm_wallet_watch as w
    events = []
    monkeypatch.setattr(w, "get_connection", lambda path: Ledger(events))
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_roster_connection", lambda: Ledger([]))
    monkeypatch.setattr(w, "send_wake", lambda address: events.append(("wake",)) or True)
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")
    monkeypatch.setattr(w, "_restart_anchor", lambda: (None, "test"))
    monkeypatch.setattr(w, "_base_lane_hook", lambda observe_only: None)
    monkeypatch.setattr(w, "_relay_origin_worker", lambda *a, **k: None)
    monkeypatch.setattr(w, "_warm_relay", lambda *a, **k: None)
    monkeypatch.setattr(w, "tx_read_host_from_env", lambda: None)
    monkeypatch.setattr(w, "erc20_decimals", lambda *a, **k: 18)
    import sniper.services.memecoin_alerts as alerts
    monkeypatch.setattr(alerts, "send_alert", lambda kind, text, **k: events.append(("alert", kind, text)))
    w._configure("8453")
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")      # _configure set the chain's own
    yield w, events
    w._configure("4663")


def _buy_row(tx, log, now):
    proof = {"proof_version": "relay-envelope-other-target-v1", "tx_hash": tx, "block_hash": ZERO}
    return dict(tx_hash=tx, wallet=WALLET, source_user_id="u1", handle="h1",
                buy={"mint": "0x" + "cd" * 20, "token_delta": 1.0, "source_size_usd": 100.0,
                     "source_price": 100.0, "source_initiation": proof}, log=log, now=now)


def _log(tx, block, block_hash):
    return {"transactionHash": tx, "blockNumber": hex(block), "blockHash": block_hash,
            "blockTimestamp": hex(1790000000), "address": "0x" + "cd" * 20,
            "topics": [TRANSFER, ZERO, "0x" + "0" * 24 + WALLET[2:]],
            "data": "0x" + "%064x" % 1, "logIndex": "0x0", "transactionIndex": "0x0"}


def _drive(monkeypatch, w, *, preconf_logs=(), sealed=(), proved=True, resolve=None, heads=(),
           pre_heads=(), check=None):
    """The preconf fake stream plays its logs (each decode awaited) then lets the sealed fake run:
    its transfers, then its heads (each settle awaited)."""
    import threading
    calls, done, seen = {"preconf": 0, "hosts": []}, threading.Event(), {}

    def detect(rpc_url, log, watched, *, now, quote_usd, outcome=None, preconf=False, **_kw):
        calls["hosts"].append((rpc_url, preconf))
        outcome.update(reason="detected", _transaction={"input": "0x" + "00" * 64})
        return _buy_row(log["transactionHash"], log, now)
    monkeypatch.setattr(w, "push_detection", detect)
    monkeypatch.setattr(w, "_prove_from_deposit", lambda row, tx: (True, 1.0) if proved else (False, "no_deposit"))
    if resolve is not None:
        monkeypatch.setattr(P, "resolve", lambda conn, tx, read_fn, now: resolve.pop(0))

    def preconf_stream(**kw):
        calls["preconf"] += 1
        seen["force"] = kw["force_reconnect"]
        for n in pre_heads:
            kw["on_head"](n)
        for log in preconf_logs:
            kw["on_log"](log).result(timeout=5)
        done.set()
    monkeypatch.setattr(P, "run_preconf_stream", preconf_stream)

    def sealed_stream(ws_url, **kw):
        done.wait(5)
        for log in sealed:
            kw["on_transfer"](log)
        for n in heads:
            kw["state"].head(time.time(), n, 1790000000 + n)
            kw["on_head"]()
            time.sleep(0.05)
        return kw.get("state")
    import time
    from sniper.services import memecoin_onchain_push as push
    monkeypatch.setattr(push, "run_push_stream", sealed_stream)
    price = w.NativePrice(lambda: 2480.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    return calls, seen


def _inserts(events):
    return [e[1] for e in events if e[0] == "insert"]


def _updates(events):
    return [e[1] for e in events if e[0] == "update"]


def test_off_by_default_no_preconf_socket(watch8453, monkeypatch):
    w, events = watch8453
    monkeypatch.delenv("MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS", raising=False)
    calls, _ = _drive(monkeypatch, w, sealed=[_log(BUY_TX, 100, SEALED_HASH)])
    assert calls["preconf"] == 0
    assert [r.get("source_observation_state") for r in _inserts(events)] == [None]


def test_a_preconfirmed_buy_is_published_and_its_sealed_log_confirms_it(watch8453, monkeypatch):
    w, events = watch8453
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS", "8453")
    calls, _ = _drive(monkeypatch, w, preconf_logs=[_log(BUY_TX, 100, ZERO)],
                      sealed=[_log(BUY_TX, 100, SEALED_HASH)])
    rows = _inserts(events)
    assert len(rows) == 1 and rows[0]["source_observation_state"] == "preconfirmed"
    assert rows[0]["source_observation_key"] == BUY_TX and rows[0]["source_slot"] == 100
    assert json.loads(rows[0]["source_valuation_json"])["preconf"]["block_number"] == 100
    assert "source_fill" not in json.loads(rows[0]["source_valuation_json"])     # no block hash yet
    assert calls["hosts"] == [(P.PRECONF_HTTP, True)]                           # one decode, preconf host
    (sql,) = [u for u in _updates(events) if "canonical" in u]
    assert ("wake",) in events


def test_a_buy_the_deposit_cannot_prove_is_left_to_its_sealed_log(watch8453, monkeypatch):
    w, events = watch8453
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS", "8453")
    calls, _ = _drive(monkeypatch, w, preconf_logs=[_log(BUY_TX, 100, ZERO)],
                      sealed=[_log(BUY_TX, 100, SEALED_HASH)], proved=False)
    rows = _inserts(events)
    assert len(rows) == 1 and rows[0].get("source_observation_state") is None       # today's row
    assert [h for _u, h in calls["hosts"]] == [True, False]


@pytest.mark.parametrize("bad", ["removed", "zero_hash", "missing_block", "negative_block", "wrong_topic", "wrong_recipient",
                                 "missing_data", "missing_address", "missing_log_index", "missing_tx_index",
                                 "malformed_topics", "unpadded_recipient", "wrong_address", "non_hex_data"])
def test_invalid_sealed_log_keeps_source_for_canonical_null_resolution(watch8453, monkeypatch, bad):
    w, events = watch8453
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS", "8453")
    log = _log(BUY_TX, 100, SEALED_HASH)
    if bad == "removed":
        log["removed"] = True
    elif bad == "zero_hash":
        log["blockHash"] = ZERO
    elif bad == "missing_block":
        log.pop("blockNumber")
    elif bad == "negative_block":
        log["blockNumber"] = "-0x1"
    elif bad == "wrong_topic":
        log["topics"][0] = ZERO
    elif bad == "wrong_recipient":
        log["topics"][2] = ZERO
    elif bad.startswith("missing_"):
        log.pop({"missing_data": "data", "missing_address": "address",
                 "missing_log_index": "logIndex", "missing_tx_index": "transactionIndex"}[bad])
    elif bad == "malformed_topics":
        log["topics"] = {0: TRANSFER, 1: ZERO, 2: log["topics"][2]}
    elif bad == "unpadded_recipient":
        log["topics"][2] = "0x" + "ff" * 12 + WALLET[2:]
    elif bad == "wrong_address":
        log["address"] = "0xbroken"
    else:
        log["data"] = "0x" + "gg" * 32
    _drive(monkeypatch, w, preconf_logs=[_log(BUY_TX, 100, ZERO)], sealed=[log],
           heads=[100, 101, 102], resolve=["null", "null"])
    assert not any("canonical" in sql for sql in _updates(events))
    assert any("retracted" in sql for sql in _updates(events))


def test_a_preconfirmed_buy_that_never_seals_is_retracted_and_paged(watch8453, monkeypatch):
    w, events = watch8453
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS", "8453")
    _drive(monkeypatch, w, preconf_logs=[_log(BUY_TX, 100, ZERO)], heads=[100, 101, 102],
           resolve=["null", "null"])
    assert any("retracted" in u for u in _updates(events))
    assert [e[1] for e in events if e[0] == "alert"] == ["onchain_preconf"]


def test_sealed_transfers_the_live_preconf_socket_missed_force_a_reconnect(watch8453, monkeypatch):
    w, events = watch8453
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS", "8453")
    sealed = [_log("0x%064x" % i, 100, SEALED_HASH) for i in (1, 2, 3)]
    _calls, seen = _drive(monkeypatch, w, sealed=sealed, pre_heads=[99])
    assert seen["force"].is_set()


def test_a_dead_preconf_socket_counts_no_misses(watch8453, monkeypatch):
    w, events = watch8453
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS", "8453")
    sealed = [_log("0x%064x" % i, 100, SEALED_HASH) for i in (1, 2, 3)]
    _calls, seen = _drive(monkeypatch, w, sealed=sealed)                            # no preconf head yet
    assert not seen["force"].is_set()


def test_a_preconf_sighting_that_records_nothing_leaves_the_tx_to_its_sealed_log(watch8453, monkeypatch):
    w, events = watch8453
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS", "8453")
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_TX_DEDUPE_S", 60.0)
    hosts = []

    def detect(rpc_url, log, watched, *, now, quote_usd, outcome=None, preconf=False, **_kw):
        hosts.append(preconf)
        if preconf:                                    # nothing to decode in the preconfirmed read
            outcome.update(reason="no_buy")
            return None
        outcome.update(reason="detected", _transaction={"input": "0x" + "00" * 64})
        return _buy_row(log["transactionHash"], log, now)
    real_drive = _drive

    def drive(*a, **k):
        out = real_drive(*a, **k)
        return out
    monkeypatch.setattr(w, "_prove_from_deposit", lambda row, tx: (True, 1.0))
    import threading
    done = threading.Event()

    def preconf_stream(**kw):
        kw["on_log"](_log(BUY_TX, 100, ZERO)).result(timeout=5)
        done.set()

    def sealed_stream(ws_url, **kw):
        done.wait(5)
        kw["on_transfer"](_log(BUY_TX, 100, SEALED_HASH))
        return kw.get("state")
    from sniper.services import memecoin_onchain_push as push
    monkeypatch.setattr(P, "run_preconf_stream", preconf_stream)
    monkeypatch.setattr(push, "run_push_stream", sealed_stream)
    monkeypatch.setattr(w, "push_detection", detect)
    price = w.NativePrice(lambda: 2480.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    assert hosts == [True, False] and len(_inserts(events)) == 1


def test_a_seen_tx_resets_the_missed_count(watch8453, monkeypatch):
    w, events = watch8453
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS", "8453")
    seen_tx = "0x%064x" % 9
    sealed = [_log("0x%064x" % 1, 100, SEALED_HASH), _log("0x%064x" % 2, 100, SEALED_HASH),
              _log(seen_tx, 100, SEALED_HASH)]
    _calls, seen = _drive(monkeypatch, w, preconf_logs=[_log(seen_tx, 100, ZERO)], sealed=sealed,
                          pre_heads=[99], proved=False)
    assert not seen["force"].is_set()


@pytest.mark.parametrize("preconf_read, fill", [(True, False), (False, True)])
def test_a_source_fill_is_written_only_with_a_real_block_hash(preconf_read, fill):
    # Without the guard a preconfirmed row would carry a source fill bound to block 0x00..0.
    import scripts.research.onchain_evm_wallet_watch as w
    b = FX["armed"][0]
    row, _outcome = _detect(b, preconf_read=preconf_read, extra={"preconf": preconf_read})
    log = dict(row["log"], transactionHash=row["tx_hash"],
               blockHash=ZERO if preconf_read else b["sealed"]["receipt"]["blockHash"])
    events = []
    w._configure("8453")
    try:
        w._record(Ledger(events), **dict(row, log=log, valuation={}))
    finally:
        w._configure("4663")
    (insert,) = _inserts(events)
    assert ("source_fill" in json.loads(insert["source_valuation_json"])) is fill


# ---- D1(b): one local read before signing refuses a retracted source (Alpha, 2026-10-09) ----

ORIGIN = "onchain:8453:%s:0x%s" % (TX, "bb" * 20)


def _ledger_file(tmp_path, state):
    # One file name per state: pytest reuses a passed test's tmp dir for the next parameter, and the
    # presign handle is cached per path for the life of the thread.
    path = tmp_path / ("ledger-%s.db" % state)
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    if state != "absent":
        conn.execute("INSERT INTO onchain_wallet_detections (signature, network_id, source_observation_state) "
                     "VALUES (?,?,?)", (ORIGIN[len("onchain:"):], "8453", state))
    conn.commit()
    conn.close()
    return str(path)


@pytest.mark.parametrize("state, want", [("retracted", "source_retracted"), ("preconfirmed", None),
                                         ("canonical", None), ("preconf_unresolved", None), (None, None),
                                         ("absent", None)])
def test_the_presign_read_refuses_only_a_retracted_source(tmp_path, state, want):
    check = P.presign_check(ORIGIN, ledger_path=_ledger_file(tmp_path, state))
    assert check is not None and check() == want


# rhfast:8453 is the Base fast lane's own origin for the same source since step 1 (Alpha's ruling
# 2026-10-14: a retraction before its send aborts it), so it is read like onchain:8453.
@pytest.mark.parametrize("origin", [None, "", "swap:uuid", "onchain:4663:%s:0xm" % TX,
                                    "rhfast:4663:%s:0xm" % TX, "bscfast:8453:x:y"])
def test_no_presign_read_for_anything_but_a_base_on_chain_detection(origin):
    assert P.presign_check(origin, ledger_path="/nonexistent") is None


def test_an_unreadable_ledger_never_refuses_and_says_so(tmp_path, caplog):
    check = P.presign_check(ORIGIN, ledger_path=str(tmp_path / "missing.db"))
    assert check() is None and "presign read failed" in caplog.text


def _leg(monkeypatch, tmp_path, *, state="retracted", executor_takes=True):
    from test_memecoin_evm_leg import _c, _Ev
    from sniper.services import memecoin_evm_leg as leg
    c, kill = _c(monkeypatch, tmp_path)
    ev = _Ev()
    ev.fomo_event_id = ORIGIN
    monkeypatch.setattr(P, "_ledger_path", lambda: _ledger_file(tmp_path, state))
    seen = {}

    def execute_fn(conn, pid, mint, size, now_iso, presign_check_fn=None, safety_gate_fn=None):
        seen["presign"] = presign_check_fn
        reason = presign_check_fn() if presign_check_fn else None
        if reason:
            return {"status": "unsafe_gate", "reason": reason}
        return {"status": "filled", "position_id": pid}

    def bare_execute_fn(conn, pid, mint, size, now_iso, safety_gate_fn=None):
        seen["bare"] = True
        return {"status": "filled", "position_id": pid}
    r = leg.evm_real_entry(c, ev, route="evm:8453", day="2026-10-09", usdc_balance=300.0, native_balance=1.0,
                           now_iso="2026-10-09T00:00:00Z", trade_size=10.0,
                           token_safety_fn=lambda mint, tax_cap=None: (True, "ok"),
                           execute_fn=execute_fn if executor_takes else bare_execute_fn, kill_path=kill)
    return r, seen


def test_the_base_leg_hands_the_executor_the_presign_read(monkeypatch, tmp_path):
    r, seen = _leg(monkeypatch, tmp_path)
    assert seen["presign"] is not None and r["status"] == "unsafe" and r["reason"] == "source_retracted"
    r, seen = _leg(monkeypatch, tmp_path, state="canonical")
    assert r["status"] == "filled"


def test_an_executor_without_the_presign_seam_refuses_a_base_on_chain_entry(monkeypatch, tmp_path):
    r, seen = _leg(monkeypatch, tmp_path, executor_takes=False)
    assert r["reason"] == "preconf_presign_unavailable" and "bare" not in seen


# ---- the on-chain lane re-checks a HELD Base row for retraction, as it does on Ink ----

def test_the_lane_drops_a_held_base_row_once_it_is_retracted():
    from sniper.services.memecoin_onchain_lane import sweep_onchain_detections
    from scripts.research.onchain_wallet_watch import SCHEMA as WATCH_SCHEMA
    from test_memecoin_onchain_lane import NOW, SLOT_SCHEMA
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(WATCH_SCHEMA)
    conn.executescript(SLOT_SCHEMA)
    sig = ORIGIN[len("onchain:"):]
    conn.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, handle, address, mint, "
                 "network_id, detected_at, created_at, source_observation_state) VALUES (?,?,?,?,?,?,?,?,?)",
                 (sig, "u1", "h1", "a", "0x" + "bb" * 20, "8453", NOW.isoformat(), NOW.isoformat(), "retracted"))
    row = conn.execute("SELECT * FROM onchain_wallet_detections").fetchone()
    held = {(sig, "u1", "8453", "0x" + "bb" * 20): dict(row)}
    calls = []
    _cursor, results = sweep_onchain_detections(conn, lambda c, ev: calls.append(ev), cursor=1,
                                               now_fn=lambda: NOW.isoformat(), max_age_s=45,
                                               approved_ids_fn=lambda c, n: {"u1"}, deferred=held)
    assert calls == [] and held == {}
    assert results[0]["result"] == {"status": "skipped", "reason": "source_retracted"}


# ---- Gamma MEDIUM on b91757822: a sealed log that lands while the preconf decode of the same tx runs ----

def _collide(monkeypatch, w, *, proved):
    """The preconf decode of BUY_TX blocks until the sealed log of BUY_TX has been handed to on_transfer."""
    import threading
    from sniper.services import memecoin_onchain_push as push
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    hosts = []

    def detect(rpc_url, log, watched, *, now, quote_usd, outcome=None, preconf=False, **_kw):
        hosts.append(preconf)
        if preconf:
            started.set()
            release.wait(5)
        outcome.update(reason="detected", _transaction={"input": "0x" + "00" * 64})
        return _buy_row(log["transactionHash"], log, now)
    monkeypatch.setattr(w, "push_detection", detect)
    monkeypatch.setattr(w, "_prove_from_deposit", lambda row, tx: (True, 1.0) if proved else (False, "no_deposit"))

    def preconf_stream(**kw):
        future = kw["on_log"](_log(BUY_TX, 100, ZERO))
        future.result(timeout=10)
        finished.set()

    def sealed_stream(ws_url, **kw):
        started.wait(5)
        kw["on_transfer"](_log(BUY_TX, 100, SEALED_HASH))          # mid-decode
        release.set()
        finished.wait(10)
        return kw.get("state")
    monkeypatch.setattr(P, "run_preconf_stream", preconf_stream)
    monkeypatch.setattr(push, "run_push_stream", sealed_stream)
    price = w.NativePrice(lambda: 2480.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    return hosts


def test_a_sealed_log_during_a_preconf_decode_that_records_nothing_is_still_detected(watch8453, monkeypatch):
    w, events = watch8453
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS", "8453")
    hosts = _collide(monkeypatch, w, proved=False)                    # preconf: needs the seal
    rows = _inserts(events)
    assert hosts == [True, False] and len(rows) == 1 and rows[0].get("source_observation_state") is None


def test_a_sealed_log_during_a_preconf_decode_that_records_confirms_it(watch8453, monkeypatch):
    w, events = watch8453
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS", "8453")
    hosts = _collide(monkeypatch, w, proved=True)
    rows = _inserts(events)
    assert hosts == [True] and len(rows) == 1 and rows[0]["source_observation_state"] == "preconfirmed"
    assert [u for u in _updates(events) if "canonical" in u]


def test_preconf_decode_keeps_socket_arrival_across_worker_queue(watch8453, monkeypatch):
    import threading
    from sniper.services import memecoin_onchain_push as push
    w, events = watch8453
    monkeypatch.setenv('MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS', '8453')
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    tx2 = '0x' + '88' * 32
    now = ['2026-10-10T13:00:00+00:00']
    detected = {}
    monkeypatch.setattr(w, 'now_iso', lambda: now[0])
    # One decoder keeps the second observation queued, so its worker clock differs from arrival.
    import concurrent.futures
    original_executor = concurrent.futures.ThreadPoolExecutor
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor',
                        lambda **kw: original_executor(**dict(kw, max_workers=1)))
    original_work = getattr(P, 'DecodeWork', None)
    if original_work is not None:
        monkeypatch.setattr(P, 'DecodeWork', lambda callback, **kw: original_work(callback, workers=1, **kw))

    def detect(rpc_url, log, watched, *, now, outcome=None, **kw):
        detected[log['transactionHash']] = now
        if log['transactionHash'] == BUY_TX:
            entered.set()
            assert release.wait(2)
        outcome.update(reason='detected', _transaction={'input': '0x' + '00' * 64})
        return _buy_row(log['transactionHash'], log, now)
    monkeypatch.setattr(w, 'push_detection', detect)
    monkeypatch.setattr(w, '_prove_from_deposit', lambda *_: (True, 1.0))

    def preconf_stream(**kw):
        first = kw['on_log'](_log(BUY_TX, 100, ZERO))
        assert entered.wait(2)
        now[0] = '2026-10-10T13:00:01+00:00'
        second = kw['on_log'](_log(tx2, 100, ZERO))
        now[0] = '2026-10-10T13:00:02+00:00'
        release.set()
        first.result(timeout=2)
        second.result(timeout=2)
        done.set()
    monkeypatch.setattr(P, 'run_preconf_stream', preconf_stream)
    monkeypatch.setattr(push, 'run_push_stream', lambda *a, **kw: done.wait(3))
    price = w.NativePrice(lambda: 2480.0, log_fn=lambda _: None)
    price.refresh()
    w.run_push('wss://test', price=price)
    assert detected[tx2] == '2026-10-10T13:00:01+00:00'
