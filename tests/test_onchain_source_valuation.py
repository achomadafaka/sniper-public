"""Confirmed source principal is event-bound; wallet debits include unrelated costs."""
import base64
import json
from pathlib import Path

import pytest

from sniper.services.memecoin_onchain_lane import OnchainEv


@pytest.mark.parametrize("value, expected", [(None, None), ("nan", None), ("inf", None),
                                             ("bad", None), (False, None), (0, 0), ("250", 250)])
def test_event_preserves_unknown_and_explicit_zero(value, expected):
    ev = OnchainEv(dict(mint="m", source_user_id="u", handle="h", network_id="solana",
                       source_size_usd=value, source_price=value, signature="s", detected_at="t"))
    assert ev.their_size_usd == expected
    assert ev.their_price == expected


def receipt(name):
    tx = json.loads((Path(__file__).parent / "fixtures/memecoin/confirmed_source_buys.json").read_text())[name]
    owner = tx["meta"]["postTokenBalances"][0]["owner"]
    return tx, owner


@pytest.mark.parametrize("name,gross,net", [("untaxxable", 20_000_000, 19_753_085),
                                          ("notdecu-later", 24_750_001, 24_444_444)])
def test_actual_principal_excludes_tips_rent_and_different_fee_payer(name, gross, net):
    from sniper.services.memecoin_source_valuation import value_source_buy
    tx, owner = receipt(name)
    facts = value_source_buy(tx, owner, sol_usd=(120.0, 100.0), now=105.0)
    assert facts["source_size_usd"] == pytest.approx(net / 1e9 * 120)
    assert facts["gross_swap_size_usd"] == pytest.approx(gross / 1e9 * 120)
    assert facts["gross_quote_atomic"] == str(gross)
    assert facts["net_pool_quote_atomic"] == str(net)
    assert facts["token_decimals"] == 6  # Token-2022 receipt
    assert facts["source_slot"] == tx["slot"]
    assert facts["source_block_time"] == tx["blockTime"]
    assert facts["sol_usd_age_s"] == 5.0
    tx["meta"]["fee"] = 999_999_999
    tx["meta"]["preBalances"] = [10**15] * len(tx["meta"]["preBalances"])
    # Rewriting every pre-lamport balance destroys untaxxable's new temporary-account proof;
    # inventory becomes unknown, while principal and price remain independent of wallet debits.
    expected_pre = None if name == 'untaxxable' else facts['source_pre_atomic']
    assert value_source_buy(tx, owner, sol_usd=(120, 100), now=105) == dict(
        facts, source_pre_atomic=expected_pre)


@pytest.mark.parametrize("price", [None, (120, 0), (120, 110), (float("nan"), 100)])
def test_missing_stale_future_or_invalid_price_is_unknown(price):
    from sniper.services.memecoin_source_valuation import value_source_buy
    tx, owner = receipt("notdecu-later")
    facts = value_source_buy(tx, owner, sol_usd=price, now=105)
    assert facts["source_size_usd"] is None
    assert facts["gross_quote_atomic"] == "24750001"


def test_stale_observation_keeps_its_timestamp_and_age_for_audit():
    from sniper.services.memecoin_source_valuation import value_source_buy
    tx, owner = receipt("notdecu-later")
    facts = value_source_buy(tx, owner, sol_usd=(120, 1), now=105)
    assert facts["source_size_usd"] is None
    assert facts["sol_usd_observed_at"] == 1
    assert facts["sol_usd_age_s"] == 104


@pytest.mark.parametrize("damage", ["owner", "mint", "decimals", "amount", "multi", "wrong_quote", "truncated_second_event"])
def test_ambiguous_or_mismatched_receipt_is_unknown(damage):
    from sniper.services.memecoin_source_valuation import value_source_buy
    tx, owner = receipt("untaxxable")
    if damage == "owner":
        owner = "not-the-trader"
    elif damage == "multi":
        tx["meta"]["logMessages"] *= 2
    elif damage == "truncated_second_event":
        logs = tx["meta"]["logMessages"]
        index = next(i for i, line in enumerate(logs) if line.startswith("Program data: Z/RSHyz1d3"))
        logs.insert(index + 1, "Program data: " + base64.b64encode(bytes.fromhex("67f4521f2cf57777")).decode())
    elif damage == "wrong_quote":
        tx["transaction"]["message"]["instructions"][2]["accounts"][4] = "wrong"
    elif damage == "mint":
        tx["meta"]["postTokenBalances"][0]["mint"] = "wrong"
    else:
        tx["meta"]["postTokenBalances"][0]["uiTokenAmount"][damage] = 7 if damage == "decimals" else "7"
    assert value_source_buy(tx, owner, sol_usd=(120, 100), now=105)["source_size_usd"] is None


@pytest.mark.parametrize("quote,valid", [(bytes(32), True), (bytes([1]) * 32, False)])
def test_curve_quote_identity_is_read_from_full_event(quote, valid):
    from sniper.services.memecoin_source_valuation import value_source_buy
    tx, owner = receipt("notdecu-later")
    for i, line in enumerate(tx["meta"]["logMessages"]):
        if line.startswith("Program data: vdt/"):
            raw = base64.b64decode(line.split(": ", 1)[1])
            raw = raw[:-72] + quote + raw[-40:]
            tx["meta"]["logMessages"][i] = "Program data: " + base64.b64encode(raw).decode()
    facts = value_source_buy(tx, owner, sol_usd=(120, 100), now=105)
    assert (facts["source_size_usd"] is not None) is valid


def rick_receipt():
    """RICK 456550933: signer paid USDC; the sole Pump event names the WSOL hop."""
    tx = json.loads((Path(__file__).parent /
                     "fixtures/memecoin/rick_usdc_pumpswap_buy.json").read_text())
    return tx, "4CRX74nxAdmFY4Eh1WhTrmYqHoxwxGzwXfgzTWvhUfFn"


@pytest.mark.parametrize("sol_usd", [None, (1.0, 100.0), (1000.0, 100.0)])
def test_usdc_funded_pump_event_uses_bound_route_payment_without_sol_usd(sol_usd):
    from sniper.services.memecoin_source_valuation import value_source_buy
    from sniper.services.memecoin_source_initiation import solana_receipt_initiation, USDC
    tx, owner = rick_receipt()
    facts = value_source_buy(tx, owner, sol_usd=sol_usd, now=105.0)
    assert facts["source_size_usd"] == pytest.approx(995.0), facts  # $5 router fee excluded
    assert facts["source_price"] == pytest.approx(995.0 / 19_381_686.474016)
    assert facts["basis"] == "route_owner_quote_out"
    assert facts["quote_out_atomic"][USDC] == "1000000000"
    assert facts["source_post_price"] == pytest.approx(
        facts["source_price"] * facts["source_impact_ratio"])
    assert facts["source_slot"] == 456550933
    assert facts["source_block_time"] == tx["blockTime"]
    assert "sol_usd" not in facts
    proof = solana_receipt_initiation(tx, owner, facts)
    assert proof["route_payment_atomic"] == "995000000"
    assert proof["acquired_atomic"] == "19381686474016"
    assert proof["tx_hash"] == tx["transaction"]["signatures"][0]


@pytest.mark.parametrize("damage", ["call_tree", "authority", "non_quote_funding", "debit_mismatch",
                                    "balance_row", "payment_decimals", "payment_owner",
                                    "instruction_decimals", "instruction_program"])
def test_usdc_funded_pump_event_cannot_borrow_an_unbound_sol_price(damage):
    from sniper.services.memecoin_source_valuation import value_source_buy, USDC
    tx, owner = rick_receipt()
    if damage == "call_tree":
        for group in tx["meta"]["innerInstructions"]:
            for ix in group["instructions"]:
                ix.pop("stackHeight", None)
    elif damage == "authority":
        for group in tx["meta"]["innerInstructions"]:
            for ix in group["instructions"]:
                info = (ix.get("parsed") or {}).get("info") or {}
                if info.get("mint") == USDC and info.get("authority") == owner:
                    info["authority"] = "unrelated-wallet"
    elif damage == "non_quote_funding":
        # An additional owned input mint cannot be hidden by the valid final Pump event.
        row = dict(tx["meta"]["preTokenBalances"][0], owner=owner, mint="unrelated-funding",
                   accountIndex=0, uiTokenAmount=dict(amount="1", decimals=6))
        tx["meta"]["preTokenBalances"].append(row)
    elif damage == "balance_row":
        tx["meta"]["preTokenBalances"].append(None)
    elif damage in ("instruction_decimals", "instruction_program"):
        for group in tx["meta"]["innerInstructions"]:
            for ix in group["instructions"]:
                info = (ix.get("parsed") or {}).get("info") or {}
                if info.get("mint") == USDC and info.get("authority") == owner:
                    if damage == "instruction_decimals":
                        info["tokenAmount"]["decimals"] = 9
                    else:
                        ix.update(program="spl-token-2022",
                                  programId="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
    else:
        row = next(b for b in tx["meta"]["postTokenBalances"]
                   if b.get("owner") == owner and b.get("mint") == USDC)
        if damage == "debit_mismatch":
            row["uiTokenAmount"]["amount"] = str(int(row["uiTokenAmount"]["amount"]) + 1)
        elif damage == "payment_decimals":
            for balances in (tx["meta"]["preTokenBalances"], tx["meta"]["postTokenBalances"]):
                for b in balances:
                    if b.get("owner") == owner and b.get("mint") == USDC:
                        b["uiTokenAmount"]["decimals"] = 9
        else:
            row["owner"] = "unrelated-wallet"
    facts = value_source_buy(tx, owner, sol_usd=(120.0, 100.0), now=105.0)
    assert facts["source_size_usd"] is None, facts
    assert facts["source_price"] is None, facts
    assert facts.get("unknown_reason"), facts


def test_usdc_funded_pump_event_refund_cannot_raise_price_reference():
    from sniper.services.memecoin_source_valuation import value_source_buy, USDC, _account_keys
    tx, owner = rick_receipt()
    keys = _account_keys(tx)
    owned = next(b for b in tx["meta"]["postTokenBalances"]
                 if b.get("owner") == owner and b.get("mint") == USDC)
    refund_from = next(b for b in tx["meta"]["postTokenBalances"]
                       if b.get("owner") != owner and b.get("mint") == USDC
                       and int(b["uiTokenAmount"]["amount"]) >= 10_000_000)
    owned["uiTokenAmount"]["amount"] = str(int(owned["uiTokenAmount"]["amount"]) + 10_000_000)
    refund_from["uiTokenAmount"]["amount"] = str(int(refund_from["uiTokenAmount"]["amount"]) - 10_000_000)
    tx["transaction"]["message"]["instructions"].append(dict(
        program="spl-token", programId="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        parsed=dict(type="transferChecked", info=dict(
            source=keys[refund_from["accountIndex"]], destination=keys[owned["accountIndex"]],
            authority=refund_from["owner"], mint=USDC,
            tokenAmount=dict(amount="10000000", decimals=6)))))
    facts = value_source_buy(tx, owner, sol_usd=None, now=105.0)
    assert facts["source_size_usd"] == pytest.approx(990.0), facts
    assert facts["source_price"] == pytest.approx(990.0 / 19_381_686.474016)
    assert facts["route_payment_atomic"] == "990000000"


@pytest.mark.parametrize("damage", ["balance_rows", "balance_owners"])
def test_usdc_payment_without_owned_balance_identity_cannot_use_sol_price(damage):
    from sniper.services.memecoin_source_valuation import value_source_buy, USDC
    tx, owner = rick_receipt()
    for side in ("preTokenBalances", "postTokenBalances"):
        if damage == "balance_rows":
            tx["meta"][side] = [b for b in tx["meta"][side]
                                 if (b.get("owner"), b.get("mint")) != (owner, USDC)]
        else:
            for row in tx["meta"][side]:
                if (row.get("owner"), row.get("mint")) == (owner, USDC):
                    row.pop("owner")
    facts = value_source_buy(tx, owner, sol_usd=(120.0, 100.0), now=105.0)
    assert facts["source_size_usd"] is None, facts
    assert facts["source_price"] is None, facts
    assert facts["unknown_reason"] == "route_usdc_payment_identity"


def test_usdc_funded_pump_event_cannot_add_unrelated_wsol_outflow_to_price():
    import base58
    from sniper.services.memecoin_source_valuation import value_source_buy, WSOL, _account_keys
    tx, owner = rick_receipt()
    keys = _account_keys(tx)
    account = base58.b58encode(bytes([100]) * 32).decode()
    destination = next(keys[b["accountIndex"]] for b in tx["meta"]["postTokenBalances"]
                       if b.get("mint") == WSOL and b.get("owner") != owner)
    index = len(keys)
    tx["transaction"]["message"]["accountKeys"].append(dict(pubkey=account, signer=False,
                                                              writable=True, source="transaction"))
    for side, amount in (("preTokenBalances", "1000000000"), ("postTokenBalances", "0")):
        tx["meta"][side].append(dict(accountIndex=index, owner=owner, mint=WSOL,
            programId="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            uiTokenAmount=dict(amount=amount, decimals=9)))
    tx["transaction"]["message"]["instructions"].append(dict(
        program="spl-token", programId="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        parsed=dict(type="transferChecked", info=dict(source=account, destination=destination,
            authority=owner, mint=WSOL, tokenAmount=dict(amount="1000000000", decimals=9)))))
    facts = value_source_buy(tx, owner, sol_usd=(120.0, 100.0), now=105.0)
    assert facts["source_size_usd"] is None, facts
    assert facts["source_price"] is None, facts
    assert facts["unknown_reason"] == "route_mixed_quote_funding"


def test_usdc_route_price_excludes_an_unrelated_usdc_gift():
    from sniper.services.memecoin_source_valuation import value_source_buy, USDC, _account_keys
    tx, owner = rick_receipt()
    keys = _account_keys(tx)
    owned = next(b for b in tx["meta"]["postTokenBalances"]
                 if (b.get("owner"), b.get("mint")) == (owner, USDC))
    destination = next(keys[b["accountIndex"]] for b in tx["meta"]["postTokenBalances"]
                       if b.get("mint") == USDC and b.get("owner") != owner)
    owned["uiTokenAmount"]["amount"] = str(int(owned["uiTokenAmount"]["amount"]) - 100_000_000)
    tx["transaction"]["message"]["instructions"].append(dict(
        program="spl-token", programId="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        parsed=dict(type="transferChecked", info=dict(source=keys[owned["accountIndex"]],
            destination=destination, authority=owner, mint=USDC,
            tokenAmount=dict(amount="100000000", decimals=6)))))
    facts = value_source_buy(tx, owner, sol_usd=None, now=105.0)
    assert facts["source_size_usd"] == pytest.approx(995.0), facts
    assert facts["route_payment_atomic"] == "995000000"
    assert facts["quote_out_atomic"][USDC] == "1100000000"
