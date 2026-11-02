"""A frozen source-reserve model is separate from an executable quote and the landed fill."""
import json
from pathlib import Path

import pytest

from sniper.execution import entry_attribution as attribution
from sniper.execution import pump_curve_buy as curve


FRAMES = json.loads((Path(__file__).parent / "fixtures/memecoin/position_73924_price_frames.json").read_text())
SPENT = 49.999999967568
ACQUIRED = 6010996.730858


def test_exact_receipts_explain_prediction_gap_by_reserve_change():
    source = curve.parse_trade_event(FRAMES["source"]["trade_event_log"])
    entry = curve.parse_trade_event(FRAMES["entry"]["trade_event_log"])
    pre_entry = {**entry,
                 "virtual_sol_reserves": entry["virtual_sol_reserves"] - entry["sol_amount"],
                 "virtual_token_reserves": entry["virtual_token_reserves"] + entry["token_amount"],
                 "real_token_reserves": entry["real_token_reserves"] + entry["token_amount"]}
    assert curve.expected_tokens_out(FRAMES["spend_lamports"], source) == 7361666965826
    assert curve.expected_tokens_out(FRAMES["spend_lamports"], pre_entry) == entry["token_amount"] == 6010996730858
    assert pre_entry["virtual_sol_reserves"] - source["virtual_sol_reserves"] == 4864638242
    assert pre_entry["virtual_token_reserves"] - source["virtual_token_reserves"] == -68702510698270
    assert (source["fee_basis_points"], source["creator_fee_basis_points"]) == (95, 30)
    assert (entry["fee_basis_points"], entry["creator_fee_basis_points"]) == (95, 30)


def test_benchmark_reports_frozen_model_and_actual_fill_without_quote_claim():
    result = attribution.slot_model_benchmark(FRAMES["price_limit"], spent_usd=SPENT,
                                              acquired_tokens=ACQUIRED)
    assert result["basis"] == "source_reserve_model"
    assert result["expected_tokens_atomic"] == 7361666965826
    assert result["expected_tokens"] == pytest.approx(7361666.965826)
    assert result["model_price_usd"] == pytest.approx(0.000006791939954918873)
    assert result["fill_price_usd"] == pytest.approx(0.000008318088032037755)
    assert result["fill_vs_model_pct"] == pytest.approx(22.46998776857)
    assert result["output_shortfall_pct"] == pytest.approx(18.347342269598)
    assert result["min_tokens_atomic"] == 5793722831229
    assert result["min_output_margin_pct"] == pytest.approx(3.750160405635)
    assert not any(key.startswith("quote_") for key in result)


def test_benchmark_keeps_negative_gap_and_floor_margin_as_measured():
    limit = dict(cost_usdc_atomic=2_000_000, base_decimals=2,
                 expected_tokens_atomic=400, min_tokens_atomic=300)
    result = attribution.slot_model_benchmark(limit, spent_usd=1.0, acquired_tokens=2.0)
    assert result["model_price_usd"] == 0.5
    assert result["fill_price_usd"] == 0.5
    assert result["fill_vs_model_pct"] == 0.0
    assert result["output_shortfall_pct"] == 50.0
    assert result["min_output_margin_pct"] == pytest.approx(-100 / 3)
    better = attribution.slot_model_benchmark(limit, spent_usd=2.0, acquired_tokens=5.0)
    assert better["fill_vs_model_pct"] == pytest.approx(-20.0)
    assert better["output_shortfall_pct"] == -25.0


@pytest.mark.parametrize("limit", [None, {}, {"expected_tokens_atomic": 1}])
def test_missing_model_is_unknown(limit):
    assert attribution.slot_model_benchmark(limit, spent_usd=SPENT, acquired_tokens=ACQUIRED) is None


@pytest.mark.parametrize("field,value", [
    ("cost_usdc_atomic", 0), ("cost_usdc_atomic", "50000000"),
    ("expected_tokens_atomic", -1), ("expected_tokens_atomic", True),
    ("min_tokens_atomic", 0), ("min_tokens_atomic", 7361666965827),
    ("base_decimals", -1), ("base_decimals", 256), ("base_decimals", "6"), ("base_decimals", True),
])
def test_invalid_atomic_model_is_unknown(field, value):
    limit = {**FRAMES["price_limit"], field: value}
    assert attribution.slot_model_benchmark(limit, spent_usd=SPENT, acquired_tokens=ACQUIRED) is None


@pytest.mark.parametrize("spent,acquired", [
    (float("nan"), ACQUIRED), (SPENT, float("inf")), (0, ACQUIRED),
    (SPENT, -1), (True, ACQUIRED), (SPENT, "6010996.730858"),
])
def test_invalid_actual_fill_is_unknown(spent, acquired):
    assert attribution.slot_model_benchmark(FRAMES["price_limit"], spent_usd=spent,
                                            acquired_tokens=acquired) is None


OWNER = "3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH"
MINT = "6XsC91TSAWe4ma57cRKkMe3ut5wPUWnd5dyV2wAUpump"


def _receipt(logs=None):
    return {"slot": FRAMES["entry"]["slot"],
            "transaction": {"signatures": [FRAMES["entry"]["signature"]]},
            "meta": {"err": None, "logMessages": logs if logs is not None else [
                f"Program {curve.PUMP_PROGRAM} invoke [1]",
                FRAMES["entry"]["trade_event_log"],
                f"Program {curve.PUMP_PROGRAM} success"]}}


def test_receipt_event_preserves_exact_log_and_receipt_identity():
    logs = [f"Program {curve.PUMP_PROGRAM} invoke [1]",
            FRAMES["source"]["trade_event_log"], FRAMES["entry"]["trade_event_log"],
            f"Program {curve.PUMP_PROGRAM} success"]
    assert attribution.curve_receipt_event(_receipt(logs), owner=OWNER, mint=MINT) == {
        "basis": "confirmed_receipt_trade_event",
        "event_log": FRAMES["entry"]["trade_event_log"],
        "slot": 458089627, "tx_sig": FRAMES["entry"]["signature"]}


@pytest.mark.parametrize("case", ["wrong_program", "nested_other_program", "wrong_actor", "wrong_mint", "sell", "multiple"])
def test_receipt_event_refuses_wrong_or_ambiguous_event(case):
    receipt = _receipt()
    owner, mint = OWNER, MINT
    if case == "wrong_program":
        receipt["meta"]["logMessages"] = [
            "Program AnotherProgram invoke [1]", FRAMES["entry"]["trade_event_log"],
            "Program AnotherProgram success"]
    elif case == "nested_other_program":
        receipt["meta"]["logMessages"] = [
            f"Program {curve.PUMP_PROGRAM} invoke [1]", "Program AnotherProgram invoke [2]",
            FRAMES["entry"]["trade_event_log"], "Program AnotherProgram success",
            f"Program {curve.PUMP_PROGRAM} success"]
    elif case == "wrong_actor":
        owner = "another-owner"
    elif case == "wrong_mint":
        mint = "another-mint"
    elif case == "sell":
        import base64
        raw = bytearray(base64.b64decode(FRAMES["entry"]["trade_event_log"].removeprefix("Program data: ")))
        raw[56] = 0  # TradeEvent.is_buy, after discriminator + mint + two u64 amounts.
        receipt["meta"]["logMessages"][1] = "Program data: " + base64.b64encode(raw).decode()
    else:
        receipt["meta"]["logMessages"].insert(2, FRAMES["entry"]["trade_event_log"])
    assert attribution.curve_receipt_event(receipt, owner=owner, mint=mint) is None


@pytest.mark.parametrize("case", ["failed", "missing_error", "missing_signature", "invalid_slot", "missing_logs"])
def test_receipt_event_requires_successful_complete_receipt(case):
    receipt = _receipt()
    if case == "failed":
        receipt["meta"]["err"] = {"InstructionError": [2, "Custom"]}
    elif case == "missing_error":
        del receipt["meta"]["err"]
    elif case == "missing_signature":
        receipt["transaction"]["signatures"] = []
    elif case == "invalid_slot":
        receipt["slot"] = True
    else:
        del receipt["meta"]["logMessages"]
    assert attribution.curve_receipt_event(receipt, owner=OWNER, mint=MINT) is None
