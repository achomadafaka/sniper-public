"""Offline outcome contract: no providers, model calls or production writes."""
import hashlib
import json
from decimal import Decimal

import pytest

from scripts.research import desk_judge_speed_outcomes as O


def state(t=0.0, slot=1, **changes):
    return {"received_at": t, "slot": slot, "tx_index": 0, "ordinal": 0,
            "signature": f"sig-{slot}", "mint": "M", "actor": "ordinary",
            "quote_mint": "1" * 32, "virtual_quote": 100_000_000_000,
            "virtual_token": 1_000_000_000_000, "real_quote": 70_000_000_000,
            "real_token": 700_000_000_000, "fee_bps": 95, "creator_fee_bps": 30,
            "cashback_fee_bps": 0, **changes}


def case():
    sheet = json.dumps({"opportunity_id": "T"}, separators=(",", ":"))
    return {"call_id": "C", "ticket_id": "T", "opportunity_id": "T", "mint": "M",
            "call_started_at": "1970-01-01T00:00:00+00:00", "sheet": sheet,
            "sheet_sha256": hashlib.sha256(sheet.encode()).hexdigest()}


def tape():
    return [state(float(t), t + 1) for t in range(313)]


def fx_proof():
    meta = {"sol_usd": 100, "sol_age_s": 10, "as_of": -0.1,
            "version": "asof-v1", "snapshot_hash": "a" * 64}
    raw = json.dumps({"asof": {"meta": meta}})
    proof = {"call_id": "C", "sheet_sha256": case()["sheet_sha256"],
             "result_json_sha256": hashlib.sha256(raw.encode()).hexdigest(), "asof": meta}
    return proof, raw


def test_integer_buy_and_sell_include_each_fee_and_own_reserve_shift():
    s = state()
    entry = O.buy_exact(s, 250_000_000)
    net = 250_000_000 * 10_000 // 10_125
    while net + (net * 95 + 9999) // 10000 + (net * 30 + 9999) // 10000 > 250_000_000:
        net -= 1
    qty = s["virtual_token"] * (net - 1) // (s["virtual_quote"] + net - 1)
    assert entry["curve_in_atomic"] == net
    assert entry["tokens_atomic"] == qty
    assert entry["spend_atomic"] == 250_000_000
    expected_gross = qty * (s["virtual_quote"] + net) // s["virtual_token"]
    exit_ = O.sell_exact(s, entry)
    assert exit_["gross_atomic"] == expected_gross
    assert exit_["out_atomic"] == expected_gross - (expected_gross * 95 + 9999) // 10000 - (expected_gross * 30 + 9999) // 10000
    assert isinstance(entry["tokens_atomic"], int)


def test_true_zero_fees_are_known_and_each_unknown_fee_is_rejected():
    entry = O.buy_exact(state(fee_bps=0, creator_fee_bps=0, cashback_fee_bps=0), 250_000_000)
    assert entry["curve_in_atomic"] == 250_000_000
    for field in ["fee_bps", "creator_fee_bps", "cashback_fee_bps"]:
        with pytest.raises(O.Censor, match="fee_unknown"):
            O.buy_exact(state(**{field: None}), 250_000_000)


def test_fixed_endpoints_and_stress_are_separate_from_core_return():
    result = O.grade_case(case(), tape(), [], Decimal("100"))
    assert result["entry_at"] == 1.5
    for h in [60, 300]:
        out = result["outcomes"][str(h)]
        assert out["state"] == "modeled"
        assert out["endpoint_at"] == 1.5 + h
        assert out["exit"]["received_at"] <= out["endpoint_at"]
        assert out["stress_return_pct"] == pytest.approx(out["return_pct"] - 2.5)
        assert out["return_pct"] < 0


@pytest.mark.parametrize("changes,reason", [
    ({"quote_mint": None}, "quote_unit_unknown"),
    ({"quote_mint": "USDC"}, "non_sol_quote"),
    ({"cashback_fee_bps": None}, "fee_unknown"),
    ({"real_token": 0}, "curve_complete"),
    ({"real_quote": None}, "reserve_unknown"),
    ({"real_quote": 100_000_000_001}, "reserve_invalid"),
    ({"real_token": 1_000_000_000_001}, "reserve_invalid"),
    ({"fee_bps": True}, "fee_invalid"),
    ({"actor": None}, "fee_identity_unknown"),
])
def test_entry_evidence_failures_are_censored(changes, reason):
    rows = tape(); rows[1].update(changes)
    out = O.grade_case(case(), rows, [], Decimal("100"))
    assert out["outcomes"]["60"]["state"] == "censored"
    assert out["outcomes"]["60"]["reason"] == reason
    assert out["outcomes"]["60"]["return_pct"] is None


def test_missing_fx_and_read_failure_never_become_zero_return():
    out = O.grade_case(case(), tape(), [], None)
    assert out["outcomes"]["60"]["reason"] == "fx_unproven"
    out = O.grade_case(case(), [], [], Decimal("100"), read_error="missing_tape")
    assert out["outcomes"]["300"]["reason"] == "missing_tape"


def test_partial_horizon_and_no_terminal_swaps_are_not_carried_forward():
    rows = tape()[:100]
    out = O.grade_case(case(), rows, [], Decimal("100"))
    assert out["outcomes"]["60"]["state"] == "modeled"
    assert out["outcomes"]["300"]["reason"] == "endpoint_unbracketed"
    rows = [r for r in tape() if not 40 <= r["received_at"] <= 55]
    out = O.grade_case(case(), rows, [], Decimal("100"))
    assert out["outcomes"]["60"]["reason"] == "coin_silence"


def test_gap_and_completion_censor_only_horizons_they_reach():
    for gaps, changes, reason in [([{"at": 120.0}], {}, "tape_gap"),
                                  ([], {"real_token": 0}, "curve_complete")]:
        rows = tape(); rows[120].update(changes)
        out = O.grade_case(case(), rows, gaps, Decimal("100"))
        assert out["outcomes"]["60"]["state"] == "modeled"
        assert out["outcomes"]["300"]["reason"] == reason


def test_asof_selects_causal_slot_not_a_late_old_print_or_future_row():
    rows = tape()
    rows.append(state(1.4, 0, virtual_quote=1))
    rows.append(state(1.6, 99999, virtual_quote=999_000_000_000))
    out = O.grade_case(case(), rows, [], Decimal("100"))
    assert out["entry"]["slot"] == 2


def test_same_slot_unknown_transaction_order_is_censored():
    rows = tape()
    rows.append(state(1.2, 2, tx_index=None, signature="other"))
    out = O.grade_case(case(), rows, [], Decimal("100"))
    assert out["outcomes"]["60"]["reason"] == "ambiguous_slot_order"


def test_supplemental_fx_is_verified_against_frozen_original_result():
    proof, raw = fx_proof()
    value, reason = O.verify_fx(case(), proof, raw)
    assert value == Decimal("100") and reason is None


@pytest.mark.parametrize("field,value,reason", [
    ("sol_age_s", 121, "fx_stale"), ("sol_age_s", -1, "fx_stale"),
    ("as_of", 0.01, "fx_future"), ("sol_usd", 0, "fx_invalid"),
    ("sol_usd", float("nan"), "fx_invalid"),
])
def test_invalid_fx_metadata_is_censored(field, value, reason):
    proof, raw = fx_proof(); proof["asof"][field] = value
    raw = json.dumps({"asof": {"meta": proof["asof"]}})
    proof["result_json_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    assert O.verify_fx(case(), proof, raw) == (None, reason)


def test_tampered_sheet_or_supplemental_hash_is_not_proven_fx():
    proof, raw = fx_proof(); proof["sheet_sha256"] = "0" * 64
    assert O.verify_fx(case(), proof, raw)[0] is None
    proof, raw = fx_proof()
    assert O.verify_fx(case(), proof, raw + " ")[0] is None


def test_loader_uses_bounded_mint_query_and_readonly_connection(tmp_path):
    import sqlite3
    path = tmp_path / "swaps"; path.mkdir()
    c = sqlite3.connect(path / "1970-01-01.db")
    c.execute("create table swaps(received_at real,slot integer,tx_index integer,ordinal integer,signature text,mint text,actor text,quote_mint text,virtual_quote integer,virtual_token integer,real_quote integer,real_token integer,fee_bps integer,creator_fee_bps integer,cashback_fee_bps integer)")
    c.execute("create index swaps_mint_slot on swaps(mint,slot)")
    r = state(); c.execute("insert into swaps values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(r[k] for k in ["received_at","slot","tx_index","ordinal","signature","mint","actor","quote_mint","virtual_quote","virtual_token","real_quote","real_token","fee_bps","creator_fee_bps","cashback_fee_bps"]))
    c.commit(); c.close()
    rows, gaps, reason = O.load_window(tmp_path, "M", 0, 10, row_limit=1)
    assert reason == "missing_gap_evidence" and len(rows) == 1
    with O.readonly(path / "1970-01-01.db") as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("delete from swaps")
