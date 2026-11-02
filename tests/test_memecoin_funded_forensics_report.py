from __future__ import annotations

import json
import sqlite3

import pytest

from scripts import render_memecoin_funded_forensics as forensic
from sniper.persistence import db_memecoin as m
from _source_guard import code_of


SINCE = "2026-08-23T00:00:00+00:00"
AFTER = "2026-08-23T00:01:00+00:00"
AFTER_TWO = "2026-08-23T00:02:00+00:00"
BEFORE = "2026-08-22T23:59:59+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _closed(
        conn, *, mint, network_id="solana", pnl=0.0, notional=100.0,
        entry_price=1.0, signal_source="follow_activity", attempt_id=None,
        their_size_usd=100.0, source_user_id=None, with_contract=True):
    source_user_id = source_user_id or f"wallet-{mint}"
    position_id = m.open_position(
        conn,
        source_user_id=source_user_id,
        source_handle=source_user_id,
        mint=mint,
        ticker=mint,
        entry_price=entry_price,
        entry_notional=notional,
        entry_fill_source="test",
        entry_lag_s=0.0,
        their_size_usd=their_size_usd,
        opened_at=SINCE,
        opened_recorded_at=SINCE,
        execution_mode="real",
        network_id=network_id,
        signal_source=signal_source,
        strategy="alpha_b",
        attempt_id=attempt_id,
    )
    conn.execute(
        "UPDATE memecoin_positions SET is_open=0,realized_pnl=?,closed_at=?,closed_recorded_at=?,"
        "exit_reason='test' WHERE id=?",
        (pnl, AFTER, AFTER, position_id),
    )
    if with_contract:
        conn.execute("UPDATE memecoin_positions SET funded_contract_json=? WHERE id=?",
                     (json.dumps({"source": {"signal_source": signal_source}}), position_id))
    conn.commit()
    return position_id


def _attempt_with_shadows(conn, *, attempt_id, mint, source_user_id, small, prior, mcap=None):
    m.start_entry_attempt(
        conn,
        attempt_id=attempt_id,
        decision_started_at=SINCE,
        signal_source="follow_activity",
        network_id="solana",
        mint=mint,
        source_user_id=source_user_id,
    )
    conn.execute(
        "UPDATE memecoin_entry_attempts SET classification_json=?,trace_json=? WHERE attempt_id=?",
        (
            json.dumps({"facts": {"effective_mcap": mcap}}),
            json.dumps({"substage_events": [{
                "name": "funded_entry_shadow_v1",
                "small_band_v1": {"state": small},
                "prior_5_negative_v1": {"state": prior},
            }]}),
            attempt_id,
        ),
    )
    conn.commit()


def _route(conn, position_id, observed_at, unit_price):
    conn.execute(
        "INSERT INTO memecoin_route_series(position_id,mint,network_id,seam,observed_at,"
        "expected_usdc,open_qty,unit_price) VALUES(?,?,?,?,?,?,?,?)",
        (position_id, "M", "solana", "exit_decision", observed_at, 100.0, 100.0, unit_price),
    )
    conn.commit()


def test_chart_entry_clock_does_not_fall_back_to_opened_at():
    assert forensic._entry_time({
        "opened_recorded_at": None,
        "opened_at": SINCE,
    }) is None


def test_report_uses_weighted_return_and_deterministic_best_two_removal():
    conn = _conn()
    _closed(conn, mint="B", network_id="4663", pnl=10.0, notional=100.0)
    _closed(conn, mint="A", network_id="solana", pnl=10.0, notional=100.0)
    _closed(conn, mint="C", network_id="solana", pnl=5.0, notional=100.0)
    _closed(conn, mint="D", network_id="solana", pnl=-5.0, notional=100.0)

    report = forensic.build_report(conn, SINCE)

    assert report["range"]["label"] == "preregistered"
    assert report["summaries"]["automatic"]["realized_return"] == pytest.approx(0.05)
    assert report["best_two_removed"]["removed_coin_groups"] == [
        {"network_id": "4663", "mint": "B"},
        {"network_id": "solana", "mint": "A"},
    ]
    assert report["best_two_removed"]["summary"]["realized_return"] == pytest.approx(0.0)


def test_chart_requires_positive_entry_price_and_post_entry_route_observations():
    conn = _conn()
    valid = _closed(conn, mint="VALID", entry_price=2.0)
    before_only = _closed(conn, mint="BEFORE", entry_price=2.0)
    invalid_entry = _closed(conn, mint="INVALID", entry_price=-1.0)
    _route(conn, valid, BEFORE, 2.2)
    _route(conn, valid, "2026-08-23T00:00:30+00:00", 3.0)
    _route(conn, valid, AFTER, 2.5)
    _route(conn, valid, AFTER_TWO, 99.0)
    _route(conn, before_only, BEFORE, 3.0)
    _route(conn, invalid_entry, AFTER, 3.0)

    report = forensic.build_report(conn, SINCE)

    by_position = {chart["position_id"]: chart for chart in report["charts"]}
    assert by_position[valid]["state"] == "eligible"
    assert by_position[valid]["points"] == [
        {"elapsed_s": 30.0, "return_pct": 50.0},
        {"elapsed_s": 60.0, "return_pct": 25.0},
    ]
    assert by_position[before_only]["state"] == "no_exact_route_series"
    assert by_position[invalid_entry]["state"] == "no_exact_route_series"


def test_nondefault_since_writes_an_exploratory_report(tmp_path):
    db_path = tmp_path / "forensics.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    _closed(conn, mint="A")
    conn.close()
    output = tmp_path / "forensics.html"

    assert forensic.main([
        "--db", str(db_path),
        "--since", "2026-08-24T00:00:00+00:00",
        "--output", str(output),
    ]) == 0

    assert "exploratory" in output.read_text(encoding="utf-8")


def test_html_shows_coin_position_shadows_and_feature_evidence():
    conn = _conn()
    m.start_entry_attempt(
        conn,
        attempt_id="attempt-1",
        decision_started_at=SINCE,
        signal_source="follow_activity",
        network_id="solana",
        mint="A",
        source_user_id="wallet-A",
    )
    conn.execute(
        "UPDATE memecoin_entry_attempts SET trace_json=? WHERE attempt_id='attempt-1'",
        (json.dumps({"substage_events": [{
            "name": "funded_entry_shadow_v1",
            "version": "funded_entry_shadow_v1",
            "small_band_v1": {"state": "would_block"},
            "prior_5_negative_v1": {"state": "would_allow"},
        }]}),),
    )
    conn.commit()
    _closed(conn, mint="A", attempt_id="attempt-1")

    rendered = forensic.render_html(forensic.build_report(conn, SINCE))

    assert "Coin dossiers" in rendered
    assert "small_band_v1=would_block" in rendered
    assert "Source-size evidence" in rendered
    assert "Effective-market-cap evidence" in rendered


def test_html_labels_missing_feature_values_unavailable():
    conn = _conn()
    _closed(conn, mint="A", their_size_usd=None)

    rendered = forensic.render_html(forensic.build_report(conn, SINCE))

    assert "source_size=unavailable" in rendered
    assert "effective_mcap=unavailable" in rendered


def test_report_summarizes_each_shadow_state_by_source_and_coin():
    conn = _conn()
    _attempt_with_shadows(
        conn, attempt_id="a", mint="A", source_user_id="source-1",
        small="would_block", prior="would_allow",
    )
    _attempt_with_shadows(
        conn, attempt_id="b", mint="B", source_user_id="source-1",
        small="would_block", prior="would_block",
    )
    _attempt_with_shadows(
        conn, attempt_id="c", mint="C", source_user_id="source-2",
        small="unknown", prior="not_applicable",
    )
    for mint, attempt_id, source_user_id in (("A", "a", "source-1"), ("B", "b", "source-1"),
                                              ("C", "c", "source-2")):
        _closed(conn, mint=mint, attempt_id=attempt_id, source_user_id=source_user_id)

    report = forensic.build_report(conn, SINCE)
    rendered = forensic.render_html(report)

    assert report["shadow_summary"]["small_band_v1"]["would_block"] == {
        "source_n": 1, "coin_n": 2,
    }
    assert report["shadow_summary"]["prior_5_negative_v1"]["would_allow"] == {
        "source_n": 1, "coin_n": 1,
    }
    assert "Shadow state summary" in rendered
    assert "<td>would_block</td><td>sources=1</td><td>coins=2</td>" in rendered


def test_trader_dossier_has_result_counts_and_coin_links():
    conn = _conn()
    for mint, pnl in (("WIN", 1.0), ("LOSS", -1.0), ("FLAT", 0.0), ("UNKNOWN", None)):
        _closed(conn, mint=mint, pnl=pnl, source_user_id="source-1")

    report = forensic.build_report(conn, SINCE)
    trader = report["trader_dossiers"][0]
    rendered = forensic.render_html(report)

    assert trader["result_counts"] == {
        "win_n": 1, "loss_n": 1, "flat_n": 1, "unknown_n": 1,
    }
    assert [coin["label"] for coin in trader["coins"]] == [
        "solana:FLAT", "solana:LOSS", "solana:UNKNOWN", "solana:WIN",
    ]
    assert f'href="#{trader["coins"][0]["anchor"]}"' in rendered
    assert f'id="{trader["coins"][0]["anchor"]}"' in rendered
    assert "1W/1L/1F/1U" in rendered


def test_feature_distributions_show_fixed_bin_counts():
    conn = _conn()
    for attempt_id, mint, size, mcap in (
            ("a", "A", 100.0, 25_000.0),
            ("b", "B", 300.0, 75_000.0),
            ("c", "C", 750.0, 500_000.0),
    ):
        _attempt_with_shadows(
            conn, attempt_id=attempt_id, mint=mint, source_user_id=f"source-{mint}",
            small="would_allow", prior="would_allow", mcap=mcap,
        )
        _closed(conn, mint=mint, attempt_id=attempt_id, their_size_usd=size)

    report = forensic.build_report(conn, SINCE)
    rendered = forensic.render_html(report)

    assert report["feature_summary"]["source_size"]["distribution"]["bins"] == [
        {"label": "<250", "n": 1}, {"label": "250_to_<500", "n": 1},
        {"label": "500_to_<1000", "n": 1}, {"label": ">=1000", "n": 0},
    ]
    assert report["feature_summary"]["effective_mcap"]["distribution"]["bins"] == [
        {"label": "<50000", "n": 1}, {"label": "50000_to_<250000", "n": 1},
        {"label": "250000_to_<1000000", "n": 1}, {"label": ">=1000000", "n": 0},
    ]
    assert "Source-size distribution" in rendered
    assert "Effective-market-cap distribution" in rendered


def test_report_code_never_reads_position_marks():
    assert "memecoin_position_marks" not in code_of(forensic)


def test_traders_and_concentration_remain_chain_scoped():
    conn = _conn()
    for chain, mint, source, pnl in (
        ("solana", "WIN", "a", 20), ("solana", "LOSS", "a", -4),
        ("solana", "WIN", "b", 2), ("solana", "OTHER", "b", -3),
        ("4663", "WIN", "a", -8),
    ):
        _closed(conn, mint=mint, network_id=chain, source_user_id=source, pnl=pnl)
    report = forensic.build_report(conn, SINCE)
    traders = {(row["network_id"], row["source_user_id"]): row
               for row in report["trader_dossiers"]}
    assert len(traders) == 3
    assert traders[("solana", "a")]["best_coin_removed"]["summary"]["realized_pnl"] == -4
    assert traders[("4663", "a")]["best_coin_removed"]["removed_coin_groups"] == []
    chains = {row["network_id"]: row for row in report["chain_concentration"]}
    assert chains["solana"]["summary"]["realized_pnl"] == 15
    assert chains["solana"]["best_trader_removed"]["source_user_id"] == "a"
    assert chains["solana"]["best_trader_removed"]["summary"]["realized_pnl"] == -1
    assert chains["solana"]["best_coin_removed"]["summary"]["realized_pnl"] == -7
    assert "Best positive coin removed" in forensic.render_html(report)


def test_unknown_source_does_not_become_automatic_evidence():
    conn = _conn()
    _closed(conn, mint="AUTO", pnl=2)
    _closed(conn, mint="MANUAL", signal_source="manual", pnl=100)
    _closed(conn, mint="UNKNOWN", signal_source=None, pnl=200)
    _closed(conn, mint="OTHER", signal_source="onchain_watch", pnl=300, with_contract=False)
    missing = _closed(conn, mint="MISSING", pnl=400, with_contract=False)
    conn.execute("UPDATE memecoin_positions SET source_user_id=NULL WHERE id=?", (missing,))
    report = forensic.build_report(conn, SINCE)
    assert report["summaries"]["automatic"]["realized_pnl"] == 2
    assert report["summaries"]["unknown_source"]["position_n"] == 3
    assert report["historical_compatibility"]["legacy_nonmanual"]["realized_pnl"] == 902
    assert report["chain_concentration"][0]["summary"]["position_n"] == 1
    assert "Historical compatibility" in forensic.render_html(report)


def test_report_preserves_typed_shadows_and_provenance():
    conn = _conn()
    _attempt_with_shadows(conn, attempt_id="a", mint="A", source_user_id="a",
                          small="would_allow", prior="would_allow")
    payload = {"name": "funded_entry_shadow_v1", "version": "funded_entry_shadow_v1",
               "source_size_ge_1000_v1": {"state": "would_block", "threshold_usd": 1000},
               "robinhood_organic_m5_lt_1000_v1": {"state": "not_applicable"},
               "turnover_10_50_v1": {"state": "would_allow", "ratio": 0.25},
               "nano_prior_profitable_v1": {"state": "insufficient_history"},
               "gate_provenance_v1": {"inputs": {"source_size_usd": None},
                                      "selection_gate_states": {"chase": "unknown"}}}
    conn.execute("UPDATE memecoin_entry_attempts SET trace_json=? WHERE attempt_id='a'",
                 (json.dumps({"substage_events": [payload]}),))
    _closed(conn, mint="A", attempt_id="a")
    report = forensic.build_report(conn, SINCE)
    assert report["position_dossiers"][0]["shadows"] == payload
    assert report["shadow_summary"]["turnover_10_50_v1"]["would_allow"]["coin_n"] == 1
    assert report["shadow_summary"]["nano_prior_profitable_v1"]["insufficient_history"]["coin_n"] == 1
    rendered = forensic.render_html(report)
    assert "source_size_ge_1000_v1=would_block" in rendered
    assert "gate_provenance_v1" in rendered
    assert "threshold_usd" in rendered


def test_historical_gate_diagnostics_use_last_recorded_trace(monkeypatch):
    conn = _conn()
    _attempt_with_shadows(conn, attempt_id="a", mint="A", source_user_id="a",
                          small="would_allow", prior="would_allow")
    events = [
        {"name": "execution_premium", "reason": "old", "cap": 2.0},
        {"name": "chase_quote_confirmation", "cap_pct": 50.0},
        {"name": "execution_premium", "reason": "within_cap", "cap": 1.3,
         "reference_basis": "display", "reference_price": 0.002, "allow": True},
        {"name": "chase_quote_skipped", "cap_pct": 0.0, "reason": "cap_off"},
    ]
    conn.execute("UPDATE memecoin_entry_attempts SET trace_json=? WHERE attempt_id='a'",
                 (json.dumps({"substage_events": events}),))
    _closed(conn, mint="A", attempt_id="a", pnl=7, network_id="4663")
    _closed(conn, mint="MISSING", pnl=-3)
    monkeypatch.setenv("MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT", "999")
    # as_of is the live clock with microseconds; ~0.4% of instants contain "999" and would trip
    # the no-config-leak assertion below. Pin it.
    monkeypatch.setattr(forensic, "real_utc_now_iso", lambda: "2026-08-23T00:05:00+00:00")
    report = forensic.build_report(conn, SINCE)
    first, missing = report["gate_diagnostics"]
    assert first["network_id"] == "4663"
    assert first["execution_premium"] == events[2]
    assert first["chase"] == events[3]
    assert first["realized_pnl"] == 7
    assert missing["execution_premium"] == {"state": "not_recorded"}
    assert missing["chase"] == {"state": "not_recorded"}
    rendered = forensic.render_html(report)
    assert "Historical gate diagnostics" in rendered
    assert "within_cap" in rendered
    assert "0.002" in rendered
    assert "999" not in rendered


def test_chart_requires_recorded_close_clock():
    conn = _conn()
    position_id = _closed(conn, mint="A")
    _route(conn, position_id, "2026-08-23T00:00:30+00:00", 1.2)
    _route(conn, position_id, AFTER, 1.3)
    conn.execute("UPDATE memecoin_positions SET closed_recorded_at=NULL WHERE id=?", (position_id,))
    assert forensic.build_report(conn, SINCE)["charts"][0]["state"] == "no_exact_route_series"


def test_report_date_uses_corrected_clock(monkeypatch):
    from sniper.util import clock
    monkeypatch.setattr(clock.time, "time", lambda: 10000)
    monkeypatch.setattr(clock, "offset_s", lambda path=None: 3600)
    assert forensic.build_report(_conn(), SINCE)["as_of"] == "1970-01-01T01:46:40+00:00"


def test_html_exposes_gate_states_and_recorded_inactive_turnover():
    conn = _conn()
    for attempt_id, provenance in (
        ("current", {"schema_version": 2,
                     "selection_gate_states": {"m5_turnover_over_ceiling": "out_of_scope"},
                     "gate_evaluations": {"m5_turnover_over_ceiling": {
                         "state": "out_of_scope", "configured_ratio": 1.5, "effective_ratio": 0.0}}}),
        ("historical", {"selection_gate_states": {"source_buy_floor": "applied"}}),
    ):
        _attempt_with_shadows(conn, attempt_id=attempt_id, mint=attempt_id, source_user_id="a",
                              small="would_allow", prior="would_allow")
        conn.execute("UPDATE memecoin_entry_attempts SET trace_json=? WHERE attempt_id=?",
                     (json.dumps({"substage_events": [{"name": "funded_entry_shadow_v1",
                         "gate_provenance_v1": provenance}]}), attempt_id))
        _closed(conn, mint=attempt_id, attempt_id=attempt_id)
    rendered = forensic.render_html(forensic.build_report(conn, SINCE))
    assert "m5_turnover_over_ceiling=out_of_scope" in rendered
    assert "source_buy_floor=applied" in rendered
    assert "Recorded turnover ceiling inactive: configured=1.5 effective=0.0" in rendered
    assert "Recorded shadow and gate evaluations" in rendered


def test_each_candidate_removes_its_own_largest_wallet_per_chain_and_state():
    conn = _conn()
    candidates = ("turnover_10_50_v1", "nano_prior_profitable_v1", "robinhood_organic_m5_lt_1000_v1")
    for index, (source, chain, pnl, states, signal, contract) in enumerate([
        ("a", "solana", 100, ("would_allow", "would_block", "unknown"), "follow_activity", True),
        ("b", "solana", 30, ("would_allow", "would_allow", "would_allow"), "follow_activity", True),
        ("c", "solana", -10, ("would_block", "would_allow", "would_allow"), "follow_activity", True),
        ("a", "solana", -5, ("unknown", "insufficient_history", "would_block"), "follow_activity", True),
        ("d", "4663", 70, ("would_allow", "would_allow", "would_allow"), "follow_activity", True),
        ("manual", "solana", 900, ("would_allow",) * 3, "manual", True),
        ("legacy", "solana", 800, ("would_allow",) * 3, "follow_activity", False),
    ]):
        attempt_id = str(index)
        _attempt_with_shadows(conn, attempt_id=attempt_id, mint=attempt_id, source_user_id=source,
                              small="would_allow", prior="would_allow")
        event = {"name": "funded_entry_shadow_v1", **{
            name: {"state": state} for name, state in zip(candidates, states)}}
        conn.execute("UPDATE memecoin_entry_attempts SET trace_json=? WHERE attempt_id=?",
                     (json.dumps({"substage_events": [event]}), attempt_id))
        _closed(conn, mint=attempt_id, attempt_id=attempt_id, network_id=chain, pnl=pnl,
                source_user_id=source, signal_source=signal, with_contract=contract)
    report = forensic.build_report(conn, SINCE)
    result = report["candidate_concentration"]
    assert set(result) == set(candidates)
    for name, removed, before, after in (
        (candidates[0], "a", 130, 30), (candidates[1], "b", 20, -10),
        (candidates[2], "b", 20, -10),
    ):
        by_chain = {row["network_id"]: row for row in result[name]["would_allow"]}
        row = by_chain["solana"]
        assert row["summary"]["realized_pnl"] == before
        assert row["best_trader_removed"]["source_user_id"] == removed
        remaining = row["best_trader_removed"]["summary"]
        assert remaining["realized_pnl"] == after
        assert remaining["position_n"] == remaining["outcome_n"] == 1
        assert remaining["wallet_n"] == remaining["coin_n"] == 1
        assert remaining["entry_notional"] == 100
        assert by_chain["4663"]["best_trader_removed"]["source_user_id"] == "d"
        assert by_chain["4663"]["best_trader_removed"]["summary"]["position_n"] == 0
    assert result[candidates[0]]["would_block"][0]["best_trader_removed"]["source_user_id"] is None
    assert result[candidates[1]]["insufficient_history"][0]["summary"]["realized_pnl"] == -5
    assert result[candidates[2]]["unknown"][0]["summary"]["realized_pnl"] == 100
    rendered = forensic.render_html(report)
    assert "Candidate wallet concentration" in rendered
    assert "remaining wallets" in rendered
    assert "remaining PnL" in rendered
