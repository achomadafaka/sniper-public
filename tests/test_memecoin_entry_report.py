import sqlite3

import pytest

from scripts.report_memecoin_entry_baseline import build_entry_report


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE memecoin_entry_attempts (
            attempt_id TEXT PRIMARY KEY, origin_event_id TEXT, signal_source TEXT,
            network_id TEXT, mint TEXT, source_user_id TEXT, source_price REAL,
            decision_started_at TEXT, chain_resolution_ms REAL, market_data_ms REAL,
            reservation_ms REAL, safety_checks_ms REAL, quote_ms REAL, signing_ms REAL,
            transaction_submitted_ms REAL, chain_confirmation_ms REAL,
            balance_delta_ms REAL, persistence_ms REAL, total_ms REAL,
            transaction_submitted_at TEXT, chain_confirmation_completed_at TEXT,
            pending_id INTEGER, fill_id INTEGER, terminal_status TEXT, terminal_reason TEXT,
            price_attribution TEXT, quote_price REAL, fill_price REAL,
            source_drift_pct REAL, execution_slippage_pct REAL, total_copy_drift_pct REAL
        )
    """)
    conn.execute("""
        CREATE TABLE memecoin_events (
            fomo_event_id TEXT, signal_source TEXT, network_id TEXT, mint TEXT,
            side TEXT, action TEXT, received_at_normalized TEXT, received_at TEXT
        )
    """)
    return conn


def test_report_keeps_sources_chains_null_stages_and_drift_separate():
    conn = _conn()
    stages = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
    conn.execute(
        "INSERT INTO memecoin_entry_attempts VALUES (" + ",".join("?" for _ in range(31)) + ")",
        ("a1", "e1", "global_feed", "solana", "So1", "u1", 100.0,
         "2026-07-15T00:00:00Z", *stages, 55.0,
         "2026-07-15T00:00:01Z", "2026-07-15T00:00:02Z", 1, 9,
         "filled", "confirmed_fill", "exact_event", 110.0, 121.0,
         10.0, 10.0, 21.0),
    )
    conn.execute(
        "INSERT INTO memecoin_entry_attempts VALUES (" + ",".join("?" for _ in range(31)) + ")",
        ("a2", "e2", "follow_activity", "4663", "0x2", "u2", 2.0,
         "2026-07-15T00:00:00Z", 1, None, None, 4, None, None, None, None,
         None, None, 8.0, None, None, None, None, "refused", "token_safety: tax",
         None, None, None, None, None, None),
    )
    conn.execute(
        "INSERT INTO memecoin_entry_attempts VALUES (" + ",".join("?" for _ in range(31)) + ")",
        ("a3", "e3", "global_feed", "8453", "0x3", "u3", 3.0,
         "2026-07-15T00:00:00Z", 2, 3, 4, 5, 6, 7, 8, None, None, None,
         40.0, "2026-07-15T00:00:01Z", None, 2, None, "unknown", "timeout",
         "exact_event", 3.1, None, 3.333, None, None),
    )
    conn.executemany(
        "INSERT INTO memecoin_events VALUES (?,?,?,?,?,?,?,?)",
        [
            ("e1", "global_feed", "solana", "So1", "buy", "opened", "2026-07-15T00:00:00Z", None),
            ("e2", "follow_activity", "4663", "0x2", "buy", "opened", "2026-07-15T00:00:00Z", None),
            ("e4", "global_feed", "evm-unknown", "0x4", "buy", "candidate", "2026-07-15T00:00:00Z", None),
        ],
    )

    report = build_entry_report(conn)
    rows = {(r["network_id"], r["signal_source"]): r for r in report["rows"]}

    assert report["trace_available"] is True
    assert {(net, src) for net, src in rows if net in {"solana", "1", "56", "8453", "4663"}} == {
        (net, src) for net in ("solana", "1", "56", "8453", "4663")
        for src in ("global_feed", "follow_activity")
    }
    sol = rows[("solana", "global_feed")]
    assert sol["funnel"]["signals_observed"] == 1
    assert sol["funnel"]["confirmed_fills"] == 1
    assert sol["latency_ms"]["total"] == {"n": 1, "p50": 55.0, "p95": 55.0}
    assert sol["attribution"]["exact_drift_coverage_pct"] == 100.0
    assert sol["attribution"]["complete_timing_pct"] == 100.0
    follow = rows[("4663", "follow_activity")]
    assert follow["funnel"]["safety_failures"] == 1
    assert follow["latency_ms"]["quote"] == {"n": 0, "p50": None, "p95": None}
    unknown = rows[("8453", "global_feed")]
    assert unknown["funnel"]["unknown_timeouts"] == 1
    assert unknown["latency_ms"]["chain_confirmation"]["n"] == 0
    unresolved = rows[("evm-unknown", "global_feed")]
    assert unresolved["funnel"]["unresolved_network_signals"] == 1


def test_report_without_trace_table_labels_legacy_unknowns():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memecoin_events (signal_source TEXT, network_id TEXT, mint TEXT, side TEXT, action TEXT, received_at TEXT)")
    conn.execute("INSERT INTO memecoin_events VALUES ('global_feed','4663','0x1','buy','candidate','2026-07-15T00:00:00Z')")

    report = build_entry_report(conn)
    row = next(r for r in report["rows"] if r["network_id"] == "4663" and r["signal_source"] == "global_feed")
    assert report["trace_available"] is False
    assert row["funnel"]["signals_observed"] == 1
    assert row["funnel"]["reservations_accepted"] is None
    assert row["legacy_unknowns"]
    assert report["legacy_evidence"]["event_actions"] == [{
        "network_id": "4663", "signal_source": "global_feed", "actions": {"candidate": 1},
    }]


def test_report_counts_the_live_one_position_per_coin_refusal_as_duplicate_open():
    conn = _conn()
    conn.execute(
        "INSERT INTO memecoin_entry_attempts VALUES (" + ",".join("?" for _ in range(31)) + ")",
        ("a1", "e1", "global_feed", "solana", "So1", "u1", 1.0,
         "2026-07-15T00:00:00Z", 1.0, None, None, None, None, None, None, None,
         None, None, 2.0, None, None, None, None, "refused",
         "one position per coin: already held or reserved",
         None, None, None, None, None, None),
    )

    report = build_entry_report(conn)
    row = next(r for r in report["rows"]
               if r["network_id"] == "solana" and r["signal_source"] == "global_feed")

    assert row["funnel"]["duplicate_open_refusals"] == 1


def test_report_uses_point_in_time_global_feed_eligibility_and_exact_trace_join():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE memecoin_entry_attempts (
            origin_event_id TEXT, signal_source TEXT, network_id TEXT, mint TEXT,
            decision_started_at TEXT, transaction_submitted_at TEXT, pending_id INTEGER,
            fill_id INTEGER, terminal_status TEXT, terminal_reason TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE memecoin_events (
            fomo_event_id TEXT, signal_source TEXT, network_id TEXT, mint TEXT,
            side TEXT, action TEXT, received_at_normalized TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE memecoin_feed_observations_v2 (
            upstream_event_id TEXT, normalized_side TEXT, network_id TEXT, mint TEXT,
            observed_at TEXT, was_curated INTEGER, was_real_approved INTEGER,
            clock_domain TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO memecoin_feed_observations_v2 VALUES (?,?,?,?,?,?,?,?)",
        [
            ("eligible-traced", "buy", "solana", "So1", "2026-07-15T00:00:00Z", 1, 1,
             "observed_real_utc"),
            ("eligible-missed", "buy", "solana", "So2", "2026-07-15T00:00:01Z", 1, 1,
             "observed_real_utc"),
            ("curated-watch", "buy", "solana", "So3", "2026-07-15T00:00:02Z", 1, 0,
             "observed_real_utc"),
            ("eligible-resolved", "buy", "evm-unknown", "0x4",
             "2026-07-15T00:00:03Z", 1, 1, "observed_real_utc"),
        ],
    )
    conn.executemany(
        "INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("eligible-traced", "global_feed", "solana", "So1", "2026-07-15T00:00:00Z",
             None, None, None, "refused", "route refusal"),
            ("eligible-resolved", "global_feed", "8453", "0x4",
             "2026-07-15T00:00:03Z", None, None, None, "refused", "not_approved"),
        ],
    )
    conn.execute(
        "INSERT INTO memecoin_events VALUES (?,?,?,?,?,?,?)",
        ("follow-1", "follow_activity", "4663", "0x1", "buy", "opened",
         "2026-07-15T00:00:03Z"),
    )

    report = build_entry_report(conn)
    rows = {(r["network_id"], r["signal_source"]): r for r in report["rows"]}

    sol = rows[("solana", "global_feed")]
    assert sol["funnel"]["signals_observed"] == 3
    assert sol["funnel"]["curated_buys"] == 3
    assert sol["funnel"]["eligible_curated_buys"] == 2
    assert sol["funnel"]["eligible_buys_with_exact_trace"] == 1
    assert sol["funnel"]["eligible_trace_coverage_pct"] == 50.0
    assert "eligible_curated_buys_not_preserved_point_in_time" not in sol["legacy_unknowns"]

    resolved = rows[("evm-unknown", "global_feed")]
    assert resolved["funnel"]["eligible_curated_buys"] == 1
    assert resolved["funnel"]["eligible_buys_with_exact_trace"] == 1
    assert resolved["funnel"]["eligible_trace_coverage_pct"] == 100.0

    follow = rows[("4663", "follow_activity")]
    assert follow["funnel"]["signals_observed"] == 1
    assert follow["funnel"]["curated_buys"] is None
    assert follow["funnel"]["eligible_curated_buys"] is None
    assert follow["funnel"]["eligible_buys_with_exact_trace"] is None
    assert follow["funnel"]["eligible_trace_coverage_pct"] is None
    assert "point_in_time_eligibility_unavailable_for_follow_activity" in follow["legacy_unknowns"]


def test_report_derives_only_same_clock_nonnegative_receipt_latency():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE memecoin_entry_attempts (
            origin_event_id TEXT, signal_source TEXT, network_id TEXT, mint TEXT,
            observed_at TEXT, clock_domain TEXT, decision_started_at TEXT,
            transaction_submitted_at TEXT, completed_at TEXT,
            terminal_status TEXT, terminal_reason TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("valid", "global_feed", "solana", "So1", "2026-07-15T00:00:00Z",
             "observed_real_utc", "2026-07-15T00:00:00.250000Z",
             "2026-07-15T00:00:01Z", "2026-07-15T00:00:02Z", "filled", "fill"),
            ("domain", "global_feed", "4663", "0x1", "2026-07-15T00:00:00Z",
             "legacy_unknown", "2026-07-15T00:00:00.250000Z",
             "2026-07-15T00:00:01Z", "2026-07-15T00:00:02Z", "refused", "x"),
            ("naive", "global_feed", "8453", "0x2", "2026-07-15T00:00:00",
             "observed_real_utc", "2026-07-15T00:00:00.250000Z",
             "2026-07-15T00:00:01Z", "2026-07-15T00:00:02Z", "refused", "x"),
            ("negative", "global_feed", "56", "0x3", "2026-07-15T00:00:01Z",
             "observed_real_utc", "2026-07-15T00:00:00Z",
             "2026-07-15T00:00:00Z", "2026-07-15T00:00:00Z", "refused", "x"),
            ("unreached", "global_feed", "1", "0x4", "2026-07-15T00:00:00Z",
             "observed_real_utc", "2026-07-15T00:00:00.500000Z",
             None, "2026-07-15T00:00:01Z", "refused", "x"),
        ],
    )

    report = build_entry_report(conn)
    rows = {(r["network_id"], r["signal_source"]): r for r in report["rows"]}

    sol = rows[("solana", "global_feed")]["receipt_latency_ms"]
    assert sol["receipt_to_decision"] == {
        "n": 1, "p50": 250.0, "p95": 250.0, "excluded_n": 0, "exclusion_reasons": {},
    }
    assert sol["receipt_to_submission"]["p50"] == 1000.0
    assert sol["receipt_to_terminal"]["p50"] == 2000.0
    assert sol["clock_valid_coverage_pct"] == 100.0

    assert rows[("4663", "global_feed")]["receipt_latency_ms"]["receipt_to_decision"][
        "exclusion_reasons"] == {"invalid_clock_domain": 1}
    assert rows[("8453", "global_feed")]["receipt_latency_ms"]["receipt_to_decision"][
        "exclusion_reasons"] == {"naive_timestamp": 1}
    assert rows[("56", "global_feed")]["receipt_latency_ms"]["receipt_to_decision"][
        "exclusion_reasons"] == {"negative_delta": 1}
    assert rows[("1", "global_feed")]["receipt_latency_ms"]["receipt_to_submission"][
        "exclusion_reasons"] == {"stage_not_reached": 1}
