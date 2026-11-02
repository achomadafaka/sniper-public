"""Receipt-priced desk expenses retain exact atom identity and honest unknowns."""
import copy
import json

import pytest

from sniper.services.memecoin_desk_universe import b58encode

try:
    from sniper.services.memecoin_desk_costs_solana import price_solana_expense
except ImportError:
    price_solana_expense = None

OWNER = b58encode(bytes(range(32)))
MINT = b58encode(bytes(range(1, 33)))
OTHER = b58encode(bytes(range(2, 34)))
SIG = b58encode(bytes(range(64)))
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
AT = "2026-10-21T12:00:00+00:00"
KNOWN = "2026-10-21T12:00:05+00:00"
STAMP = 1792584000
FX = {"price": 250, "observed_at": AT, "known_at": KNOWN, "source": "coinbase:SOL-USD:60s"}


def token(index, mint, amount, decimals=6, owner=OWNER):
    return {"accountIndex": index, "mint": mint, "owner": owner,
            "uiTokenAmount": {"amount": str(amount), "decimals": decimals}}


def trade(kind="entry", *, net=5000, fee=5000, native=False):
    entry = kind == "entry"
    event = f"entry:7" if entry else f"{'partial' if kind == 'derisk' else 'exit'}:7:{SIG}"
    fill = {"kind": kind, "qty": 10, "notional": 25, "funding_asset": "sol" if native else "usdc",
            "funding_spent_atomic": "100000000" if native else None}
    req = {"cohort_id": "desk-cohort:U:one", "event_id": event, "position_id": 7,
           "pending_id": None, "network_id": "solana", "mint": MINT, "tx_sig": SIG,
           "occurred_at": AT, "fill": fill}
    pre_token = [token(1, MINT, 0 if entry else 10_000_000), token(2, USDC, 100_000_000)]
    post_token = [token(1, MINT, 10_000_000 if entry else 0),
                  token(2, USDC, 100_000_000 if native else (75_000_000 if entry else 125_000_000))]
    principal = 100_000_000 if native else 0
    receipt = {"slot": 100, "blockTime": STAMP,
        "transaction": {"signatures": [SIG], "message": {"accountKeys": [
            {"pubkey": OWNER, "signer": True}, {"pubkey": MINT, "signer": False},
            {"pubkey": USDC, "signer": False}, {"pubkey": WSOL, "signer": False}]}},
        "meta": {"err": None, "fee": fee, "preBalances": [1_000_000_000, 2_000_000, 2_000_000, 0],
            "postBalances": [1_000_000_000 - principal - net, 2_000_000, 2_000_000, 0],
            "preTokenBalances": pre_token, "postTokenBalances": post_token}}
    return req, receipt


def price(req, receipt, *, usd_quote=FX, known_at=KNOWN, owner=OWNER):
    assert callable(price_solana_expense), "pure Solana expense pricer is missing"
    return price_solana_expense(req, receipt, owner=owner, usd_quote=usd_quote, known_at=known_at)


@pytest.mark.parametrize("kind", ["entry", "bracket_exit", "manual_close", "mirror_exit", "derisk"])
def test_usdc_trades_price_actual_fee_once_without_mutating_inputs(kind):
    req, receipt = trade(kind)
    before = copy.deepcopy((req, receipt, FX))
    out = price(req, receipt)
    assert out["execution_usd"] == pytest.approx(.00125)
    assert out["refund_usd"] == 0
    ev = out["evidence"]
    assert all(ev[key] == req[key] for key in ("cohort_id", "event_id", "network_id", "mint", "tx_sig"))
    assert ev["execution_lamports"] == 5000 and ev["network_fee_lamports"] == 5000
    assert ev["event_at"] == AT and ev["known_at"] == KNOWN and len(ev["receipt_hash"]) == 64
    json.dumps(out, allow_nan=False)
    assert (req, receipt, FX) == before


def test_native_entry_excludes_actual_trade_principal_but_counts_tip_and_rent():
    req, receipt = trade(native=True, net=2_005_000)
    receipt["meta"]["preBalances"][1] = 0
    receipt["meta"]["preTokenBalances"].pop(0)
    req["fill"].update(network_fee_lamports=5000, priority_fee_lamports=3000,
                        rent_lamports=2_000_000, returned_rent_lamports=0)
    out = price(req, receipt)
    assert out["execution_usd"] == pytest.approx(.50125)
    assert out["evidence"]["trade_principal_lamports"] == 100_000_000
    # Add a paid 1m-lamport tip; it is already in the wallet's native debit.
    receipt["meta"]["postBalances"][0] -= 1_000_000
    assert price(req, receipt)["execution_usd"] == pytest.approx(.75125)


def test_wsol_principal_is_not_counted_as_an_execution_charge():
    req, receipt = trade()
    receipt["meta"]["postBalances"][0] -= 100_000_000
    receipt["meta"]["postBalances"][3] = 102_000_000
    receipt["meta"]["preBalances"][3] = 2_000_000
    receipt["meta"]["preTokenBalances"].append(token(3, WSOL, 0, 9))
    receipt["meta"]["postTokenBalances"].append(token(3, WSOL, 100_000_000, 9))
    assert price(req, receipt)["execution_usd"] == pytest.approx(.00125)


def test_exit_refund_is_separate_from_network_expense():
    req, receipt = trade("bracket_exit", net=-1_995_000)
    receipt["meta"]["postBalances"][1] = 0
    receipt["meta"]["postTokenBalances"].pop(0)
    req["fill"].update(network_fee_lamports=5000, returned_rent_lamports=2_000_000)
    out = price(req, receipt)
    assert out["execution_usd"] == pytest.approx(.00125)
    assert out["refund_usd"] == pytest.approx(.5)
    assert out["evidence"]["refund_lamports"] == 2_000_000


def test_failed_signature_only_charges_paid_fee_and_ignores_unexecuted_trade_intent():
    req, receipt = trade(net=999_999)
    req.update(event_id=f"pending:3:{SIG}", pending_id=3, position_id=None, fill=None)
    receipt["meta"]["err"] = {"InstructionError": [0, {"Custom": 6001}]}
    receipt["meta"]["postBalances"] = list(receipt["meta"]["preBalances"])
    receipt["meta"]["postBalances"][0] -= 5000
    receipt["meta"]["postTokenBalances"] = copy.deepcopy(receipt["meta"]["preTokenBalances"])
    receipt["transaction"]["message"]["instructions"] = [{"parsed": {
        "type": "transfer", "info": {"lamports": 999_999}}}]
    out = price(req, receipt)
    assert out["execution_usd"] == pytest.approx(.00125) and out["refund_usd"] == 0


@pytest.mark.parametrize("index, change", [
    pytest.param(0, -1, id="fee-payer-overcharged"),
    pytest.param(0, 1, id="fee-payer-undercharged"),
    pytest.param(1, 1, id="other-account-changed"),
])
def test_failed_receipt_rejects_native_changes_beyond_actual_fee(index, change):
    req, receipt = trade()
    req.update(event_id=f"pending:3:{SIG}", pending_id=3, position_id=None, fill=None)
    meta = receipt["meta"]
    meta["err"] = {"InstructionError": [0, {"Custom": 6001}]}
    meta["postBalances"] = list(meta["preBalances"])
    meta["postBalances"][0] -= meta["fee"]
    meta["postTokenBalances"] = copy.deepcopy(meta["preTokenBalances"])
    meta["postBalances"][index] += change
    with pytest.raises(ValueError, match="^failed_receipt_balance_mismatch$"):
        price(req, receipt)


@pytest.mark.parametrize("failed", [False, True])
def test_proven_zero_expense_needs_no_usd_quote(failed):
    req, receipt = trade(fee=0, net=0)
    if failed:
        req.update(event_id=f"pending:3:{SIG}", pending_id=3, position_id=None, fill=None)
        receipt["meta"]["err"] = {"InstructionError": [0, {"Custom": 6001}]}
        receipt["meta"]["postTokenBalances"] = copy.deepcopy(receipt["meta"]["preTokenBalances"])
    out = price(req, receipt, usd_quote=None)
    assert out["execution_usd"] == out["refund_usd"] == 0


def test_nonzero_expense_has_one_missing_fx_reason():
    with pytest.raises(ValueError, match="^usd_quote_missing$"):
        price(*trade(), usd_quote=None)


@pytest.mark.parametrize("change", [
    {"price": 0}, {"price": True}, {"price": float("nan")}, {"price": float("inf")},
    {"observed_at": "2026-10-21T11:58:59+00:00"},
    {"observed_at": "2026-10-21T12:00:01+00:00"},
    {"known_at": "2026-10-21T12:00:06+00:00"},
    {"known_at": "2026-10-21T11:59:59+00:00"},
    {"source": "https://private.example/?api-key=secret"},
    {"observed_at": "2026-10-21T12:00:00"},
])
def test_bad_or_noncausal_fx_stays_unknown(change):
    with pytest.raises(ValueError):
        price(*trade(), usd_quote={**FX, **change})


@pytest.mark.parametrize("mutation", [
    lambda q, r: q.update(network_id="8453"),
    lambda q, r: q.update(tx_sig=b58encode(bytes(reversed(range(64))))),
    lambda q, r: q.update(mint=OTHER),
    lambda q, r: r["transaction"]["message"]["accountKeys"][0].update(pubkey=OTHER),
    lambda q, r: r["transaction"]["message"]["accountKeys"][0].update(signer=False),
    lambda q, r: r.update(slot=True), lambda q, r: r.update(blockTime=None),
    lambda q, r: r["meta"].pop("err"), lambda q, r: r["meta"].update(err=False),
    lambda q, r: r["meta"].update(err={}),
    lambda q, r: r["meta"].update(err={"InstructionError": [True, "bad"]}),
    lambda q, r: r["meta"].update(fee=None), lambda q, r: r["meta"].update(fee=True),
    lambda q, r: r["meta"]["postBalances"].pop(),
    lambda q, r: r["meta"]["postBalances"].__setitem__(0, -1),
    lambda q, r: r["meta"]["preBalances"].__setitem__(0, 2 ** 64),
    lambda q, r: r["meta"]["preTokenBalances"].append(copy.deepcopy(r["meta"]["preTokenBalances"][0])),
    lambda q, r: r["meta"]["postTokenBalances"][0]["uiTokenAmount"].update(decimals=9),
    lambda q, r: r["meta"]["postTokenBalances"][0].update(owner=OTHER),
    lambda q, r: r["meta"]["postTokenBalances"][0].update(accountIndex=99),
    lambda q, r: r["meta"]["postTokenBalances"][0]["uiTokenAmount"].update(amount="1.2"),
    lambda q, r: r["meta"]["postTokenBalances"].pop(1),
    lambda q, r: q["fill"].update(qty=None), lambda q, r: q["fill"].update(qty=11),
    lambda q, r: q["fill"].update(notional=None), lambda q, r: q["fill"].update(notional=26),
    lambda q, r: q["fill"].update(funding_asset="eth"),
    lambda q, r: q["fill"].update(network_fee_lamports=999),
    lambda q, r: q["fill"].update(priority_fee_lamports=6000),
    lambda q, r: q["fill"].update(rent_lamports=2_000_000),
])
def test_wrong_identity_partial_balances_or_fill_mismatch_remain_unknown(mutation):
    req, receipt = trade()
    mutation(req, receipt)
    with pytest.raises(ValueError):
        price(req, receipt)


@pytest.mark.parametrize("fill", [None, {"kind": "entry", "qty": 10, "notional": 25, "funding_asset": "sol"}])
def test_unbooked_success_or_unknown_native_principal_is_not_priced(fill):
    req, receipt = trade(native=True)
    req["fill"] = fill
    with pytest.raises(ValueError):
        price(req, receipt)


def test_known_at_precedes_receipt_is_unknown():
    with pytest.raises(ValueError):
        price(*trade(), known_at="2026-10-21T11:59:59+00:00")


def test_failed_receipt_cannot_value_a_contradictory_successful_fill():
    req, receipt = trade()
    receipt["meta"]["err"] = {"InstructionError": [0, {"Custom": 6001}]}
    with pytest.raises(ValueError, match="fill"):
        price(req, receipt)


def test_non_json_receipt_has_a_typed_serialization_refusal():
    req, receipt = trade()
    receipt["untrusted_extra"] = float("nan")
    with pytest.raises(ValueError, match="^receipt_serialization_invalid$"):
        price(req, receipt)


def test_nonzero_expense_cannot_underflow_into_a_proven_zero():
    with pytest.raises(ValueError, match="^usd_amount_invalid$"):
        price(*trade(), usd_quote={**FX, "price": 5e-324})


def test_jsonparsed_lookup_keys_are_not_appended_or_rewritten():
    req, receipt = trade()
    receipt["transaction"]["message"]["accountKeys"][2]["source"] = "lookupTable"
    receipt["meta"]["loadedAddresses"] = {"writable": [USDC], "readonly": []}
    before = copy.deepcopy(receipt)
    assert price(req, receipt)["execution_usd"] == pytest.approx(.00125)
    assert receipt == before


def test_receipt_quantity_matches_the_actual_writers_float_round_trip():
    req, receipt = trade()
    atoms = 8_692_308_924_080_165
    receipt["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = str(atoms)
    req["fill"]["qty"] = atoms / 10 ** 6
    out = price(req, receipt)
    assert out["execution_usd"] == pytest.approx(.00125)
    assert out["refund_usd"] == 0


def test_surplus_credited_slot_notional_remains_unsupported_instead_of_counted_twice():
    req, receipt = trade()
    req["fill"]["notional"] = 24
    # Slot settlement credited a SOL surplus against the actual 25-USDC trade cost.
    receipt["meta"]["postBalances"][0] += 4_000_000
    with pytest.raises(ValueError, match="fill_notional_mismatch"):
        price(req, receipt)
