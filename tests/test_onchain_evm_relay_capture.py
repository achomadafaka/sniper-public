"""A refused Relay envelope's tx and receipt are kept as decoder fixtures (2026-10-02, Alpha yes).

16 J7 BSC buys by watched wallets since 09-28 were delivered to the 56 watcher and refused
unsupported_relayer_swap, and no allowed 56 host still serves their receipts: the research dRPC node
keeps block bodies for ~1.5-3 h (block 125288612 answered 0 transactions at head-22k). So the watcher
keeps what it already holds. The detection thread only queues (put_nowait: never blocks, never
reads); one writer thread appends to data/onchain_evm_relay_unsupported.<chain>.jsonl, rotated to
.1 at RELAY_CAPTURE_PART_BYTES, so at most two parts a chain.
"""
import copy
import gzip
import json
import queue
from pathlib import Path

import pytest

import scripts.research.onchain_evm_wallet_watch as w
from sniper.services import memecoin_evm_onchain_watch as M

RELAY = json.load(gzip.open(Path(__file__).parent / "fixtures" / "relay_purchases_2026-09-28.json.gz", "rt"))
BUY = next(b for b in RELAY["buys"] if b["chain"] == "56")


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "_CAPTURE", [])
    monkeypatch.setattr(w, "_CAPTURE_SEEN", w.deque(maxlen=512))
    monkeypatch.setattr(w, "_CAPTURE_STATS", dict.fromkeys(w._CAPTURE_STATS, 0))
    monkeypatch.setattr(w, "STATE_ROOT", tmp_path)
    (tmp_path / "data").mkdir()


def _held_queue(maxsize=64):
    """The capture queue with NO writer thread: what the detection thread queued stays visible."""
    q = queue.Queue(maxsize=maxsize)
    w._CAPTURE.append(q)
    return q


def _refused_relay_buy():
    """The real 56 relay buy without the approval proxy's FundsMovement log: still the Relay Permit2
    call, so the decode refuses it unsupported_relayer_swap."""
    from sniper.execution.evm_relay_quote import RELAY_APPROVAL_PROXY
    receipt = copy.deepcopy(BUY["receipt"])
    receipt["logs"] = [lg for lg in receipt["logs"] if lg["address"].lower() != RELAY_APPROVAL_PROXY]
    return BUY["tx"], receipt


def _detect(tx, receipt, wallet=None):
    wallet = wallet or BUY["wallet"]

    def read(method, params):
        return {"eth_getTransactionByHash": tx, "eth_getTransactionReceipt": receipt}[method]
    log = next(lg for lg in receipt["logs"] if lg["topics"] and lg["topics"][0] == M.TRANSFER_TOPIC
               and lg["topics"][2][-40:] == wallet[2:])
    w._configure("56")
    try:
        outcome = {}
        row = w.push_detection("http://unused", log, {wallet: ("uid", "h")},
                               decimals_cache={log["address"].lower(): 18}, quote_usd=600.0,
                               now="2026-10-02T16:00:00+00:00", read_fn=read, outcome=outcome)
    finally:
        w._configure("4663")
    return row, outcome


def test_a_refused_relay_envelope_is_queued_exactly_as_held_and_nothing_else_is():
    q = _held_queue()
    tx, receipt = _refused_relay_buy()
    row, outcome = _detect(tx, receipt)
    assert row is None and outcome["decode"] == "unsupported_relayer_swap"
    path, record = q.get_nowait()
    assert Path(path).name == "onchain_evm_relay_unsupported.56.jsonl"
    assert record["transaction"] is tx and record["receipt"] is receipt     # as held, not re-read
    assert record["watched"] == [BUY["wallet"]] and record["chain"] == "56"
    assert record["arrival"] == "2026-10-02T16:00:00+00:00"
    _detect(tx, receipt)                                      # the same tx again: one capture
    assert q.empty() and w._CAPTURE_STATS["duplicate"] == 1


def test_a_proven_buy_and_a_non_relay_refusal_queue_nothing():
    q = _held_queue()
    frames = json.load(open(Path(__file__).parent / "fixtures" / "memecoin"
                            / "bsc_third_party_transfers_20260928.json"))["frames"]
    frame = frames[-1]
    row, outcome = _detect(frame["tx"], frame["receipt"], wallet=frame["wallets"][0])
    assert row is None and outcome["decode"] == "not_relay_envelope"
    assert q.empty()


def test_a_full_queue_drops_instead_of_blocking_the_detection_thread():
    q = _held_queue(maxsize=1)
    q.put_nowait(("x", {}))
    tx, receipt = _refused_relay_buy()
    row, outcome = _detect(tx, receipt)
    assert row is None and outcome["decode"] == "unsupported_relayer_swap"
    assert w._CAPTURE_STATS["dropped_full"] == 1 and q.qsize() == 1


def test_the_writer_rotates_to_one_older_part(tmp_path):
    path = tmp_path / "data" / "onchain_evm_relay_unsupported.56.jsonl"
    record = {"tx_hash": "0x" + "ab" * 32, "pad": "x" * 300}
    size = len(json.dumps(record, separators=(",", ":"))) + 1
    for _ in range(7):
        w._capture_write(path, record, part_bytes=3 * size)
    assert path.stat().st_size == size and Path(str(path) + ".1").stat().st_size == 3 * size
    assert w._CAPTURE_STATS["written"] == 7 and w._CAPTURE_STATS["rotated"] == 2
    assert json.loads(path.read_text()) == record
    assert not Path(str(path) + ".2").exists()


def test_a_failed_write_is_counted_and_the_writer_keeps_going(tmp_path):
    q = queue.Queue()
    bad = tmp_path / "data" / "is_a_dir"
    bad.mkdir()
    good = tmp_path / "data" / "onchain_evm_relay_unsupported.56.jsonl"
    q.put((bad, {"a": 1}))
    q.put((good, {"b": 2}))
    seen = []
    real = w._capture_write

    def counted(path, record, part_bytes=None):
        seen.append(path)
        real(path, record, part_bytes)
        if len(seen) == 2:
            raise SystemExit                                  # end the writer loop for the test
    w._capture_write, saved = counted, w._capture_write
    try:
        with pytest.raises(SystemExit):
            w._capture_run(q)
    finally:
        w._capture_write = saved
    assert w._CAPTURE_STATS["write_failed"] == 1 and json.loads(good.read_text()) == {"b": 2}
