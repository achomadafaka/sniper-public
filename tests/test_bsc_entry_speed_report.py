import json
import sqlite3

from scripts.research.bsc_entry_speed_report import evaluate, read_cohort, _witness_ms


def row(i, *, elapsed_ms=150, eligible=True, outcome="filled", stratum="native_warm"):
    tx = "0x" + f"{i + 1:064x}"
    copy = "0x" + f"{i + 1001:064x}"
    return {"source_tx": tx, "wallet": "0x" + "11" * 20,
            "mint": "0x" + "22" * 20, "eligible": eligible, "outcome": outcome,
            "stratum": stratum, "receipt_clock": {"boot_id": "boot", "monotonic_ns": 1000000},
            "observed_boot_id": "boot", "observed_ns": 1000000 + int(elapsed_ms * 1000000),
            "copy_tx": copy, "observed_tx": copy, "confirmed_tx": copy, "booked_tx": copy,
            "observed_block": 20, "confirmed_block": 20,
            "observed_hash": "0x" + "aa" * 32, "confirmed_hash": "0x" + "aa" * 32,
            "booked_block": 20, "booked_hash": "0x" + "aa" * 32,
            "observed_status": 1, "confirmed_status": 1, "booked_status": 1,
            "dispatch_ms": 15, "dispatch_boundary": "socket_write", "ack_ms": 30}


def test_fast_fills_do_not_hide_failures_in_all_eligible_denominator():
    rows = [row(i) for i in range(30)]
    rows += [row(i, outcome="reverted") for i in range(30, 100)]
    result = evaluate(rows, universe_complete=True)
    assert result["eligible"] == 100
    assert result["verified_sub200_fraction"] == .3
    assert result["strata"]["native_warm"]["fill_ms"]["p90"] == 150
    assert not result["goal_proved"]


def test_complete_organic_cohort_can_meet_declared_empirical_goal():
    result = evaluate([row(i) for i in range(100)], universe_complete=True)
    assert result["goal_proved"]
    assert result["confirmed_witness_coverage"] == 1


def test_source_duplicates_do_not_inflate_count():
    rows = [row(i) for i in range(50)] * 2
    result = evaluate(rows, universe_complete=True)
    assert result["eligible"] == 50
    assert result["duplicates"] == 50
    assert "eligible_below_100" in result["blockers"]


def test_missing_clock_cross_boot_and_forked_receipt_are_not_successes():
    rows = [row(i) for i in range(100)]
    rows[0]["receipt_clock"] = None
    rows[1]["observed_boot_id"] = "old_boot"
    rows[2]["confirmed_hash"] = "0x" + "bb" * 32
    rows[3]["booked_tx"] = "0x" + "cc" * 32
    rows[4]["observed_ns"] = 0
    rows[5]["observed_tx"] = "0x" + "dd" * 32
    result = evaluate(rows, universe_complete=True)
    assert result["verified_witnesses"] == 94
    assert result["confirmed_witness_coverage"] == .94
    assert not result["goal_proved"]


def test_unknown_eligibility_overlap_and_publication_loss_block_verdict():
    rows = [row(i) for i in range(100)]
    rows += [row(101, eligible=None), row(102, eligible=False)]
    rows[0].pop("source_tx")
    rows[0]["upstream_id"] = "j7-opaque"
    result = evaluate(rows, universe_complete=True, publication_losses=1)
    assert result["eligible"] == 100
    assert result["unknown_eligibility"] == 1
    assert result["policy_refusals"] == 1
    assert {"unknown_eligibility", "cross_feed_overlap_unknown", "publication_loss"} <= set(result["blockers"])
    assert not result["goal_proved"]


def test_small_stratum_and_incomplete_universe_remain_unproved():
    rows = [row(i, stratum="native_warm" if i < 90 else "shared") for i in range(100)]
    result = evaluate(rows)
    assert "universe_incomplete" in result["blockers"]
    assert "witnesses_below_30:shared" in result["blockers"]
    assert not result["goal_proved"]


def test_native_dispatch_target_uses_original_receipt_even_when_fill_is_fast():
    rows = [row(i) for i in range(100)]
    for item in rows:
        item["dispatch_ms"] = 60
    result = evaluate(rows, universe_complete=True)
    assert "native_dispatch_target_unmet" in result["blockers"]
    assert not result["goal_proved"]


def test_sender_handoff_cannot_prove_physical_dispatch_target():
    rows = [dict(row(i), dispatch_boundary="sender_call_after_durable_binding") for i in range(100)]
    result = evaluate(rows, universe_complete=True)
    assert "native_dispatch_target_unmet" in result["blockers"]
    assert result["strata"]["native_warm"]["dispatch_ms"]["n"] == 0
    assert result["strata"]["native_warm"]["sender_dispatch_ms"]["n"] == 100


def test_unknown_route_readiness_cannot_bypass_prepared_dispatch_target():
    rows = [dict(row(i, stratum="native_unknown"),
                 dispatch_boundary="sender_call_after_durable_binding") for i in range(100)]
    result = evaluate(rows, universe_complete=True)
    assert "unknown_route_readiness" in result["blockers"]
    assert not result["goal_proved"]


def test_block_gap_and_received_failures_remain_visible_outside_eligible_subset():
    rows = [dict(row(1), source_block=18, landing_block=20, reason='filled', venue='v2', source_feed='watcher'),
            dict(row(2, eligible=None, outcome='aborted'), reason='float_short', source_feed='j7')]
    result = evaluate(rows)
    assert result['strata']['native_warm']['blocks_behind'] == {'n': 1, 'p50': 2, 'p90': 2}
    assert result['received_reasons'] == {'filled': 1, 'float_short': 1}
    assert result['unknown_eligibility_reasons'] == {'float_short': 1}
    assert result['source_feeds'] == {'watcher': 1, 'j7': 1}


def test_booked_block_and_all_receipt_statuses_are_authoritative():
    for field, wrong in (("booked_hash", "0x" + "bb" * 32), ("booked_block", 21),
                         ("observed_status", 0), ("confirmed_status", 0), ("booked_status", None)):
        rows = [row(i) for i in range(100)]
        for item in rows:
            item[field] = wrong
        result = evaluate(rows, universe_complete=True)
        assert result["verified_witnesses"] == 0, field
        assert not result["goal_proved"], field


def test_duplicate_contradictory_witness_cannot_leave_optimistic_first_row():
    rows = [row(i) for i in range(100)]
    duplicate = dict(rows[0], confirmed_hash="0x" + "bb" * 32)
    result = evaluate(rows + [duplicate], universe_complete=True)
    assert "duplicate_conflict" in result["blockers"]
    assert not result["goal_proved"]


def test_live_reader_retains_no_attempt_and_requires_actual_booked_fill():
    from sniper.services.memecoin_fill_timing import SCHEMA
    ledger, timing = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    ledger.row_factory = timing.row_factory = sqlite3.Row
    ledger.executescript("CREATE TABLE memecoin_entry_attempts(attempt_id TEXT PRIMARY KEY, "
        "origin_event_id TEXT,network_id TEXT,source_user_id TEXT,mint TEXT,decision_started_at TEXT,"
        "terminal_status TEXT,tx_sig TEXT,trace_json TEXT);"
        "CREATE INDEX idx_mc_entry_attempt_origin ON memecoin_entry_attempts(origin_event_id);"
        "CREATE TABLE memecoin_fills(id INTEGER,position_id INTEGER,price REAL,qty REAL,notional REAL,"
        "tx_sig TEXT,attempt_id TEXT,kind TEXT);")
    timing.executescript(SCHEMA)
    release, generation = "a" * 40, "a" * 40 + ":1:boot:producer"
    datum = row(1)
    signal_id = "bscfast:56:" + datum["source_tx"] + ":" + datum["mint"]
    signal = {"kind": "signal", "signal_id": signal_id, "source_user_id": datum["wallet"],
              "mint": datum["mint"], "receipt_clock": datum["receipt_clock"], "eligibility": True,
              "generation": generation}
    for sequence, item in enumerate((signal, dict(signal, signal_id="unattempted", eligibility=None)), 1):
        timing.execute("INSERT INTO memecoin_execution_observer_events VALUES (?,?,?,?)",
                       (generation, sequence, "signal", json.dumps(item)))
    ledger.execute("INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?,?,?,?,?,?)",
        ("attempt", signal_id, "56", datum["wallet"], datum["mint"], "now", "filled",
         datum["copy_tx"], json.dumps({"submission_events": [
             {"stage": "transaction_submitted", "receipt_elapsed_ms": 30}]})))
    receipt = {"transactionHash": datum["copy_tx"], "blockHash": datum["booked_hash"],
               "blockNumber": "0x14", "status": "0x1", "logs": []}
    success = {"tx_hash": datum["copy_tx"], "observed_ns": datum["observed_ns"], "boot_id": "boot",
               "block_number": 20, "block_hash": datum["observed_hash"]}
    timing.execute("INSERT INTO memecoin_execution_observations VALUES (?,?,?,?,?,?,?,?)",
        (generation, "attempt", datum["copy_tx"], "{}", json.dumps(success),
         json.dumps({"receipt": receipt}), "observed", "verified"))
    document = read_cohort(ledger, timing, release)
    assert len(document["rows"]) == 2
    assert document["rows"][0]["source_tx"] == datum["source_tx"]
    assert document["rows"][0]["ack_ms"] == 30
    assert _witness_ms(document["rows"][0]) is None
    ledger.execute("INSERT INTO memecoin_fills VALUES (1,1,1,1,1,?,'attempt','entry')", (datum["copy_tx"],))
    document = read_cohort(ledger, timing, release)
    assert _witness_ms(document["rows"][0]) == 150
    assert document["rows"][1]["eligible"] is None
    assert not document["metadata"]["universe_complete"]
    ledger.close()
    timing.close()
