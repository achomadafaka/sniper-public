"""The on-chain watcher values PumpSwap buy_exact_quote_in_v2 sources (2026-10-08).

Before: value_source_buy refused the v2 name (unsupported_buy_instruction), and its instruction binding
read the v1 layout (token programs at 11/12). The v2 instruction has 17 accounts with the token
programs at 9/10, so every v2 source (all sampled ones came through the FLASHX8D router) had no size and
no price. Real receipts: tests/fixtures/memecoin/pumpswap_v2_receipts_20261008.json."""
import base64
import copy
import hashlib
import json
from pathlib import Path

import base58
import pytest

from sniper.execution import pumpswap_buy as ps
from sniper.services import memecoin_source_valuation as v

FIX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "pumpswap_v2_receipts_20261008.json").read_text())
V2 = hashlib.sha256(b"global:buy_exact_quote_in_v2").digest()[:8]
SOL = (110.377, 100.0)


def _receipt(name):
    row = FIX[name]
    return copy.deepcopy(row["tx"]), row["owner"]


def _event(tx):
    for line in tx["meta"]["logMessages"]:
        if line.startswith("Program data: "):
            try:
                return ps.parse_buy_event(line)
            except ValueError:
                pass


def _v2_ix(tx):
    keys = v._account_keys(tx)
    found = [ix for group in tx["meta"]["innerInstructions"] for ix in group["instructions"]
             if keys[ix["programIdIndex"]] == ps.AMM_PROGRAM and base58.b58decode(ix["data"])[:8] == V2]
    assert len(found) == 1
    return found[0], keys


@pytest.mark.parametrize("name", ["tardigrade", "v2_second"])
def test_a_v2_source_is_valued_from_its_event(name):
    tx, owner = _receipt(name)
    ev = _event(tx)
    ix, keys = _v2_ix(tx)
    assert len(ix["accounts"]) == 17
    facts = v.value_source_buy(tx, owner, sol_usd=SOL, now=105.0)
    net = ev["quote_amount_in_with_lp_fee"] - ev["lp_fee"]
    assert facts.get("unknown_reason") is None, facts
    assert facts["instruction_name"] == "buy_exact_quote_in_v2"
    assert facts["token_amount_atomic"] == str(ev["base_amount_out"])
    assert facts["token_program"] == keys[ix["accounts"][9]]
    if name == "tardigrade":
        # The receipt pays USDC into a first swap; the final event's WSOL is an intermediate hop.
        assert facts["basis"] == "route_owner_quote_out"
        assert facts["route_payment_atomic"] == "750000000"
        assert facts["quote_out_atomic"][v.USDC] == "750000000"
        assert facts["source_size_usd"] == pytest.approx(750.0)
        assert "sol_usd" not in facts
    else:
        assert facts["basis"] == "pump_event_net_sol"
        assert facts["gross_quote_atomic"] == str(ev["quote_amount_in"])
        assert facts["net_pool_quote_atomic"] == str(net)
        assert facts["source_size_usd"] == pytest.approx(net / 1e9 * SOL[0])


def test_tardigrade_is_the_omakase_buy_that_refused():
    tx, owner = _receipt("tardigrade")
    facts = v.value_source_buy(tx, owner, sol_usd=SOL, now=105.0)
    assert tx["transaction"]["signatures"][0].startswith("653VF487")
    assert facts["mint"] == "DXvY5Ep1j9Eqwf6qHkemgWgBsEfhEKDsSizDjT4ipump"
    assert (facts["token_decimals"], facts["token_program"]) == (6, ps.TOKEN_2022_PROGRAM)
    assert facts["source_size_usd"] == pytest.approx(750.0)


@pytest.mark.parametrize("damage", ["accounts", "data", "negative_index"])
def test_a_raw_usdc_payment_with_malformed_instruction_stays_unknown(damage):
    tx, owner = _receipt("tardigrade")
    payment = next(ix for group in tx["meta"]["innerInstructions"] for ix in group["instructions"]
                   if (data := base58.b58decode(ix["data"]))[:1] == b"\x0c"
                   and int.from_bytes(data[1:9], "little") == 750_000_000)
    if damage == "negative_index":
        payment["accounts"][0] = -1
    else:
        payment[damage] = None
    facts = v.value_source_buy(tx, owner, sol_usd=SOL, now=105.0)
    assert facts["source_size_usd"] is None, facts
    assert facts["source_price"] is None, facts
    assert facts["unknown_reason"] == ("instruction_index_invalid" if damage == "negative_index"
                                       else "instruction_metadata_invalid")


@pytest.mark.parametrize("tag", [4, 13])
def test_valid_unrelated_raw_spl_approval_preserves_native_sol_valuation(tag):
    tx, owner = _receipt("v2_second")
    before = v.value_source_buy(tx, owner, sol_usd=SOL, now=105.0)
    buy, keys = _v2_ix(tx)
    accounts = [buy["accounts"][5], buy["accounts"][0], buy["accounts"][1]]
    data = bytes([tag]) + (1).to_bytes(8, "little")
    if tag == 13:
        accounts.insert(1, buy["accounts"][3])
        data += bytes([before["token_decimals"]])
    tx["transaction"]["message"]["instructions"].append(dict(
        programIdIndex=buy["accounts"][9], accounts=accounts,
        data=base58.b58encode(data).decode()))
    after = v.value_source_buy(tx, owner, sol_usd=SOL, now=105.0)
    assert after.get("unknown_reason") is None, after
    assert after["basis"] == before["basis"] == "pump_event_net_sol"
    assert after["source_size_usd"] == before["source_size_usd"]
    assert after["source_price"] == before["source_price"]


def test_unrelated_malformed_parsed_container_stays_typed_unknown():
    tx, owner = _receipt("v2_second")
    tx["transaction"]["message"]["instructions"].append(dict(
        programId="unrelated-router", parsed=["bad"]))
    facts = v.value_source_buy(tx, owner, sol_usd=SOL, now=105.0)
    assert facts["source_size_usd"] is None, facts
    assert facts["source_price"] is None, facts
    assert facts["unknown_reason"] == "instruction_metadata_invalid"


@pytest.mark.parametrize("position", [9, 10])
def test_a_v2_token_program_in_the_wrong_slot_is_unknown(position):
    tx, owner = _receipt("tardigrade")
    ix, keys = _v2_ix(tx)
    ix["accounts"][position] = keys.index(ps.SYSTEM_PROGRAM)
    facts = v.value_source_buy(tx, owner, sol_usd=SOL, now=105.0)
    assert facts["source_size_usd"] is None
    assert facts["unknown_reason"] == "quote_or_instruction_identity"


def test_an_unlisted_buy_name_stays_unknown():
    tx, owner = _receipt("tardigrade")
    for i, line in enumerate(tx["meta"]["logMessages"]):
        if line.startswith("Program data: "):
            try:
                ps.parse_buy_event(line)
            except ValueError:
                continue
            raw = base64.b64decode(line.split(": ", 1)[1]).replace(b"buy_exact_quote_in_v2", b"buy_exact_quote_in_v3")
            tx["meta"]["logMessages"][i] = "Program data: " + base64.b64encode(raw).decode()
    assert _event(tx)["ix_name"] == "buy_exact_quote_in_v3"
    facts = v.value_source_buy(tx, owner, sol_usd=SOL, now=105.0)
    assert (facts["source_size_usd"], facts["unknown_reason"]) == (None, "unsupported_buy_instruction")
    assert facts["instruction_name"] == "buy_exact_quote_in_v3"
    assert facts["program_id"] == ps.AMM_PROGRAM
    assert facts["pool"] == _event(tx)["pool"]
    diagnostic = facts["unsupported_instruction_evidence"]
    ix, keys = _v2_ix(tx)
    assert diagnostic["account_positions"] == [keys[a] for a in ix["accounts"]]
    assert diagnostic["instruction_discriminator"] == V2.hex()
    assert diagnostic["event_log_index"] == facts["event_log_index"]


def test_exact_100xbroker_buy_v2_receipt_is_bound_and_valued():
    incident = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "pumpswap_buy_v2_100xbroker_20261011.json").read_text())
    tx, owner = incident['tx'], incident['owner']
    assert tx['version'] == 1 and tx['slot'] == 455575152
    assert tx['transaction']['signatures'][0] == '2vcqP5A3PJpGkeQZyQzgcCCgimJyFVQAugC2LvjfbaaCC2DHc4CJeVMCW9BzRmdp84EXyvPRhvvmQSoN2e9PRmP1'
    facts = v.value_source_buy(tx, owner, sol_usd=SOL, now=105.0)
    assert facts.get('unknown_reason') is None, facts
    assert facts['instruction_name'] == 'buy_v2'
    assert facts['mint'] == 'Fr28jEWz2msSCKLaHzus2nCKrwqbpTx2EyDYtsVVpump'
    assert facts['source_size_usd'] > 0
    from sniper.services.memecoin_source_initiation import solana_receipt_initiation
    assert solana_receipt_initiation(tx, owner, facts)['payer'] == owner
    damaged = copy.deepcopy(tx)
    for key in damaged['transaction']['message']['accountKeys']:
        if isinstance(key, dict) and key['pubkey'] == owner:
            key['signer'] = False
    assert solana_receipt_initiation(damaged, owner, facts) is None


@pytest.mark.parametrize('damage', ['wrong_token_program', 'wrong_actor', 'wrong_pool', 'wrong_discriminator'])
def test_exact_buy_v2_cannot_borrow_an_unrelated_instruction(damage):
    incident = json.loads((Path(__file__).parent / 'fixtures' / 'memecoin' / 'pumpswap_buy_v2_100xbroker_20261011.json').read_text())
    tx, owner = incident['tx'], incident['owner']
    keys = v._account_keys(tx)
    disc = hashlib.sha256(b'global:buy_v2').digest()[:8]
    instructions = tx['transaction']['message']['instructions'] + [ix for group in tx['meta']['innerInstructions'] for ix in group['instructions']]
    bound = [ix for ix in instructions if (ix.get('programId') or keys[ix['programIdIndex']]) == ps.AMM_PROGRAM
             and base58.b58decode(ix.get('data', ''))[:8] == disc]
    assert len(bound) == 1
    ix = bound[0]
    if damage == 'wrong_discriminator':
        ix['data'] = base58.b58encode(b'\x00' * 8 + base58.b58decode(ix['data'])[8:]).decode()
    else:
        position = {'wrong_token_program': 9, 'wrong_actor': 1, 'wrong_pool': 0}[damage]
        ix['accounts'][position] = keys.index(ps.SYSTEM_PROGRAM)
    facts = v.value_source_buy(tx, owner, sol_usd=SOL, now=105.0)
    assert facts['source_size_usd'] is None
    assert facts['unknown_reason'] == 'quote_or_instruction_identity'
    from sniper.services.memecoin_source_initiation import solana_receipt_initiation
    assert solana_receipt_initiation(tx, owner, facts) is None
