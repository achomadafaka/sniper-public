import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "nozomi_grade", Path(__file__).parents[1] / "scripts/research/nozomi_ab_grade.py")
grade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grade)
AB_RULE = "nozomi-sha256-attempt-odd-v1"
CAPPED_RULE = "nozomi-capped-bid-v1"


def row(rule=CAPPED_RULE, tip=1_000_000, sent_tip=1_000_000, budget=True, priority=1_520_000,
        fallback=None, **send):
    aid = next(str(i) for i in range(100) if grade.arm_of(str(i)) == "B")
    trace = {"terminal_status": "failed", "terminal_reason": "instruction_error",
             "substage_events": [
                 {"name": "nozomi_ab", "rule": rule, "arm": "B", "tip_lamports": tip},
                 {"name": "slot_send_call", "tx_sig": "sig", "elapsed_ms": 5},
                 {"name": "slot_receipt", "source_slot": 10},
                 {"name": "slot_resend", "landing_slot": 10},
                 {"name": "nozomi_ab_send", "tip_lamports": sent_tip,
                  "nozomi_state": "accepted", **send}]}
    if budget:
        trace["substage_events"].append({"name": "entry_bid_signed",
            "priority_fee_lamports": priority, "tip_lamports": sent_tip,
            "combined_fee_lamports": priority + sent_tip if type(sent_tip) is int else None})
    if fallback is not None:
        trace["substage_events"] = [s for s in trace["substage_events"] if s["name"] != "nozomi_ab_send"]
        trace["substage_events"].append({"name": "nozomi_ab_fallback", "sent": fallback,
                                        "reason": "tip_over_packet"})
    return grade.grade_row(aid, "wallet", trace)


@pytest.mark.parametrize("tip", [1_000_000, 2_500_000, 5_000_000])
def test_capped_tips_are_distinct_valid_cohort(tip):
    result = row(tip=tip, sent_tip=tip)
    assert result["problems"] == []
    assert result["rule"] == CAPPED_RULE
    assert result["tip_lamports"] == tip
    assert result["landing_slot"] == result["source_slot"]
    assert result["terminal"] == "failed"
    assert result["reason"] == "instruction_error"


@pytest.mark.parametrize("tip,sent_tip", [
    (999_999, 999_999), (5_000_001, 5_000_001), (2_000_000, 3_000_000),
    (2_000_000, None), (None, None), (True, True), ("2000000", "2000000")])
def test_invalid_capped_tip_stops(tip, sent_tip):
    result = row(tip=tip, sent_tip=sent_tip)
    assert result["problems"]
    assert grade.early_stops([result])


@pytest.mark.parametrize("rule", [AB_RULE, "nozomi-all-b-v1"])
def test_original_fixed_tip_remains_strict(rule):
    assert row(rule, 5_000_000, 5_000_000)["problems"] == []
    assert grade.early_stops([row(rule, 2_000_000, 2_000_000)])


@pytest.mark.parametrize("rule", [CAPPED_RULE, "nozomi-all-b-v1"])
def test_all_b_speed_is_not_compared_against_original_control(rule):
    control = dict(row(AB_RULE, 5_000_000, 5_000_000), arm="A", send_ms=5)
    treatment = dict(row(rule, 5_000_000, 5_000_000), send_ms=50)
    assert not any(s.startswith("1 ") for s in grade.early_stops([control] * 5 + [treatment] * 5))
    original = dict(treatment, rule=AB_RULE)
    assert any(s.startswith("1 ") for s in grade.early_stops([control] * 5 + [original] * 5))


def test_actual_capped_tips_determine_spend_stop():
    landed = dict(row(), terminal="filled")
    assert not any(s.startswith("6 ") for s in grade.early_stops([landed] * 80))
    assert any(s.startswith("6 ") for s in grade.early_stops([landed] * 81))


def test_capped_bytes_and_provider_failures_still_stop():
    assert any("BYTES" in s for s in grade.early_stops([row(nozomi_payload_sha_match=False)]))
    assert any(s.startswith("5 ") for s in grade.early_stops([row(nozomi_state="explicit_rejected")] * 3))


def test_signed_fee_cap_and_missing_evidence():
    assert row(tip=5_000_000, sent_tip=5_000_000)["problems"] == []
    assert any("BID" in s for s in grade.early_stops([
        row(tip=5_000_000, sent_tip=5_000_000, priority=1_520_001)]))
    assert "signed_bid_unknown" in row(budget=False)["problems"]


def test_capped_packet_fallback_checks_actual_untipped_signed_budget():
    result = row(tip=5_000_000, sent_tip=0, fallback="untipped")
    assert result["tip_lamports"] == 5_000_000
    assert result["fallback"] is True
    assert result["problems"] == []
    assert grade.early_stops([result]) == []
    for invalid in (row(sent_tip=0), row(sent_tip=0, fallback="unknown"),
                    row(sent_tip=1_000_000, fallback="untipped"),
                    row(sent_tip=0, fallback="untipped", priority=6_520_001)):
        assert any("BID" in s for s in grade.early_stops([invalid]))


def test_main_prints_separate_rule_cohorts(tmp_path, monkeypatch, capsys):
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE memecoin_entry_attempts (attempt_id, source_user_id, trace_json, "
                   "delivery_lane, network_id, decision_started_at)")
        for rule in (AB_RULE, grade.ALL_B_RULE, CAPPED_RULE):
            trace = {"substage_events": [
                {"name": "nozomi_ab", "rule": rule, "arm": "B", "tip_lamports": 5_000_000},
                {"name": "slot_send_call", "tx_sig": "sig", "elapsed_ms": 5},
                {"name": "nozomi_ab_send", "tip_lamports": 5_000_000, "nozomi_state": "accepted"}]}
            aid = next(str(i) for i in range(100) if grade.arm_of(str(i)) == "B")
            db.execute("INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?,?,?)",
                       (aid, "wallet", json.dumps(trace), "slot_lane_processed", "solana", "2026-10-12"))
    monkeypatch.setattr(grade, "env_wallets", lambda db: {"wallet": "wallet"})
    monkeypatch.setattr(grade, "key_hits", lambda db, since: None)
    monkeypatch.setattr(grade.sys, "argv", ["grade", "--db", str(path), "--read"])
    assert grade.main() == 0
    out = capsys.readouterr().out
    for rule in (AB_RULE, grade.ALL_B_RULE, CAPPED_RULE):
        assert "rule %s:" % rule in out
        assert "  %s B" % rule in out
    assert out.count("source lane slot_lane_processed arm B: n=1;") == 3
    assert "arm B: n=3" not in out
