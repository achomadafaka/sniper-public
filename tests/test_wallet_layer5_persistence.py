"""Schema tests for wallet_layer5 persistence helpers.

Phase 3 Workstream A, Task A1 — locks the SQL contract for:
- wallet_layer5_decision (per Phase 2 spec § 3.1)
- position_signal_decision (per Phase 2 spec § 3.2)
- analyzer_runs (per Phase 2 spec § 3.3)
- tag_market_stats (per Phase 3 plan § 0.2)
- position_market_state (per Phase 3 plan § A7 + Workstream B trace)
"""
from __future__ import annotations

import sqlite3

import pytest

from sniper.persistence.db_layer5 import (
    ensure_analyzer_runs,
    ensure_position_market_state,
    ensure_position_signal_decision,
    ensure_tag_market_stats,
    ensure_wallet_analysis_batch,
    ensure_wallet_analyzer_intake_queue,
    ensure_wallet_layer5_decision,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_ensure_wallet_layer5_decision_creates_table(conn):
    ensure_wallet_layer5_decision(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(wallet_layer5_decision)")}
    expected = {
        "wallet", "analysis_version", "analyzer_run_id",
        "as_of_ts", "data_window_start_ts", "data_window_end_ts",
        "gate_result", "trading_mode", "overall_verdict",
        "primary_edge_type", "flip_rate",
        "n_closed_positions", "n_held_to_resolution",
        "n_open_positions", "n_open_dead",
        "net_pnl_usdc", "dw_roi_point", "dw_roi_ci_lower",
        "dw_roi_ci_upper", "ew_roi_point",
        "activity_cap_hit", "archetype_layer1",
        "action_decision", "action_reason_codes_json",
        "confidence_band", "is_current",
        "held_branch_summary_json", "flipper_branch_summary_json",
        "tag_grain_summary_json", "capital_ledger_summary_json",
        "data_quality_flags_json", "applicable_limitations_json",
        "open_questions_json", "copyability_note",
        "report_markdown", "created_at", "updated_at",
    }
    assert expected.issubset(cols), f"missing columns: {expected - cols}"


def test_ensure_position_signal_decision_creates_table(conn):
    ensure_position_signal_decision(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(position_signal_decision)")}
    expected = {
        "wallet", "condition_id", "outcome", "analysis_version",
        "wallet_action_decision", "primary_tag",
        "tag_fit_score", "tag_fit_rank", "tag_fit_peer_count",
        "tag_fit_confidence_flag", "signal_reason_codes_json",
        "analyzed_ts", "created_at",
    }
    assert expected.issubset(cols), f"missing columns: {expected - cols}"


def test_ensure_analyzer_runs_creates_table(conn):
    ensure_analyzer_runs(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(analyzer_runs)")}
    expected = {
        "run_id", "analysis_version", "wallet",
        "started_ts", "finished_ts", "duration_ms",
        "success", "error_class", "error_message",
        "invocation_source", "args_json",
        "fetch_quality_flags_json", "activity_rows_fetched",
        "activity_cap_hit", "subgraph_fallback_used",
        "created_at",
    }
    assert expected.issubset(cols), f"missing columns: {expected - cols}"


def test_ensure_tag_market_stats_creates_table(conn):
    ensure_tag_market_stats(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tag_market_stats)")}
    expected = {
        "tag", "n_markets", "n_resolved", "resolved_yes_count",
        "yes_share", "avg_resolved_price_yes",
        "median_market_volume_24h", "top_co_tags_json",
        "computed_at", "inputs_version",
    }
    assert expected.issubset(cols), f"missing columns: {expected - cols}"


def test_ensure_wallet_analysis_batch_creates_tables(conn):
    ensure_analyzer_runs(conn)
    ensure_wallet_analysis_batch(conn)

    batch_cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(wallet_analysis_batch)")
    }
    assert {
        "batch_id", "source", "status", "total_wallets",
        "created_ts", "started_ts", "finished_ts", "created_at",
        "updated_at",
    }.issubset(batch_cols)

    item_cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(wallet_analysis_batch_item)")
    }
    expected_item_cols = {
        "batch_id", "wallet", "status",
        "started_ts", "heartbeat_ts", "finished_ts", "duration_ms",
        "analyzer_run_id",
        "action_bucket", "action_decision", "confidence_band",
        "overall_verdict", "primary_edge_type",
        "summary_markdown", "summary_json",
        "error_class", "error_message", "attempt_count",
        "source", "source_ref", "metadata_json", "intake_queue_id",
        "created_at", "updated_at",
    }
    assert expected_item_cols.issubset(item_cols), (
        f"missing columns: {expected_item_cols - item_cols}"
    )


def test_ensure_wallet_analyzer_intake_queue_creates_table(conn):
    ensure_wallet_analyzer_intake_queue(conn)
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(wallet_analyzer_intake_queue)")
    }
    expected = {
        "id", "wallet", "source", "source_ref", "status", "priority",
        "created_ts", "claimed_ts", "started_ts", "finished_ts",
        "last_attempt_ts", "attempt_count", "metadata_json", "notes",
        "error_class", "error_message", "created_at", "updated_at",
    }
    assert expected.issubset(cols), f"missing columns: {expected - cols}"


def test_idempotent_ensure_wallet_layer5_decision(conn):
    ensure_wallet_layer5_decision(conn)
    ensure_wallet_layer5_decision(conn)


def test_idempotent_ensure_position_signal_decision(conn):
    ensure_position_signal_decision(conn)
    ensure_position_signal_decision(conn)


def test_idempotent_ensure_analyzer_runs(conn):
    ensure_analyzer_runs(conn)
    ensure_analyzer_runs(conn)


def test_idempotent_ensure_tag_market_stats(conn):
    ensure_tag_market_stats(conn)
    ensure_tag_market_stats(conn)


def test_idempotent_ensure_wallet_analysis_batch(conn):
    ensure_analyzer_runs(conn)
    ensure_wallet_analysis_batch(conn)
    ensure_wallet_analysis_batch(conn)


def test_idempotent_ensure_wallet_analyzer_intake_queue(conn):
    ensure_wallet_analyzer_intake_queue(conn)
    ensure_wallet_analyzer_intake_queue(conn)


def test_wallet_layer5_decision_indexes_created(conn):
    ensure_wallet_layer5_decision(conn)
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='wallet_layer5_decision'"
    )}
    assert "idx_wld_action_current" in idx
    assert "idx_wld_wallet_current" in idx
    assert "idx_wld_run" in idx


def test_position_signal_decision_indexes_created(conn):
    ensure_position_signal_decision(conn)
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='position_signal_decision'"
    )}
    assert "idx_psd_action" in idx
    assert "idx_psd_wallet" in idx


def test_analyzer_runs_indexes_created(conn):
    ensure_analyzer_runs(conn)
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='analyzer_runs'"
    )}
    assert "idx_ar_wallet_started" in idx
    assert "idx_ar_success_started" in idx


def test_tag_market_stats_indexes_created(conn):
    ensure_tag_market_stats(conn)
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='tag_market_stats'"
    )}
    assert "idx_tms_computed_at" in idx


def test_wallet_layer5_decision_primary_key(conn):
    ensure_wallet_layer5_decision(conn)
    pk_cols = [
        r["name"]
        for r in conn.execute("PRAGMA table_info(wallet_layer5_decision)")
        if r["pk"] > 0
    ]
    assert set(pk_cols) == {"wallet", "analysis_version"}


def test_position_signal_decision_primary_key(conn):
    ensure_position_signal_decision(conn)
    pk_cols = sorted(
        (r["pk"], r["name"])
        for r in conn.execute("PRAGMA table_info(position_signal_decision)")
        if r["pk"] > 0
    )
    assert [n for _, n in pk_cols] == [
        "wallet", "condition_id", "outcome", "analysis_version",
    ]


def test_analyzer_runs_primary_key(conn):
    ensure_analyzer_runs(conn)
    pk_cols = [
        r["name"]
        for r in conn.execute("PRAGMA table_info(analyzer_runs)")
        if r["pk"] > 0
    ]
    assert pk_cols == ["run_id"]


def test_tag_market_stats_primary_key(conn):
    ensure_tag_market_stats(conn)
    pk_cols = [
        r["name"]
        for r in conn.execute("PRAGMA table_info(tag_market_stats)")
        if r["pk"] > 0
    ]
    assert pk_cols == ["tag"]


def test_wallet_analysis_batch_item_primary_key(conn):
    ensure_analyzer_runs(conn)
    ensure_wallet_analysis_batch(conn)
    pk_cols = sorted(
        (r["pk"], r["name"])
        for r in conn.execute("PRAGMA table_info(wallet_analysis_batch_item)")
        if r["pk"] > 0
    )
    assert [n for _, n in pk_cols] == ["batch_id", "wallet"]


def test_wallet_analysis_batch_constraints_and_fks(conn):
    ensure_analyzer_runs(conn)
    ensure_wallet_analysis_batch(conn)
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='wallet_analysis_batch_item'"
    ).fetchone()[0]
    for status in ("queued", "running", "completed", "failed", "skipped"):
        assert status in ddl
    for bucket in (
        "actionable_now", "review", "low_confidence", "no_action", "failed",
    ):
        assert bucket in ddl

    fks = [
        dict(r)
        for r in conn.execute(
            "PRAGMA foreign_key_list(wallet_analysis_batch_item)"
        )
    ]
    assert any(
        fk["from"] == "batch_id"
        and fk["table"] == "wallet_analysis_batch"
        and fk["to"] == "batch_id"
        for fk in fks
    )
    assert any(
        fk["from"] == "analyzer_run_id"
        and fk["table"] == "analyzer_runs"
        and fk["to"] == "run_id"
        for fk in fks
    )


def test_wallet_analyzer_intake_queue_constraints_and_indexes(conn):
    ensure_wallet_analyzer_intake_queue(conn)
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='wallet_analyzer_intake_queue'"
    ).fetchone()[0]
    for status in (
        "pending", "claimed", "running", "completed",
        "failed", "skipped", "duplicate",
    ):
        assert status in ddl
    for source in (
        "manual", "discovery_agent", "import_file", "api", "other",
    ):
        assert source in ddl
    assert "low_buys_legacy" not in ddl

    idx = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='wallet_analyzer_intake_queue'"
        )
    }
    assert "idx_waiq_status_priority" in idx
    assert "idx_waiq_wallet_status" in idx


def test_init_db_creates_layer5_tables(tmp_path):
    """Integration: init_db wires all layer5 ensure_* helpers."""
    from db import init_db

    db_path = tmp_path / "layer5.db"
    c = sqlite3.connect(str(db_path))
    try:
        init_db(c)
        tables = {
            r[0]
            for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        expected = {
            "wallet_layer5_decision",
            "position_signal_decision",
            "analyzer_runs",
            "tag_market_stats",
            "position_market_state",
            "wallet_analysis_batch",
            "wallet_analysis_batch_item",
            "wallet_analyzer_intake_queue",
        }
        assert expected.issubset(tables), f"missing tables: {expected - tables}"
    finally:
        c.close()


# =============================================================================
# Task A7: position_market_state schema (per Workstream B trace)
# =============================================================================

# Locked KEEP set per docs/research/scopes/2026-04-29-position-signals-column-validation.md
POSITION_MARKET_STATE_KEEP_COLUMNS: frozenset[str] = frozenset({
    # Identity
    "wallet", "condition_id", "outcome",
    # Market descriptors
    "title", "event_slug", "end_date_iso",
    # Open / current
    "is_open", "position_value", "position_share", "position_count_wallet",
    "current_price", "current_size",
    # Entry
    "entry_anchor_ts", "entry_price", "entry_size", "entry_notional",
    # Trade-window counts
    "buys_5m", "buys_1h", "buys_24h",
    "trades_5m", "trades_1h", "trades_24h",
    "sells_5m", "sells_1h", "sells_24h",
    # Notionals
    "notional_5m", "notional_1h", "notional_24h",
    "sell_notional_5m", "sell_notional_1h", "sell_notional_24h",
    # Since-entry
    "buys_since_entry", "sells_since_entry",
    "buy_notional_since_entry", "sell_notional_since_entry",
    # Net flows
    "net_notional_24h", "net_notional_since_entry",
    # State descriptors
    "last_trade_side", "activity_state", "hold_hours",
    "unique_sizes_5m", "max_trades_60s", "latest_trade_ts",
    # Snapshot timestamp (renamed from analyzed_ts → observed_ts permitted by B)
    "analyzed_ts",
})

# Per Workstream B disposition: these are forbidden in the new table.
POSITION_MARKET_STATE_FORBIDDEN_COLUMNS: frozenset[str] = frozenset({
    # Score-coupled (analyzer output, not market state)
    "position_insider_score",
    "position_speculator_score",
    "position_classification",
    "signal_reason",
    # Paper feedback (belongs with paper_trades)
    "paper_trade_outcome",
    "paper_trade_roi",
    "paper_trade_closed_at",
    # Legacy table metadata
    "updated_at",
    # Replaced by analyzer_run_id (owner decision per Codex's B summary)
    "analysis_version",
})


def test_ensure_position_market_state_creates_table(conn):
    ensure_position_market_state(conn)
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(position_market_state)")
    }
    missing = POSITION_MARKET_STATE_KEEP_COLUMNS - cols
    assert not missing, f"missing KEEP columns: {missing}"


def test_position_market_state_has_analyzer_run_id_replacement(conn):
    """analysis_version is replaced by analyzer_run_id (Codex B summary)."""
    ensure_position_market_state(conn)
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(position_market_state)")
    }
    assert "analyzer_run_id" in cols
    assert "analysis_version" not in cols


def test_position_market_state_has_own_timestamp(conn):
    """B says: drop legacy updated_at; new table owns its own timestamp."""
    ensure_position_market_state(conn)
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(position_market_state)")
    }
    assert "created_at" in cols
    assert "updated_at" not in cols


def test_position_market_state_excludes_forbidden_columns(conn):
    """Score-coupled, paper feedback, and legacy metadata must NOT be present
    (Workstream B trace 2026-04-29)."""
    ensure_position_market_state(conn)
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(position_market_state)")
    }
    leaked = POSITION_MARKET_STATE_FORBIDDEN_COLUMNS & cols
    assert not leaked, (
        f"forbidden columns leaked into position_market_state: {leaked}"
    )


def test_position_market_state_primary_key(conn):
    ensure_position_market_state(conn)
    pk_cols = sorted(
        (r["pk"], r["name"])
        for r in conn.execute("PRAGMA table_info(position_market_state)")
        if r["pk"] > 0
    )
    assert [n for _, n in pk_cols] == ["wallet", "condition_id", "outcome"]


def test_position_market_state_indexes_created(conn):
    ensure_position_market_state(conn)
    idx = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='position_market_state'"
        )
    }
    assert "idx_pms_run" in idx
    assert "idx_pms_wallet_open" in idx


def test_wallet_layer5_decision_fk_to_analyzer_runs(conn):
    ensure_analyzer_runs(conn)
    ensure_wallet_layer5_decision(conn)
    fks = [
        dict(r)
        for r in conn.execute("PRAGMA foreign_key_list(wallet_layer5_decision)")
    ]
    assert any(
        fk["from"] == "analyzer_run_id"
        and fk["table"] == "analyzer_runs"
        and fk["to"] == "run_id"
        for fk in fks
    )


def test_position_market_state_fk_to_analyzer_runs(conn):
    ensure_analyzer_runs(conn)
    ensure_position_market_state(conn)
    fks = [
        dict(r)
        for r in conn.execute("PRAGMA foreign_key_list(position_market_state)")
    ]
    assert any(
        fk["from"] == "analyzer_run_id"
        and fk["table"] == "analyzer_runs"
        and fk["to"] == "run_id"
        for fk in fks
    )


def test_idempotent_ensure_position_market_state(conn):
    ensure_position_market_state(conn)
    ensure_position_market_state(conn)


def test_position_market_state_keep_count_matches_workstream_b(conn):
    """Workstream B locked the KEEP set at 44 columns. Schema must include
    all 44 plus exactly two infrastructure additions: analyzer_run_id (replaces
    analysis_version per Codex B summary) and created_at (replaces legacy
    updated_at per B's 'new table should own its own timestamp' note)."""
    ensure_position_market_state(conn)
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(position_market_state)")
    }
    assert len(POSITION_MARKET_STATE_KEEP_COLUMNS) == 44
    expected_total = (
        POSITION_MARKET_STATE_KEEP_COLUMNS
        | {"analyzer_run_id", "created_at"}
    )
    extras = cols - expected_total
    assert not extras, (
        f"unexpected columns in position_market_state (not in KEEP-44 + "
        f"infrastructure): {extras}"
    )


# =============================================================================
# Task A5: atomic persistence helpers
# =============================================================================

import json  # noqa: E402
import time  # noqa: E402

from sniper.analysis.wallet_protocol.persistence import (  # noqa: E402
    DEFAULT_ANALYSIS_VERSION,
    get_wallet_layer5_decision,
    persist_failed_run,
    persist_position_signal_decision,
    persist_successful_run_with_decision,
)
from sniper.analysis.wallet_protocol.types import ProtocolReport  # noqa: E402
from sniper.analysis.wallet_protocol.wallet_layer5_mapping import Layer5Decision  # noqa: E402


@pytest.fixture
def db(tmp_path):
    from db import init_db

    p = tmp_path / "t.db"
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    init_db(c)
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")
    yield c
    c.close()


def _make_pr(wallet: str = "0xabc") -> ProtocolReport:
    return ProtocolReport(
        wallet=wallet,
        analyzed_at=int(time.time()),
        overall_verdict="edge_detected",
        trading_mode="held_analyst_only",
        gate_result="pass",
        primary_edge_type="entry_timing",
    )


def _make_decision() -> Layer5Decision:
    return Layer5Decision(
        "validated_copy_candidate",
        ["edge_detected", "primary_edge_type:entry_timing"],
        "high",
    )


def _persist_kwargs() -> dict:
    """Common kwargs for persist_successful_run_with_decision."""
    now = int(time.time())
    return {
        "started_ts": now,
        "finished_ts": now,
        "duration_ms": 100,
        "invocation_source": "test",
        "args_json": "{}",
        "fetch_quality_flags_json": "[]",
        "activity_rows_fetched": 120,
        "activity_cap_hit": False,
        "subgraph_fallback_used": False,
    }


def test_success_path_round_trip(db):
    run_id = persist_successful_run_with_decision(
        db, pr=_make_pr(), decision=_make_decision(), **_persist_kwargs()
    )
    runs = list(
        db.execute("SELECT * FROM analyzer_runs WHERE run_id=?", (run_id,))
    )
    assert len(runs) == 1
    assert runs[0]["success"] == 1
    got = get_wallet_layer5_decision(db, "0xabc")
    assert got is not None
    assert got["is_current"] == 1
    assert got["analyzer_run_id"] == run_id
    assert got["action_decision"] == "validated_copy_candidate"
    assert got["confidence_band"] == "high"
    assert json.loads(got["action_reason_codes_json"]) == [
        "edge_detected",
        "primary_edge_type:entry_timing",
    ]


def test_failure_path_run_only(db):
    now = int(time.time())
    run_id = persist_failed_run(
        db,
        wallet="0xabc",
        started_ts=now,
        finished_ts=now,
        duration_ms=10,
        error_class="ValueError",
        error_message="boom",
        invocation_source="test",
        args_json="{}",
        fetch_quality_flags_json="[]",
        activity_rows_fetched=None,
        activity_cap_hit=None,
        subgraph_fallback_used=False,
    )
    runs = list(
        db.execute("SELECT * FROM analyzer_runs WHERE run_id=?", (run_id,))
    )
    assert len(runs) == 1
    assert runs[0]["success"] == 0
    assert runs[0]["error_class"] == "ValueError"
    assert runs[0]["error_message"] == "boom"
    assert get_wallet_layer5_decision(db, "0xabc") is None


class _BeginLockedOnceProxy:
    def __init__(self, conn):
        self.conn = conn
        self.begin_attempts = 0
        self.rollback_count = 0

    def execute(self, sql, *args, **kwargs):
        if str(sql).strip().upper() == "BEGIN IMMEDIATE":
            self.begin_attempts += 1
            if self.begin_attempts == 1:
                raise sqlite3.OperationalError("database is locked")
        return self.conn.execute(sql, *args, **kwargs)

    def commit(self):
        return self.conn.commit()

    def rollback(self):
        self.rollback_count += 1
        return self.conn.rollback()


def test_successful_persist_retries_transient_begin_locked(db, monkeypatch):
    from sniper.analysis.wallet_protocol import persistence as P

    monkeypatch.setattr(P.time, "sleep", lambda _seconds: None)
    proxy = _BeginLockedOnceProxy(db)

    run_id = persist_successful_run_with_decision(
        proxy, pr=_make_pr(), decision=_make_decision(), **_persist_kwargs()
    )

    assert proxy.begin_attempts == 2
    assert proxy.rollback_count == 1
    assert get_wallet_layer5_decision(db, "0xabc")["analyzer_run_id"] == run_id


def test_failed_persist_retries_transient_begin_locked(db, monkeypatch):
    from sniper.analysis.wallet_protocol import persistence as P

    monkeypatch.setattr(P.time, "sleep", lambda _seconds: None)
    proxy = _BeginLockedOnceProxy(db)
    now = int(time.time())

    run_id = persist_failed_run(
        proxy,
        wallet="0xabc",
        started_ts=now,
        finished_ts=now,
        duration_ms=10,
        error_class="ValueError",
        error_message="boom",
        invocation_source="test",
        args_json="{}",
        fetch_quality_flags_json="[]",
        activity_rows_fetched=None,
        activity_cap_hit=None,
        subgraph_fallback_used=False,
    )

    assert proxy.begin_attempts == 2
    assert proxy.rollback_count == 1
    row = db.execute(
        "SELECT success, error_class FROM analyzer_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert dict(row) == {"success": 0, "error_class": "ValueError"}


def test_atomic_rollback_on_decision_failure(db, monkeypatch):
    """If the decision insert fails mid-transaction, the analyzer_runs row
    must NOT survive — that's the atomicity guarantee."""
    from sniper.analysis.wallet_protocol import persistence as P

    def _boom(branch):
        raise RuntimeError("synthetic failure mid-transaction")

    monkeypatch.setattr(P, "_branch_to_json", _boom)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        persist_successful_run_with_decision(
            db, pr=_make_pr(), decision=_make_decision(), **_persist_kwargs()
        )

    runs = list(db.execute("SELECT * FROM analyzer_runs"))
    decisions = list(db.execute("SELECT * FROM wallet_layer5_decision"))
    assert len(runs) == 0, (
        f"analyzer_runs must roll back when the decision insert fails; "
        f"got {len(runs)} surviving rows"
    )
    assert len(decisions) == 0


def test_version_bump_flips_is_current(db):
    """Old-version row's is_current flips to 0 when new-version row lands."""
    pr = _make_pr()
    d = _make_decision()
    persist_successful_run_with_decision(
        db, pr=pr, decision=d, analysis_version="wpv2.0", **_persist_kwargs()
    )
    persist_successful_run_with_decision(
        db, pr=pr, decision=d, analysis_version="wpv2.1", **_persist_kwargs()
    )
    rows = list(
        db.execute(
            "SELECT analysis_version, is_current FROM wallet_layer5_decision "
            "WHERE wallet='0xabc' ORDER BY analysis_version"
        )
    )
    by_v = {r["analysis_version"]: r["is_current"] for r in rows}
    assert by_v == {"wpv2.0": 0, "wpv2.1": 1}


def test_in_place_rerun_same_version_overwrites(db):
    """Rerunning the same version produces ONE row, not three."""
    pr = _make_pr()
    d = _make_decision()
    for _ in range(3):
        persist_successful_run_with_decision(
            db, pr=pr, decision=d, **_persist_kwargs()
        )
    n = list(
        db.execute(
            "SELECT COUNT(*) AS c FROM wallet_layer5_decision WHERE wallet='0xabc'"
        )
    )[0]["c"]
    assert n == 1


def test_get_returns_none_for_unknown_wallet(db):
    assert get_wallet_layer5_decision(db, "0xnope") is None


def test_pragmas_enforced(db):
    """Persistence presumes foreign_keys=ON and busy_timeout>=5000."""
    fk = list(db.execute("PRAGMA foreign_keys"))[0][0]
    bt = list(db.execute("PRAGMA busy_timeout"))[0][0]
    assert fk == 1
    assert bt >= 5000


def test_persist_position_signal_decision_round_trip(db):
    persist_position_signal_decision(
        db,
        wallet="0xabc",
        condition_id="0xcond1",
        outcome="YES",
        analysis_version=DEFAULT_ANALYSIS_VERSION,
        wallet_action_decision="validated_copy_candidate",
        primary_tag="political",
        tag_fit_score=0.65,
        tag_fit_rank=12,
        tag_fit_peer_count=42,
        tag_fit_confidence_flag="ok",
        signal_reason_codes_json='["edge_detected"]',
        analyzed_ts=int(time.time()),
    )
    rows = list(
        db.execute(
            "SELECT * FROM position_signal_decision "
            "WHERE wallet='0xabc' AND condition_id='0xcond1'"
        )
    )
    assert len(rows) == 1
    assert rows[0]["wallet_action_decision"] == "validated_copy_candidate"
    assert rows[0]["tag_fit_score"] == 0.65
    assert rows[0]["tag_fit_rank"] == 12


def test_default_analysis_version_locked():
    assert DEFAULT_ANALYSIS_VERSION == "wpv2.0"
