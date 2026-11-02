import sqlite3
import json

from sniper.persistence import db_evm_stream as journal
from sniper.persistence import db_memecoin as funded
from sniper.services.memecoin_onchain_lane import source_time
from sniper.services.memecoin_evm_onchain_watch import TRANSFER_TOPIC


def test_dark_polled_buy_keeps_source_clock():
    row = {"source_valuation_json": '{"backfill":true}', "block_time": 1790671000,
           "detected_at": "2026-09-29T08:37:25+00:00"}
    assert source_time(row) == "2026-09-29T08:36:40+00:00"


def test_gap_closes_only_after_each_canonical_block_is_checked(tmp_path):
    with sqlite3.connect(tmp_path / "journal.db") as conn:
        journal.ensure_stream_tables(conn)
        journal.record_gap(conn, 100, "queue_overflow", end_block_number=102)
        journal.record_canonical_block(conn, 100, "0x" + "11" * 32)
        journal.record_canonical_block(conn, 102, "0x" + "33" * 32)
        assert journal.resolve_covered_gaps(conn) == 0
        assert journal.open_gaps(conn)
        journal.record_canonical_block(conn, 101, "0x" + "22" * 32)
        assert journal.resolve_covered_gaps(conn) == 1
        assert journal.open_gaps(conn) == []


def test_zero_hash_never_marks_canonical_coverage(tmp_path):
    with sqlite3.connect(tmp_path / "journal.db") as conn:
        journal.ensure_stream_tables(conn)
        journal.record_gap(conn, 100, "stream_restart", end_block_number=100)
        assert journal.record_canonical_block(conn, 100, "0x" + "00" * 32) is False
        assert journal.resolve_covered_gaps(conn) == 0


def test_canonical_buy_projects_once_with_exact_amount_and_source_age(tmp_path):
    from scripts.research.onchain_ink_watch import project_canonical_once
    HASH, WALLET, POOL_A, TOKEN = ("0x" + part * size for part, size in
                                   (("aa", 32), ("11", 20), ("22", 20), ("44", 20)))
    WETH = "0x4200000000000000000000000000000000000006"
    def transfer(token, sender, recipient, amount, index):
        return {"address": token, "topics": [TRANSFER_TOPIC,
                "0x" + "0" * 24 + sender[2:], "0x" + "0" * 24 + recipient[2:]],
                "data": hex(amount), "logIndex": hex(index), "transactionHash": HASH}

    block_hash = "0x" + "bb" * 32
    logs = [transfer(WETH, WALLET, POOL_A, 10**15, 0),
            transfer(TOKEN, POOL_A, WALLET, 2 * 10**18, 1)]
    for log in logs:
        log["blockHash"] = block_hash
    receipt = {"transactionHash": HASH, "blockHash": block_hash,
               "blockNumber": "0x123", "status": "0x1", "logs": logs}
    record = {"observation_key": f"57073:payload:0:{HASH}", "payload_id": "payload",
              "block_number": 291, "tx_hash": HASH, "owner": WALLET,
              "state": "provisional", "source_timestamp": 1790671000,
              "receipt": {**receipt, "logs": [{k: v for k, v in log.items()
                                              if k not in ("logIndex", "blockHash")}
                                             for log in logs]}}
    with sqlite3.connect(tmp_path / "journal.db") as stream, sqlite3.connect(tmp_path / "main.db") as main:
        main.row_factory = sqlite3.Row
        journal.ensure_stream_tables(stream)
        funded.ensure_memecoin_tables(main)
        journal.insert_observation(stream, record)
        journal.reconcile_canonical(stream, record["observation_key"], receipt, block_hash=block_hash)
        journal.record_canonical_block(stream, 291, block_hash)
        block = {"number": "0x123", "hash": block_hash, "timestamp": hex(1790671000)}
        tx = {"hash": HASH, "from": WALLET, "blockHash": block_hash,
              "blockNumber": "0x123"}
        def rpc(method, params):
            return tx if method == "eth_getTransactionByHash" else block
        watched = {WALLET: ("source", "handle")}
        assert project_canonical_once(stream, main, rpc, watched=watched,
                                      quote_usd=2500.0, decimals_fn=lambda _mint: None,
                                      detected_at="2026-09-29T08:37:25+00:00") == 0
        assert journal.next_unprojected(stream)
        assert project_canonical_once(stream, main, rpc, watched=watched,
                                      quote_usd=2500.0, decimals_fn=lambda _mint: 18,
                                      detected_at="2026-09-29T08:37:25+00:00") == 1
        assert project_canonical_once(stream, main, rpc, watched=watched,
                                      quote_usd=2500.0, decimals_fn=lambda _mint: 18,
                                      detected_at="2026-09-29T08:37:25+00:00") == 0
        row = main.execute("SELECT * FROM onchain_wallet_detections").fetchone()
        assert row["token_amount_atomic"] == str(2 * 10**18)
        assert row["quote_amount_atomic"] == str(10**15)
        assert row["block_time"] == 1790671000
        assert json.loads(row["source_valuation_json"])["backfill"] is True
        assert source_time(row) == "2026-09-29T08:36:40+00:00"
        journal.record_canonical_block(stream, 291, "0x" + "dd" * 32)
        assert project_canonical_once(stream, main, rpc, watched=watched,
                                      quote_usd=2500.0, decimals_fn=lambda _mint: 18,
                                      detected_at="2026-09-29T08:37:26+00:00") == 0
        assert main.execute("SELECT source_observation_state FROM onchain_wallet_detections"
                            ).fetchone()[0] == "retracted"


def test_backfill_does_not_advance_on_partial_receipts(tmp_path):
    from scripts.research.onchain_ink_watch import backfill_block
    wallet = "0x" + "11" * 20
    h1, h2 = "0x" + "aa" * 32, "0x" + "bb" * 32
    block_hash = "0x" + "cc" * 32
    block = {"number": "0x64", "hash": block_hash, "timestamp": "0x6ad00f00",
             "transactions": [{"hash": h1, "from": wallet, "blockHash": block_hash,
                               "blockNumber": "0x64"},
                              {"hash": h2, "from": wallet, "blockHash": block_hash,
                               "blockNumber": "0x64"}]}
    receipts = {h1: {"transactionHash": h1, "blockHash": block_hash,
                     "blockNumber": "0x64", "status": "0x1", "logs": []}}
    def rpc(method, params):
        return block if method == "eth_getBlockByNumber" else receipts.get(params[0])
    with sqlite3.connect(tmp_path / "journal.db") as stream:
        journal.ensure_stream_tables(stream)
        journal.record_gap(stream, 100, "stream_restart", end_block_number=100)
        assert backfill_block(stream, rpc, 100, watched={wallet}) is False
        assert journal.resolve_covered_gaps(stream) == 0
        assert journal.next_unprojected(stream) == []
        receipts[h2] = {**receipts[h1], "transactionHash": h2}
        assert backfill_block(stream, rpc, 100, watched={wallet}) is True
        assert journal.open_gaps(stream) == []
        assert len(journal.next_unprojected(stream)) == 2


def test_backfill_frontier_never_skips_failed_block(tmp_path):
    with sqlite3.connect(tmp_path / "journal.db") as stream:
        journal.ensure_stream_tables(stream)
        assert journal.start_backfill(stream, 100) == 100
        assert journal.next_backfill_block(stream) == 100
        assert journal.advance_backfill(stream, 101) is False
        assert journal.next_backfill_block(stream) == 100
        journal.record_canonical_block(stream, 100, "0x" + "11" * 32)
        assert journal.advance_backfill(stream, 100) is True
        assert journal.next_backfill_block(stream) == 101
        assert journal.start_backfill(stream, 999) == 101


def test_retracted_ink_detection_cannot_enter_after_journal_update(tmp_path):
    from sniper.services.memecoin_onchain_lane import sweep_onchain_detections
    with sqlite3.connect(tmp_path / "main.db") as main:
        main.row_factory = sqlite3.Row
        funded.ensure_memecoin_tables(main)
        funded.record_onchain_detection(
            main, signature="57073:tx:owner:mint:buy", source_user_id="source",
            handle="handle", address="0x" + "11" * 20, mint="0x" + "44" * 20,
            detected_at="2026-09-29T08:37:25+00:00", created_at="2026-09-29T08:37:25+00:00",
            network_id="57073", source_size_usd=5, source_price=2.5,
            block_time=1790671000, source_valuation_json='{"backfill":true}',
            source_observation_key="key", source_observation_state="retracted")
        called = []
        cursor, _ = sweep_onchain_detections(
            main, lambda *_args, **_kwargs: called.append(True), cursor=0,
            now_fn=lambda: "2026-09-29T08:37:25+00:00", max_age_s=120,
            approved_ids_fn=lambda _conn, _chain: {"source"})
        assert cursor == 1
        assert called == []
