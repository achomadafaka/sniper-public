"""4663 receipt hint (Alpha GO 2026-10-16, option B; the 7 identity-matched fast-lane signals that expired
source_pending_expired:proof_absent although their source block came 0.2-1.2 s before the signal).

A deposit-matched re-ask still waiting for the watcher's receipt proof 0.4 s after it was queued asks
the watcher, over a local datagram, to read that receipt now. The watcher binds the receipt to the
hint's tx, wallet, mint and source user, takes the real Transfer log from it and runs its own
on_transfer: the same decode, deposit proof and row as a pushed log. Nothing new decides a buy.
"""
import json
import os
import shutil
import tempfile
import threading
import time

import pytest

from sniper.services import memecoin_receipt_hint as RH

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TX = "0x" + "77" * 32
OTHER_TX = "0x" + "78" * 32
WALLET = "0x" + "12" * 20
MINT = "0x" + "cd" * 20
UID = "u1"
BLOCK_HASH = "0x" + "ab" * 32
HINT = dict(tx_hash=TX, wallet=WALLET, mint=MINT, source_user_id=UID)
WATCHED = {WALLET: (UID, "h1")}


def _transfer(mint=MINT, to=WALLET, tx=TX):
    return {"address": mint, "topics": [TRANSFER, "0x" + "0" * 64, "0x" + "0" * 24 + to[2:]],
            "data": "0x" + "%064x" % 5, "logIndex": "0x3", "transactionIndex": "0x1",
            "transactionHash": tx, "blockHash": BLOCK_HASH, "blockNumber": "0x64", "removed": False}


def _receipt(**over):
    receipt = {"transactionHash": TX, "blockHash": BLOCK_HASH, "blockNumber": "0x64", "status": "0x1",
               "logs": [_transfer(mint="0x" + "ee" * 20, to="0x" + "99" * 20), _transfer()]}
    receipt.update(over)
    return receipt


# ---- the receipt binds to all four hint fields before anything decodes it ----

def test_a_matching_receipt_yields_its_own_transfer_log():
    log, why = RH.receipt_log(_receipt(), HINT, WATCHED)
    assert why is None and log == _transfer()


@pytest.mark.parametrize("receipt, watched, why", [
    (_receipt(transactionHash=OTHER_TX), WATCHED, "tx_mismatch"),
    (_receipt(logs=[_transfer(mint="0x" + "ee" * 20)]), WATCHED, "no_transfer"),
    (_receipt(logs=[_transfer(to="0x" + "99" * 20)]), WATCHED, "no_transfer"),
    (_receipt(), {WALLET: ("someone_else", "h2")}, "wallet_mismatch"),
    (_receipt(), {}, "wallet_mismatch"),
    (_receipt(status="0x0"), WATCHED, "reverted"),
    (_receipt(blockHash=None), WATCHED, "unsealed"),
    (_receipt(logs=[dict(_transfer(), removed=True)]), WATCHED, "no_transfer"),
    ("not a receipt", WATCHED, "malformed"),
])
def test_a_receipt_for_another_tx_mint_or_user_is_refused(receipt, watched, why):
    assert RH.receipt_log(receipt, HINT, watched) == (None, why)


@pytest.mark.parametrize("hint", [
    dict(HINT, tx_hash="0x77"), dict(HINT, wallet="0x12"), dict(HINT, mint=None),
    dict(HINT, source_user_id=""), {k: v for k, v in HINT.items() if k != "mint"},
])
def test_a_malformed_hint_never_encodes(hint):
    assert RH.encode(hint) is None


def test_encode_lowercases_and_decode_reads_back_exactly_four_fields():
    payload = RH.encode(dict(HINT, tx_hash=TX.upper().replace("0X", "0x"), extra="x"))
    assert RH.decode(payload) == HINT
    assert RH.decode(b"{not json") is None
    assert RH.decode(json.dumps(dict(HINT, wallet="0xbad")).encode()) is None


# ---- the watcher's worker: known is a no-op, a null receipt is re-read once, nothing decodes unbound ----

class Reads:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, method, params):
        self.calls.append((method, params))
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        return answer


def _hints(reads, *, known=lambda tx: False, watched=WATCHED):
    handed = []
    hints = RH.ReceiptHints(read_fn=reads, known_fn=known, watched_fn=lambda: watched,
                            on_log=handed.append, log=lambda line: None, null_retry_s=0.0)
    return hints, handed


def test_a_matching_receipt_is_handed_to_the_watchers_own_path_once():
    reads = Reads(_receipt())
    hints, handed = _hints(reads)
    assert hints.handle(HINT) == "handed"
    assert handed == [_transfer()] and reads.calls == [("eth_getTransactionReceipt", [TX])]
    assert hints.stats["outcomes"] == {"handed": 1}


def test_a_tx_the_watcher_already_pushed_is_a_counted_no_op_without_a_read():
    reads = Reads(_receipt())
    hints, handed = _hints(reads, known=lambda tx: tx == TX)
    assert hints.handle(HINT) == "known"
    assert handed == [] and reads.calls == [] and hints.stats["outcomes"] == {"known": 1}


def test_the_push_thread_taking_the_tx_during_the_read_wins():
    state = {"n": 0}

    def known(tx):
        state["n"] += 1
        return state["n"] > 1                    # unknown before the read, known after it
    hints, handed = _hints(Reads(_receipt()), known=known)
    assert hints.handle(HINT) == "known" and handed == []


def test_a_null_receipt_is_read_once_more_then_left_to_the_wait():
    reads = Reads(None, None)
    hints, handed = _hints(reads)
    assert hints.handle(HINT) == "receipt_null"
    assert handed == [] and len(reads.calls) == 2


def test_a_null_then_present_receipt_is_handed():
    hints, handed = _hints(Reads(None, _receipt()))
    assert hints.handle(HINT) == "handed" and handed == [_transfer()]


def test_a_read_error_is_left_to_the_wait():
    hints, handed = _hints(Reads(RuntimeError("timeout")))
    assert hints.handle(HINT) == "read_error" and handed == []


def test_a_known_check_that_fails_hands_nothing():
    def known(tx):
        raise RuntimeError("ledger locked")
    hints, handed = _hints(Reads(_receipt()), known=known)
    assert hints.handle(HINT) == "known_check_error" and handed == []


def test_an_unbound_receipt_never_reaches_the_watcher():
    hints, handed = _hints(Reads(_receipt(transactionHash=OTHER_TX)))
    assert hints.handle(HINT) == "tx_mismatch" and handed == []


def test_the_queue_is_bounded_and_a_full_queue_drops_the_hint():
    hints, _ = _hints(Reads())
    hints.queue.maxsize = 1
    assert hints.offer(RH.encode(HINT)) is True
    assert hints.offer(RH.encode(HINT)) is False
    assert hints.offer(b"junk") is False
    assert hints.stats["queue_full"] == 1 and hints.stats["malformed"] == 1


@pytest.fixture
def sock_dir():
    path = tempfile.mkdtemp(prefix="rh", dir="/tmp")    # an AF_UNIX path must stay under 108 bytes
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_a_datagram_reaches_the_worker_and_a_dead_watcher_costs_the_sender_nothing(sock_dir):
    path = os.path.join(sock_dir, "h.sock")
    sender = RH.Sender(path)
    assert sender.send(dict(HINT, source_user_id=UID)) is False      # no watcher: nowhere to go
    assert sender.stats["failed"] == 1
    reads = Reads(_receipt())
    handed = []
    hints = RH.start(path, read_fn=reads, known_fn=lambda tx: False, watched_fn=lambda: WATCHED,
                     on_log=handed.append, log=lambda line: None)
    try:
        assert sender.send(HINT) is True
        deadline = time.monotonic() + 3
        while not handed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert handed == [_transfer()]
    finally:
        hints.close()
    assert sender.send({"tx_hash": TX}) is False and sender.stats["invalid"] == 1


# ---- the fast lane: one hint per matched re-ask still waiting for the receipt at 0.4 s ----

class Tape:
    """The re-ask's view: the receipt proof is absent until `learned`."""

    def __init__(self):
        self.learned = False

    def source_swap(self, rec, require_source_fill=False):
        if self.learned:
            return {"route_kind": "v3_shared", "source_fill": {"tx_hash": rec["tx_hash"]}}
        return {"reason": "source_fill_pending"}

    def source_blocker(self, rec):
        return "resolved_at_deadline" if self.learned else "proof_absent"


def _rec(**over):
    rec = dict(tx_hash=TX, wallet=WALLET, mint=MINT, source_user_id=UID, seq=100,
               deposit_proof={"order_id": "0x" + "11" * 32}, needs_source_fill=True)
    rec.update(over)
    return rec


def _pending(sent, after_s=0.4):
    from sniper.services import memecoin_rh_fast_decide as fast
    return fast.PendingSourceSwaps(wait_s=2.0, hint_fn=sent.append, hint_after_s=after_s)


def test_a_matched_reask_unresolved_at_0_4_s_hints_once_then_sends_on_the_learned_fill():
    sent, tape = [], Tape()
    pending = _pending(sent)
    assert pending.offer(_rec(), enqueued=0.0, now=10.0)
    assert pending.ready(tape, now=10.3) == [] and sent == []
    assert pending.ready(tape, now=10.41) == [] and [r["tx_hash"] for r in sent] == [TX]
    assert pending.ready(tape, now=10.6) == [] and len(sent) == 1           # once
    tape.learned = True
    [(rec, _enq)] = pending.ready(tape, now=10.7)
    assert rec["tx_hash"] == TX and len(sent) == 1


def test_a_hinted_reask_whose_receipt_never_comes_expires_as_today():
    sent, tape = [], Tape()
    pending = _pending(sent)
    pending.offer(_rec(), enqueued=0.0, now=10.0)
    pending.ready(tape, now=10.5)
    assert pending.ready(tape, now=12.0) == []
    [(_rec_, _enq, blocker)] = pending.pop_expired()
    assert blocker == "proof_absent" and len(sent) == 1


@pytest.mark.parametrize("rec", [
    _rec(deposit_proof=None),                                   # not deposit-matched: Relay's own wait
    _rec(needs_source_fill=False),                              # a deposit re-ask waits for the tape swap
])
def test_only_a_matched_reask_waiting_for_the_receipt_hints(rec, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE", False)
    sent = []
    pending = _pending(sent)
    pending.offer(rec, enqueued=0.0, now=10.0)
    pending.ready(Tape(), now=10.5)
    assert sent == []


def test_no_hint_fn_is_todays_wait():
    from sniper.services import memecoin_rh_fast_decide as fast
    pending = fast.PendingSourceSwaps(wait_s=2.0)
    pending.offer(_rec(), enqueued=0.0, now=10.0)
    assert pending.ready(Tape(), now=10.5) == []


def test_a_hint_fn_that_raises_never_stops_the_reask():
    def boom(rec):
        raise OSError("no socket")
    from sniper.services import memecoin_rh_fast_decide as fast
    pending = fast.PendingSourceSwaps(wait_s=2.0, hint_fn=boom, hint_after_s=0.4)
    tape = Tape()
    pending.offer(_rec(), enqueued=0.0, now=10.0)
    assert pending.ready(tape, now=10.5) == []
    tape.learned = True
    assert len(pending.ready(tape, now=10.6)) == 1


@pytest.mark.parametrize("raw, on, after", [("1", True, 0.4), ("0", False, 0.4), ("", True, 0.4)])
def test_the_knobs_parse(monkeypatch, raw, on, after):
    import importlib
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv("MEMECOIN_RH_FAST_RECEIPT_FETCH", raw)
    try:
        importlib.reload(cfg)
        assert cfg.MEMECOIN_RH_FAST_RECEIPT_FETCH is on
        assert cfg.MEMECOIN_RH_FAST_RECEIPT_FETCH_AFTER_S == after
    finally:
        monkeypatch.undo()                       # the suite's own pin (conftest) back before the reload
        importlib.reload(cfg)


def test_the_live_name_is_abstract_and_a_second_binder_cannot_steal_it():
    assert RH.socket_path("4663").startswith("\0")
    name = "\0sniper-test-receipt-hint-%d-%d" % (os.getpid(), time.monotonic_ns())
    handed = []
    first = RH.start(name, read_fn=Reads(_receipt()), known_fn=lambda tx: False,
                     watched_fn=lambda: WATCHED, on_log=handed.append, log=lambda line: None)
    try:
        with pytest.raises(OSError):
            RH.start(name, read_fn=Reads(), known_fn=lambda tx: False, watched_fn=lambda: WATCHED,
                     on_log=handed.append, log=lambda line: None)
        assert RH.Sender(name).send(HINT) is True
        deadline = time.monotonic() + 3
        while not handed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert handed == [_transfer()]                 # the first binder still owns the name
    finally:
        first.close()


# ---- the watcher end to end: one row, whichever path reaches the tx first ----

@pytest.fixture
def watch4663(monkeypatch, tmp_path, sock_dir):
    import scripts.research.onchain_evm_wallet_watch as w
    from sniper.settings import memecoin as cfg
    from test_memecoin_base_preconf import Ledger

    class Tagged(Ledger):
        """A ledger handle that names the thread which opened it on every INSERT it runs."""
        def __init__(self, log):
            super().__init__(log)
            self.owner = threading.current_thread().name

        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("INSERT"):
                self.log.append(("writer", self.owner, threading.current_thread().name))
            return super().execute(sql, params)
    events = []
    monkeypatch.setattr(w, "get_connection", lambda path: Tagged(events))
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: dict(WATCHED))
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: dict(WATCHED))
    monkeypatch.setattr(w, "_roster_connection", lambda: Ledger([]))
    monkeypatch.setattr(w, "send_wake", lambda address: events.append(("wake",)) or True)
    monkeypatch.setattr(w, "_restart_anchor", lambda: (None, "test"))
    monkeypatch.setattr(w, "_base_lane_hook", lambda observe_only: None)
    monkeypatch.setattr(w, "_relay_origin_worker", lambda *a, **k: None)
    monkeypatch.setattr(w, "_warm_relay", lambda *a, **k: None)
    monkeypatch.setattr(w, "tx_read_host_from_env", lambda **k: None)
    monkeypatch.setattr(w, "erc20_decimals", lambda *a, **k: 18)
    monkeypatch.setattr(w, "_detected_before", lambda conn, chain, tx: False)
    monkeypatch.setattr(w, "get_readonly_connection", lambda path: Ledger([]))
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_RECEIPT_FETCH", True)
    path = os.path.join(sock_dir, "w.sock")
    monkeypatch.setattr(RH, "socket_path", lambda chain: path)
    w._configure("4663")
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")
    yield w, events, path
    w._configure("4663")


def _run_with_hint(monkeypatch, w, path, *, push_first=False, push_during_hint=False):
    """The fake push stream: optionally delivers the tx's own log, and sends one hint. Returns the
    detections (one per push_detection call), the ReceiptHints the watcher started, and its log lines."""
    from sniper.services import memecoin_onchain_push as push
    detections, box, lines = [], [], []
    hint_seen = threading.Event()
    release = threading.Event()

    def detect(rpc_url, log, watched, *, now, quote_usd, outcome=None, preconf=False, **_kw):
        detections.append(log["transactionHash"])
        if push_during_hint:
            hint_seen.set()
            release.wait(3)                      # the push thread is mid-detection while the hint lands
        outcome.update(reason="detected", _transaction={"input": "0x" + "00" * 64})
        return dict(tx_hash=log["transactionHash"], wallet=WALLET, source_user_id=UID, handle="h1",
                    buy={"mint": MINT, "token_delta": 1.0, "source_size_usd": 100.0, "source_price": 100.0,
                         "source_initiation": {"proof_version": "relay-envelope-other-target-v1",
                                               "tx_hash": log["transactionHash"], "block_hash": BLOCK_HASH}},
                    log=log, now=now)
    monkeypatch.setattr(w, "push_detection", detect)
    monkeypatch.setattr(w, "_prove_from_deposit", lambda row, tx: (True, 1.0))
    monkeypatch.setattr(w, "_rpc_result", lambda url, method, params: _receipt()
                        if method == "eth_getTransactionReceipt" else None)
    started = RH.start

    def capture(*a, **k):
        k["log"] = lines.append
        box.append(started(*a, **k))
        return box[-1]
    monkeypatch.setattr(RH, "start", capture)

    def stream(ws_url, **kw):
        deadline = time.monotonic() + 5
        while not box and time.monotonic() < deadline:
            time.sleep(0.01)
        if push_first:
            kw["on_transfer"](_transfer())
        pusher = None
        if push_during_hint:
            pusher = threading.Thread(target=kw["on_transfer"], args=(_transfer(),))
            pusher.start()
            hint_seen.wait(3)
        RH.Sender(path).send(HINT)
        while not lines and time.monotonic() < deadline:
            time.sleep(0.01)
        release.set()
        if pusher is not None:
            pusher.join(3)
        return kw.get("state")
    monkeypatch.setattr(push, "run_push_stream", stream)
    price = w.NativePrice(lambda: 2480.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    for hints in box:
        hints.close()
    return detections, box, lines


def _inserts(events):
    return [e[1] for e in events if e[0] == "insert"]


def test_a_hint_with_no_pushed_log_makes_the_watcher_detect_and_publish_once(watch4663, monkeypatch):
    w, events, path = watch4663
    detections, box, lines = _run_with_hint(monkeypatch, w, path)
    assert detections == [TX] and len(_inserts(events)) == 1
    assert box[0].stats["outcomes"] == {"handed": 1} and "outcome=handed" in lines[0]


def test_the_hint_path_writes_on_its_own_connection_never_the_push_threads(watch4663, monkeypatch):
    """Gamma B1 on e4d9325e7: the hint worker wrote through wconn, the push thread's connection."""
    w, events, path = watch4663
    _run_with_hint(monkeypatch, w, path)
    writers = [e[1:] for e in events if e[0] == "writer"]
    assert writers and all(opened == used == "receipt-hint-work" for opened, used in writers), writers


def test_a_hint_for_a_tx_the_watcher_already_pushed_is_a_no_op(watch4663, monkeypatch):
    w, events, path = watch4663
    detections, box, lines = _run_with_hint(monkeypatch, w, path, push_first=True)
    assert detections == [TX] and len(_inserts(events)) == 1
    assert box[0].stats["outcomes"] == {"known": 1}


def test_a_hint_while_the_push_thread_is_mid_detection_decides_once(watch4663, monkeypatch):
    w, events, path = watch4663
    detections, box, lines = _run_with_hint(monkeypatch, w, path, push_during_hint=True)
    assert detections == [TX] and len(_inserts(events)) == 1
    assert box[0].stats["outcomes"] == {"known": 1}


def test_knob_off_starts_no_listener(watch4663, monkeypatch):
    from sniper.settings import memecoin as cfg
    w, events, path = watch4663
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_RECEIPT_FETCH", False)
    from sniper.services import memecoin_onchain_push as push
    monkeypatch.setattr(push, "run_push_stream", lambda ws_url, **kw: kw.get("state"))
    started = []
    monkeypatch.setattr(RH, "start", lambda *a, **k: started.append(1))
    price = w.NativePrice(lambda: 2480.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    assert started == []
