"""Round-trip tests for WalletDecisionResponse builder per Phase 2 § 8 #7.

Phase 3 Workstream A Task A10. Verifies the responder produces the three
status states (``ok`` / ``stale_version`` / ``never_analyzed``) over the
persistence layer landed in A5.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from db import init_db
from sniper.analysis.wallet_protocol.persistence import (
    DEFAULT_ANALYSIS_VERSION,
    persist_successful_run_with_decision,
)
from sniper.analysis.wallet_protocol.types import (
    BranchReport,
    HypothesisResult,
    ProtocolReport,
    StationarityResult,
)
from sniper.analysis.wallet_protocol.wallet_layer5_mapping import Layer5Decision
from sniper.analysis.wallet_protocol.wallet_layer5_responder import (
    build_wallet_decision_response,
)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "responder.db"
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    init_db(c)
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")
    yield c
    c.close()


def _seed(db, *, analysis_version: str = DEFAULT_ANALYSIS_VERSION) -> None:
    branch = BranchReport(
        branch="held_analyst",
        is_active=True,
        hypotheses=[
            HypothesisResult(
                name="H1", verdict="supported", n=80,
                ci_lower=0.05, ci_upper=0.30,
            )
        ],
        stationarity=StationarityResult(
            cusum_status="calibrated", h_used=25.0, k=0.5,
            signaled=False, signal_index=None,
            pre_regime_n=60, post_regime_n=20,
        ),
    )
    pr = ProtocolReport(
        wallet="0xabc",
        analyzed_at=int(time.time()),
        n_closed_positions=87,
        n_held_to_resolution=64,
        net_pnl=12345.0,
        gate_result="pass",
        trading_mode="held_analyst_only",
        overall_verdict="edge_detected",
        primary_edge_type="entry_timing",
        held_analyst_branch=branch,
    )
    decision = Layer5Decision(
        "validated_copy_candidate",
        ["edge_detected", "primary_edge_type:entry_timing"],
        "high",
    )
    persist_successful_run_with_decision(
        db,
        pr=pr,
        decision=decision,
        started_ts=int(time.time()),
        finished_ts=int(time.time()),
        duration_ms=100,
        invocation_source="test",
        args_json="{}",
        fetch_quality_flags_json="[]",
        activity_rows_fetched=120,
        activity_cap_hit=False,
        subgraph_fallback_used=False,
        analysis_version=analysis_version,
    )


def test_response_status_ok_for_seeded_wallet(db):
    _seed(db)
    resp = build_wallet_decision_response(db, "0xabc")
    assert resp["status"] == "ok"
    assert resp["wallet"] == "0xabc"
    assert resp["analysis_version"] == DEFAULT_ANALYSIS_VERSION
    assert resp["is_current"] is True
    assert resp["action_decision"] == "validated_copy_candidate"
    assert resp["confidence_band"] == "high"
    assert resp["overall_verdict"] == "edge_detected"
    assert resp["gate_result"] == "pass"
    assert resp["trading_mode"] == "held_analyst_only"
    # Required list fields per Phase 2 § 6.1 — never null.
    for k in (
        "action_reason_codes",
        "data_quality_flags",
        "applicable_limitations",
        "open_questions",
    ):
        assert isinstance(resp[k], list), (
            f"{k} must be a list, got {type(resp[k])}"
        )
    # Numeric direct measurements.
    assert resp["n_closed_positions"] == 87
    assert resp["net_pnl_usdc"] == 12345.0
    assert resp["activity_cap_hit"] is False
    # Reason codes round-trip through JSON unchanged.
    assert resp["action_reason_codes"] == [
        "edge_detected",
        "primary_edge_type:entry_timing",
    ]
    # Phase-4 endpoint placeholder.
    assert resp["report_markdown_url"] == "/api/wallet-decision/0xabc/report.md"


def test_response_status_never_analyzed(db):
    resp = build_wallet_decision_response(db, "0xnope")
    assert resp == {"wallet": "0xnope", "status": "never_analyzed"}


def test_response_status_stale_version(db, monkeypatch):
    """Persist at wpv2.0, ask the responder for wpv2.1 — returns
    stale_version with the wpv2.0 body and is_current=False even though
    the stored row's is_current flag is True (within its own version)."""
    _seed(db)
    monkeypatch.setattr(
        "sniper.analysis.wallet_protocol.wallet_layer5_responder.CURRENT_ANALYSIS_VERSION",
        "wpv2.1",
    )
    resp = build_wallet_decision_response(db, "0xabc")
    assert resp["status"] == "stale_version"
    assert resp["analysis_version"] == "wpv2.0"
    assert resp["is_current"] is False
    # Body still surfaces so consumers can decide whether to refresh or
    # display the stale verdict.
    assert resp["action_decision"] == "validated_copy_candidate"


def test_response_stale_version_when_only_old_row_persisted(db, monkeypatch):
    """A wallet seeded at wpv2.0 alone, then the responder bumped to wpv2.1.
    The wpv2.0 row's is_current is still True (no newer-version write
    flipped it). The responder must still mark stale_version because the
    requested version doesn't match."""
    _seed(db, analysis_version="wpv2.0")
    monkeypatch.setattr(
        "sniper.analysis.wallet_protocol.wallet_layer5_responder.CURRENT_ANALYSIS_VERSION",
        "wpv2.1",
    )
    resp = build_wallet_decision_response(db, "0xabc")
    assert resp["status"] == "stale_version"
    assert resp["analysis_version"] == "wpv2.0"


def test_response_finds_only_old_version_row_after_is_current_flipped(db):
    """Wallet seeded at wpv2.0, then a wpv2.1 row lands → wpv2.0's
    is_current flips to 0. With CURRENT_ANALYSIS_VERSION still wpv2.0 (no
    monkeypatch), the wpv2.0 row is no longer canonical-latest.

    Variation: seed wpv2.0 only, do NOT seed wpv2.1, but flip is_current
    manually so get_wallet_layer5_decision returns None. The responder
    must fall back to the most-recent any-version row and mark
    stale_version."""
    _seed(db, analysis_version="wpv2.0")
    # Manually flip is_current to simulate "an old-version row exists but
    # the canonical-latest filter excludes it."
    db.execute(
        "UPDATE wallet_layer5_decision SET is_current = 0 WHERE wallet = ?",
        ("0xabc",),
    )
    db.commit()
    resp = build_wallet_decision_response(db, "0xabc")
    assert resp["status"] == "stale_version"
    assert resp["analysis_version"] == "wpv2.0"
    assert resp["is_current"] is False
