"""A pump.fun 6042 failure names the tokens available and the minimum signed, and says nothing was bought.

Send 4020 (slot lane, 2026-09-26) landed at S+1 and failed ix 8 with Custom 6042
BuySlippageBelowMinTokensOut. The program logs Left (tokens the spend would buy at that moment) and
Right (the signed minimum), which is the whole diagnosis, but the ledger kept only the error name.
The failure frame is real: tests/fixtures/memecoin/slot_price_limit_a47a6c14.json, row 5.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.execution import solana_real_seams as seams
from sniper.execution.solana_slot_executor import execute_prebuilt_entry, pump_slippage_failure
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_entry_view import entry_view

FAILED = json.loads((Path(__file__).parent / "fixtures/memecoin/slot_price_limit_a47a6c14.json")
                    .read_text())[4]
RECEIPT = {"slot": FAILED["slot"], "meta": {"err": FAILED["err"], "fee": FAILED["fee_lamports"],
                                            "logMessages": FAILED["failure_logs"]}}
EXPECTED = {"reason": "price_moved_beyond_limit", "available_tokens_atomic": 19_494_581_726_667,
            "min_tokens_atomic": 21_600_089_757_031, "fee_lamports": 85_204,
            "landing_slot": 450_797_211, "no_tokens_bought": True}
NOW = "2026-09-26T21:36:12+00:00"


def test_the_real_6042_frame_parses_available_and_minimum():
    assert pump_slippage_failure(RECEIPT) == EXPECTED


@pytest.mark.parametrize("mutate", ["other_code", "other_program", "no_right", "left_not_below"])
def test_anything_else_is_not_a_pump_slippage_failure(mutate):
    meta = json.loads(json.dumps(RECEIPT["meta"]))
    logs = meta["logMessages"]
    if mutate == "other_code":
        meta["err"] = {"InstructionError": [8, {"Custom": 6001}]}
    elif mutate == "other_program":
        logs[-1] = logs[-1].replace("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                                    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
    elif mutate == "no_right":
        del logs[2]
    else:
        logs[1] = "Program log: Left: 21600089757031"
    assert pump_slippage_failure({**RECEIPT, "meta": meta}) is None


def test_the_failed_confirm_verdict_carries_it_from_the_receipt_it_already_read(monkeypatch):
    calls = []

    def rpc_call(url, method, params, http):
        calls.append(method)
        return 200, {"result": RECEIPT}
    monkeypatch.setattr(seams, "_rpc_call", rpc_call)
    state = seams._failed_confirm_state(FAILED["err"], FAILED["signature"], "rpc", None)
    assert state == "failed" and state.pump_slippage == EXPECTED
    assert state.reason_code == "chain_error:BuySlippageBelowMinTokensOut"
    assert calls == ["getTransaction"]


class _Trace:
    def __init__(self):
        self.substages = []

    def mark_substage(self, name, **kw):
        self.substages.append((name, kw))

    def mark(self, *a, **k):
        pass

    def mark_submission(self, *a, **k):
        pass


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    yield c
    c.close()


def test_the_executor_records_it_and_releases_the_reservation(conn):
    pid = m.reserve_pending(conn, mint="81mmpbV6YK1zwEkcBfwFBSGj3yJtp445yCPyfLFLQeif",
                            source_user_id="notdecu", strategy="alpha_b", notional=175.0,
                            reserved_at=NOW, network_id="solana", origin_event_id="slotlane:sig",
                            signal_source="slot_lane", source_price=5.3e-6)
    status = seams.ConfirmState("failed", err=FAILED["err"],
                                program_error="BuySlippageBelowMinTokensOut",
                                pump_slippage=EXPECTED, landing_slot=450_797_211)
    trace = _Trace()
    result = execute_prebuilt_entry(
        conn, pending=m.pending_row(conn, pid), signed_b64="AA==", sig=FAILED["signature"],
        validity=None, send_fn=lambda _b64: True, confirm_fn=lambda _sig: status,
        receipt_fill_fn=lambda *a: pytest.fail("a failed buy has no fill"), now_iso=NOW, trace=trace)
    assert result["status"] == "failed" and result["pump_slippage"] == EXPECTED
    assert result["reason"] == "chain_error:BuySlippageBelowMinTokensOut"
    assert ("slot_pump_slippage", EXPECTED) in trace.substages
    assert conn.execute("SELECT status FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone()[0] == "failed"


def test_the_operator_view_says_what_moved_and_that_nothing_was_bought():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.start_entry_attempt(c, attempt_id="attempt", origin_event_id="slotlane:sig",
                          decision_started_at=NOW, source_handle="notdecu", network_id="solana",
                          mint="MINT", source_user_id="notdecu")
    c.execute("UPDATE memecoin_entry_attempts SET terminal_status='failed', "
              "terminal_reason='chain_error:BuySlippageBelowMinTokensOut', completed_at=?, trace_json=? "
              "WHERE attempt_id='attempt'",
              (NOW, json.dumps({"substage_events": [{"name": "slot_pump_slippage", **EXPECTED}]})))
    pid = m.reserve_pending(c, mint="MINT", source_user_id="notdecu", strategy="test", notional=175,
                            reserved_at=NOW, network_id="solana", origin_event_id="slotlane:sig",
                            attempt_id="attempt")
    m.set_pending_sig(c, pid, "signed", NOW)
    m.release_pending(c, pid, "chain_error:BuySlippageBelowMinTokensOut")
    row = entry_view(c, attempt_id="attempt")
    assert row["status"] == "transaction_failed"
    assert row["pump_slippage"]["min_tokens_atomic"] == 21_600_089_757_031
    assert row["reason"] == ("Price moved beyond limit: minimum 21.60M tokens, available 19.49M; "
                             "no purchase; fee 0.000085204 SOL")
    # PumpSwap mints need their recorded decimals; an absent unit stays atomic.
    amm = dict(EXPECTED, program_id="pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
    for decimals, want in ((8, "minimum 0.22M tokens"), (None, "minimum 21600089757031 atomic units")):
        events = [{"name":"slot_pump_slippage", **amm},
                  {"name":"slot_price_limit", "base_decimals":decimals}]
        c.execute("UPDATE memecoin_entry_attempts SET trace_json=? WHERE attempt_id='attempt'",
                  (json.dumps({"substage_events":events}),))
        assert want in entry_view(c, attempt_id="attempt")["reason"]

    events = [{'name': 'slot_pump_slippage', **EXPECTED},
              {'name': 'slot_price_limit', 'base_decimals': 6, 'cost_usdc_atomic': 175_000_000,
               'source_price_usd': 5.32e-6, 'cap_pct': 30}]
    c.execute("UPDATE memecoin_entry_attempts SET trace_json=? WHERE attempt_id='attempt'",
              (json.dumps({'substage_events': events}),))
    assert ('implied ticket price +68.7% over source post-trade (signed limit +52.3%)'
            in entry_view(c, attempt_id='attempt')['reason'])

    c.close()


def test_pumpswap_6040_retains_exact_failed_output():
    receipt = json.loads((Path(__file__).parent / "fixtures/memecoin/pumpswap_gramr_failure_20260928.json").read_text())
    result = pump_slippage_failure(receipt)
    assert result is not None
    assert result["available_tokens_atomic"] == 1486439091825
    assert result["min_tokens_atomic"] == 1719465729732
    assert result["program_id"] == "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
    receipt["meta"]["err"] = {"InstructionError": [5, {"Custom": 6042}]}
    assert pump_slippage_failure(receipt) is None


@pytest.mark.parametrize("minimum,available,decimals", [
    (6_217_523_259_107, 6_216_038_470_014, 6),  # ref 15a0f6ce: both rounded to 6.22M
    (1_000_000_000_001, 1_000_000_000_000, 6),
    (2, 1, 18),
    (2, 1, 0),
])
def test_a_shortfall_hidden_by_rounding_keeps_exact_atomic_amounts(conn, minimum, available, decimals):
    m.start_entry_attempt(conn, attempt_id="close-floor", origin_event_id="slotlane:close-floor",
                          decision_started_at=NOW, network_id="solana", mint="MINT",
                          source_user_id="u")
    pid = m.reserve_pending(conn, mint="MINT", source_user_id="u", strategy="test", notional=50,
                            reserved_at=NOW, network_id="solana", origin_event_id="slotlane:close-floor",
                            attempt_id="close-floor")
    m.set_pending_sig(conn, pid, "signed", NOW)
    m.release_pending(conn, pid, "chain_error:BuySlippageBelowMinTokensOut")
    events = [{"name": "slot_pump_slippage", **EXPECTED,
               "min_tokens_atomic": minimum, "available_tokens_atomic": available},
              {"name": "slot_price_limit", "base_decimals": decimals}]
    conn.execute("UPDATE memecoin_entry_attempts SET terminal_status='failed', terminal_reason=?, "
                 "completed_at=?, trace_json=? WHERE attempt_id='close-floor'",
                 ("chain_error:BuySlippageBelowMinTokensOut", NOW,
                  json.dumps({"substage_events": events})))
    row = entry_view(conn, attempt_id="close-floor")
    assert row["status"] == "transaction_failed"
    assert f"minimum {minimum} atomic units, available {available}" in row["reason"]
    assert "no purchase" in row["reason"]


def test_bounds_failure_names_the_binding_source_average_limit(conn):
    # ref 6ed31cee: post-trade +27.3% passed its 30% cap; average +48.2% broke the hard 35% cap.
    failure = dict(EXPECTED, available_tokens_atomic=1_200_548_833_453,
                   min_tokens_atomic=1_317_675_479_094, fee_lamports=10_005_000,
                   landing_slot=458142146,
                   program_id="pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
    limit = {"name": "slot_price_limit", "base_decimals": 6, "cost_usdc_atomic": 99_130_000,
             "source_price_usd": 6.486198318628652e-5, "cap_pct": 30,
             "source_average_price_usd": 5.572664194992709e-5, "source_average_cap_pct": 35}
    events = [{"name": "slot_pump_slippage", **failure}, limit]
    m.start_entry_attempt(conn, attempt_id="bounds", origin_event_id="slotlane:bounds",
                          decision_started_at=NOW, source_handle="DegenCapitalLLC",
                          network_id="solana", mint="BOUNDS", source_user_id="degen")
    pid = m.reserve_pending(conn, mint="BOUNDS", source_user_id="degen", strategy="test",
                            notional=99.13, reserved_at=NOW, network_id="solana",
                            origin_event_id="slotlane:bounds", attempt_id="bounds")
    m.set_pending_sig(conn, pid, "signed-bounds", NOW)
    m.release_pending(conn, pid, "chain_error:BuySlippageBelowMinBaseAmountOut")
    conn.execute("UPDATE memecoin_entry_attempts SET terminal_status='failed', terminal_reason=?, "
                 "completed_at=?, trace_json=? WHERE attempt_id='bounds'",
                 ("chain_error:BuySlippageBelowMinBaseAmountOut", NOW,
                  json.dumps({"substage_events": events})))

    view = entry_view(conn, attempt_id="bounds")
    assert view["status"] == "transaction_failed"
    assert view["reason"] == (
        "Price moved beyond limit: minimum 1.32M tokens, available 1.20M; no purchase; "
        "implied ticket price +48.2% over source average (signed limit +35.0%); "
        "fee 0.010005000 SOL")

    # The displayed ceiling must follow the signed minimum, even if a different floor is tighter.
    events[0]["min_tokens_atomic"] = 1_400_000_000_000
    conn.execute("UPDATE memecoin_entry_attempts SET trace_json=? WHERE attempt_id='bounds'",
                 (json.dumps({"substage_events": events}),))
    assert "signed limit +27.1%" in entry_view(conn, attempt_id="bounds")["reason"]

    events[0]["min_tokens_atomic"] = failure["min_tokens_atomic"]
    for unknown in (None, 0, -1, True, "0.0000557", float("nan"), float("inf")):
        limit["source_average_price_usd"] = unknown
        conn.execute("UPDATE memecoin_entry_attempts SET trace_json=? WHERE attempt_id='bounds'",
                     (json.dumps({"substage_events": events}),))
        reason = entry_view(conn, attempt_id="bounds")["reason"]
        assert "implied ticket price +27.3% over source post-trade (signed limit +16.0%)" in reason
        assert "over source average" not in reason
