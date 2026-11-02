import json
import sqlite3
import queue
import threading
from pathlib import Path

from sniper.persistence import db_evm_stream as stream
from sniper.services.memecoin_ink_subblocks import InkSubblockState


def _observation(index=0):
    frames = json.loads(Path("docs/superpowers/research/2026-09-29-ink-evidence/subblock-sample.json").read_text())
    state = InkSubblockState()
    for frame_index in range(3):
        result = state.accept(frames[frame_index], received_mono_ns=frame_index, received_utc="t")
    return result["observations"][index]


def test_journal_survives_crash_before_wake_and_repeat_projection(tmp_path):
    path = tmp_path / "stream.sqlite"
    observation = _observation()
    with sqlite3.connect(path) as conn:
        stream.ensure_stream_tables(conn)
        assert stream.insert_observation(conn, observation) is True
    with sqlite3.connect(path) as conn:
        stream.ensure_stream_tables(conn)
        pending = stream.next_unprojected(conn)
        assert len(pending) == 1
        assert pending[0]["observation_key"] == observation["observation_key"]
        assert stream.insert_observation(conn, observation) is False
        stream.advance_cursor(conn, pending[0]["id"])
    with sqlite3.connect(path) as conn:
        assert stream.next_unprojected(conn) == []


def test_retracted_payload_keeps_order_ledger_outside_stream_db(tmp_path):
    observation = _observation()
    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    conn.row_factory = sqlite3.Row
    stream.ensure_stream_tables(conn)
    stream.insert_observation(conn, observation)
    stream.invalidate_payload(conn, observation["payload_id"])
    row = stream.observation(conn, observation["observation_key"])
    assert row["state"] == "retracted"
    assert row["tx_hash"] == observation["tx_hash"]


def test_canonical_receipt_requires_same_block_and_transaction(tmp_path):
    observation = _observation()
    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    conn.row_factory = sqlite3.Row
    stream.ensure_stream_tables(conn)
    stream.insert_observation(conn, observation)
    wrong = {"transactionHash": observation["tx_hash"], "blockHash": "0x" + "d" * 64,
             "blockNumber": hex(observation["block_number"]), "status": "0x1", "logs": []}
    assert stream.reconcile_canonical(conn, observation["observation_key"], wrong,
                                      block_hash="0x" + "c" * 64) == "retracted"


def test_matching_canonical_receipt_has_real_log_indexes(tmp_path):
    observation = _observation(2)
    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    stream.ensure_stream_tables(conn)
    stream.insert_observation(conn, observation)
    canonical = {**observation["receipt"], "transactionHash": observation["tx_hash"],
                 "blockHash": "0x" + "c" * 64, "blockNumber": hex(observation["block_number"]),
                 "logs": [{**log, "logIndex": hex(i), "transactionHash": observation["tx_hash"],
                           "blockHash": "0x" + "c" * 64}
                          for i, log in enumerate(observation["receipt"]["logs"])]}
    assert stream.reconcile_canonical(conn, observation["observation_key"], canonical,
                                      block_hash=canonical["blockHash"]) == "canonical_unsafe"
    assert stream.observation(conn, observation["observation_key"])["state"] == "canonical_unsafe"


def test_transaction_moved_to_another_block_retracts_old_observation(tmp_path):
    observation = _observation(2)
    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    stream.ensure_stream_tables(conn)
    stream.insert_observation(conn, observation)
    canonical = {**observation["receipt"], "transactionHash": observation["tx_hash"],
                 "blockHash": "0x" + "c" * 64,
                 "blockNumber": hex(observation["block_number"] + 1),
                 "logs": [{**log, "logIndex": hex(i), "transactionHash": observation["tx_hash"],
                           "blockHash": "0x" + "c" * 64}
                          for i, log in enumerate(observation["receipt"]["logs"])]}
    assert stream.reconcile_canonical(conn, observation["observation_key"], canonical,
                                      block_hash=canonical["blockHash"]) == "retracted"


def test_provisional_receipt_with_no_log_indexes_stays_provisional(tmp_path):
    from scripts.research.onchain_ink_watch import reconcile_once

    record = _observation(2)
    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    stream.ensure_stream_tables(conn)
    stream.insert_observation(conn, record)
    alleged = {**record["receipt"], "transactionHash": record["tx_hash"],
               "blockHash": "0x" + "c" * 64, "blockNumber": hex(record["block_number"])}
    assert stream.reconcile_canonical(conn, record["observation_key"], alleged,
                                      block_hash=alleged["blockHash"]) == "provisional"
    assert stream.observation(conn, record["observation_key"])["state"] == "provisional"

    def rpc(method, params):
        return alleged if method == "eth_getTransactionReceipt" else {"hash": alleged["blockHash"]}

    assert reconcile_once(conn, rpc) == 0


def test_zero_preconfirmation_hash_cannot_become_canonical(tmp_path):
    record = _observation()
    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    stream.ensure_stream_tables(conn)
    stream.insert_observation(conn, record)
    alleged = {**record["receipt"], "transactionHash": record["tx_hash"],
               "blockHash": "0x" + "0" * 64, "blockNumber": hex(record["block_number"])}
    assert stream.reconcile_canonical(conn, record["observation_key"], alleged,
                                      block_hash=alleged["blockHash"]) == "provisional"


def test_canonical_reconciler_never_finalizes_on_missing_receipt(tmp_path):
    from scripts.research.onchain_ink_watch import reconcile_once

    observation = _observation()
    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    stream.ensure_stream_tables(conn)
    stream.insert_observation(conn, observation)
    calls = []

    def rpc(method, params):
        calls.append(method)
        return None

    assert reconcile_once(conn, rpc) == 0
    assert calls == ["eth_getTransactionReceipt"]
    assert stream.observation(conn, observation["observation_key"])["state"] == "provisional"


def test_missing_old_receipt_does_not_starve_newer_observation(tmp_path):
    from scripts.research.onchain_ink_watch import reconcile_once

    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    stream.ensure_stream_tables(conn)
    for suffix in ("a", "b"):
        record = _observation()
        record["observation_key"] += suffix
        record["tx_hash"] = "0x" + suffix * 64
        stream.insert_observation(conn, record)
    requested = []
    def rpc(_method, params):
        requested.append(params[0])
        return None
    reconcile_once(conn, rpc, limit=1)
    reconcile_once(conn, rpc, limit=1)
    assert requested == ["0x" + "a" * 64, "0x" + "b" * 64]


def test_backfill_reorg_retracts_old_block_observation(tmp_path):
    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    stream.ensure_stream_tables(conn)
    record = _observation()
    old = "0x" + "c" * 64
    new = "0x" + "d" * 64
    receipt = {**record["receipt"], "transactionHash": record["tx_hash"],
               "blockHash": old, "blockNumber": hex(record["block_number"])}
    stream.insert_observation(conn, record)
    stream.reconcile_canonical(conn, record["observation_key"], receipt, block_hash=old)
    assert stream.record_canonical_block(conn, record["block_number"], old)
    assert stream.record_canonical_block(conn, record["block_number"], new)
    assert stream.observation(conn, record["observation_key"])["state"] == "retracted"


def test_reorg_retracts_previously_canonical_observation(tmp_path):
    from scripts.research.onchain_ink_watch import reconcile_once

    record = _observation()
    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    stream.ensure_stream_tables(conn)
    stream.insert_observation(conn, record)
    receipt = {**record["receipt"], "transactionHash": record["tx_hash"],
               "blockHash": "0x" + "c" * 64, "blockNumber": hex(record["block_number"])}
    block_hash = [receipt["blockHash"]]

    def rpc(method, params):
        return receipt if method == "eth_getTransactionReceipt" else {"hash": block_hash[0]}

    assert reconcile_once(conn, rpc) == 1
    assert stream.observation(conn, record["observation_key"])["state"] == "canonical_unsafe"
    block_hash[0] = "0x" + "d" * 64
    reconcile_once(conn, rpc)
    assert stream.observation(conn, record["observation_key"])["state"] == "retracted"


def test_canonical_observation_promotes_only_with_matching_safe_and_finalized_heads(tmp_path):
    from scripts.research.onchain_ink_watch import promote_once

    record = _observation()
    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    stream.ensure_stream_tables(conn)
    stream.insert_observation(conn, record)
    receipt = {**record["receipt"], "transactionHash": record["tx_hash"],
               "blockHash": "0x" + "c" * 64, "blockNumber": hex(record["block_number"])}
    stream.reconcile_canonical(conn, record["observation_key"], receipt,
                               block_hash=receipt["blockHash"])
    assert stream.promote_finality(conn, record["observation_key"], "safe",
                                   block_hash="0x" + "d" * 64) is False
    def rpc(method, params):
        tag = params[0]
        if tag in ("safe", "finalized"):
            return {"number": hex(record["block_number"]), "hash": receipt["blockHash"]}
        return {"number": hex(record["block_number"]), "hash": receipt["blockHash"]}

    assert promote_once(conn, rpc) == 1
    assert stream.observation(conn, record["observation_key"])["state"] == "finalized"


def test_recheck_does_not_demote_safe_receipt(tmp_path):
    from scripts.research.onchain_ink_watch import reconcile_once

    record = _observation()
    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    stream.ensure_stream_tables(conn)
    stream.insert_observation(conn, record)
    receipt = {**record["receipt"], "transactionHash": record["tx_hash"],
               "blockHash": "0x" + "c" * 64, "blockNumber": hex(record["block_number"])}
    stream.reconcile_canonical(conn, record["observation_key"], receipt,
                               block_hash=receipt["blockHash"])
    stream.promote_finality(conn, record["observation_key"], "safe",
                            block_hash=receipt["blockHash"])
    reconcile_once(conn, lambda _m, _p: {"hash": receipt["blockHash"]})
    assert stream.observation(conn, record["observation_key"])["state"] == "safe"


def test_overflow_gap_is_durable_and_not_healthy(tmp_path):
    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    stream.ensure_stream_tables(conn)
    stream.record_gap(conn, 123, "queue_overflow")
    conn.commit()
    assert stream.open_gaps(conn) == [{"block_number": 123, "reason": "queue_overflow"}]


def test_retraction_after_projection_emits_new_change(tmp_path):
    conn = sqlite3.connect(tmp_path / "stream.sqlite")
    stream.ensure_stream_tables(conn)
    record = _observation()
    stream.insert_observation(conn, record)
    first = stream.next_unprojected(conn)[0]
    stream.advance_cursor(conn, first["id"])
    stream.invalidate_payload(conn, record["payload_id"])
    changed = stream.next_unprojected(conn)
    assert len(changed) == 1
    assert changed[0]["id"] > first["id"]
    assert changed[0]["state"] == "retracted"


def test_bounded_receive_queue_reports_overflow():
    from scripts.research.onchain_ink_watch import enqueue_frame

    frames = queue.Queue(maxsize=1)
    assert enqueue_frame(frames, "first") is True
    assert enqueue_frame(frames, "second") is False
    assert frames.get_nowait() == "first"


def test_worker_commits_recorded_frame_before_recovery(tmp_path):
    from scripts.research.onchain_ink_watch import process_frames

    frames = json.loads(Path("docs/superpowers/research/2026-09-29-ink-evidence/subblock-sample.json").read_text())
    pending = queue.Queue()
    for index in range(3):
        pending.put((1, json.dumps(frames[index]), index, "t"))
    stop = threading.Event()
    stop.set()
    path = tmp_path / "stream.sqlite"
    process_frames(pending, stop, threading.Event(), path)
    with sqlite3.connect(path) as conn:
        assert len(stream.next_unprojected(conn)) == 3


def test_reconnect_records_durable_gap_until_canonical_backfill(tmp_path):
    from scripts.research.onchain_ink_watch import process_frames

    frames = json.loads(Path("docs/superpowers/research/2026-09-29-ink-evidence/subblock-sample.json").read_text())
    pending = queue.Queue()
    pending.put((1, json.dumps(frames[0]), 1, "t"))
    pending.put((2, json.dumps(frames[0]), 2, "t"))
    stop = threading.Event()
    stop.set()
    path = tmp_path / "stream.sqlite"
    process_frames(pending, stop, threading.Event(), path)
    with sqlite3.connect(path) as conn:
        assert any(gap["reason"] == "stream_restart" for gap in stream.open_gaps(conn))


def test_source_retraction_preserves_signed_funded_reservation(tmp_path, monkeypatch):
    from sniper.persistence import db_memecoin as funded
    from sniper.services.memecoin_evm_risk import reserve_entry_evm
    from sniper.settings import memecoin as cfg

    record = _observation()
    main_path = tmp_path / "funded.sqlite"
    with sqlite3.connect(main_path) as conn:
        conn.row_factory = sqlite3.Row
        funded.ensure_memecoin_tables(conn)
        monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
        decision, pending_id = reserve_entry_evm(
            conn, mint="0x" + "b" * 40, source_user_id="source", strategy="alpha_b",
            network_id="57073", day="2026-09-29", usdc_balance=300.0,
            native_balance=1.0, now_iso="2026-09-29T09:00:00Z",
            origin_event_id=record["observation_key"])
        assert decision.allowed
        funded.set_pending_broadcast(conn, pending_id, "0x" + "a" * 64, 5,
                                     "2026-09-29T09:00:01Z")
    with sqlite3.connect(tmp_path / "stream.sqlite") as conn:
        stream.ensure_stream_tables(conn)
        stream.insert_observation(conn, record)
        stream.invalidate_payload(conn, record["payload_id"])
        assert stream.observation(conn, record["observation_key"])["state"] == "retracted"
    with sqlite3.connect(main_path) as conn:
        conn.row_factory = sqlite3.Row
        pending = funded.pending_row(conn, pending_id)
        assert pending["status"] == "reserved"
        assert pending["tx_sig"] == "0x" + "a" * 64
        assert pending["nonce"] == 5
