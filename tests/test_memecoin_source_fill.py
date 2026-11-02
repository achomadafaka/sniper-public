"""Receipt economics stay bound to the wallet, not a partial pool swap."""
import importlib
import importlib.util
import inspect
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_evm_onchain_watch as decoder
import scripts.research.onchain_evm_wallet_watch as watcher


WALLET, MINT, QUOTE, POOL = ("0x" + byte * 20 for byte in ("11", "22", "33", "44"))
TX, BLOCK = "0x" + "55" * 32, "0x" + "66" * 32
UID = "wallet-uid"


def transfer(token, sender, recipient, amount):
    return dict(address=token, topics=[decoder.TRANSFER_TOPIC,
        "0x" + sender[2:].rjust(64, "0"), "0x" + recipient[2:].rjust(64, "0")],
        data=hex(amount))


def frame(*, split=False, native=False, mixed=False):
    tx = dict(hash=TX, blockHash=BLOCK, blockNumber="0x10", chainId=hex(4663),
              value=hex(100 * 10**18) if native else "0x0", **{"from": WALLET, "to": POOL})
    logs = [] if native and not mixed else [transfer(QUOTE, WALLET, POOL, 100 * 10**18)]
    # The pool pays 200 gross, while the wallet receives only 180; the rest is a pool-side fee.
    logs += [transfer(MINT, POOL, WALLET, 180 * 10**18),
             transfer(MINT, POOL, "0x" + "77" * 20, 20 * 10**18)]
    if split:
        other = "0x" + "88" * 20
        logs += [transfer(QUOTE, WALLET, other, 60 * 10**18),
                 transfer(MINT, other, WALLET, 120 * 10**18)]
    receipt = dict(transactionHash=TX, blockHash=BLOCK, blockNumber="0x10", status="0x1",
                   logs=logs, **{"from": WALLET, "to": POOL})
    return receipt, tx


def direct(*, split=False, native=False, mixed=False):
    receipt, tx = frame(split=split, native=native, mixed=mixed)
    buy = decoder.direct_buy_from_receipt(receipt, tx, WALLET, weth=QUOTE, quote_usd=1,
                                          decimals_fn=lambda _: 18, chain="4663")
    if buy is not None:
        buy["source_initiation"]["source_user_id"] = UID
    return buy


def signal():
    return dict(chain="4663", tx_hash=TX, wallet=WALLET, source_user_id=UID, mint=MINT,
                seq=16, block_hash=BLOCK)


def proof():
    return dict(proof_version="wallet-net-fill-v1", receipt_proof_version="direct-wallet-paid-v1",
                network_id="4663", tx_hash=TX, block_hash=BLOCK, block_number=16, wallet=WALLET,
                source_user_id=UID, mint=MINT, payment_token=QUOTE, payment_atomic=str(100 * 10**18),
                acquired_atomic=str(180 * 10**18), payment_decimals=18, token_decimals=18, paid_usd=100.,
                payment_basis="wallet_quote_outflow")


def validator():
    name = "sniper.services.memecoin_source_fill"
    assert importlib.util.find_spec(name) is not None, "receipt-bound source-fill validator missing"
    return importlib.import_module(name).validate_source_fill


class Recorder:
    def execute(self, sql, values):
        self.row = dict(zip(sql.split("(", 1)[1].split(")", 1)[0].split(","), values))


def record(buy, **overrides):
    conn = Recorder()
    args = dict(tx_hash=TX, wallet=WALLET, source_user_id=UID, handle="fixture", buy=buy,
                log=dict(transactionHash=TX, blockHash=BLOCK, blockNumber="0x10"),
                now="2026-10-04T23:00:00+00:00", valuation=dict(price_source="fixture"))
    args.update(overrides)
    watcher._record(conn, **args)
    return json.loads(conn.row["source_valuation_json"])


def test_direct_producer_uses_net_wallet_atoms_after_pool_fee():
    buy = direct()
    assert buy.get("acquired_atomic") == str(180 * 10**18)
    assert buy.get("payment_atomic") == str(100 * 10**18)
    assert buy.get("payment_token") == QUOTE
    assert buy.get("payment_decimals") == buy.get("token_decimals") == 18
    assert buy["source_price"] == pytest.approx(100 / 180)
    stored = record(buy)
    assert stored["source_fill"] == proof()
    assert stored["price_source"] == "fixture"


def test_split_receipt_producer_stores_whole_wallet_net():
    stored = record(direct(split=True))["source_fill"]
    assert stored["acquired_atomic"] == str(300 * 10**18)
    assert stored["payment_atomic"] == str(160 * 10**18)
    assert stored["paid_usd"] == 160.


def test_direct_native_payment_is_identified_without_another_read():
    stored = record(direct(native=True))["source_fill"]
    assert stored["payment_token"] == "0x" + "00" * 20
    assert stored["payment_atomic"] == str(100 * 10**18)
    assert stored.get("payment_basis") == "transaction_value_upper_bound"


def test_mixed_direct_currencies_never_emit_an_understated_source_price():
    assert direct(native=True, mixed=True) is None


def test_relay_tax_receipt_keeps_exact_decoder_wallet_output():
    fixture = json.loads((Path(__file__).parent / "fixtures/memecoin/wisp_relay_tax_20261004.json").read_text())
    receipt_proof = decoder.relayer_purchase_from_receipt(fixture["receipt"], fixture["transaction"],
        fixture["watched"][0], chain="56", contracts_verified=True, any_route=True)
    assert receipt_proof is not None
    buy = decoder.price_relayer_purchase(receipt_proof, decimals_fn=lambda _: 18, price_fn=lambda _: 1.)
    assert buy.get("acquired_atomic") == "27170417015396852807106572"
    assert buy.get("payment_atomic") == "3966977369000000000000"
    assert buy.get("payment_token") == receipt_proof["payment_token"]
    assert buy.get("payment_decimals") == buy.get("token_decimals") == 18


@pytest.mark.parametrize("key,value", [
    ("source_user_id", "another-uid"), ("tx_hash", "0x" + "99" * 32),
    ("network_id", "56"), ("block_number", 17), ("block_hash", "0x" + "99" * 32),
    ("mint", "0x" + "99" * 20), ("beneficiary", "0x" + "99" * 20),
    ("proof_version", "unverified"), ("block_hash", None),
    ("schema_version", True), ("schema_version", 2), ("payer", None),
    ("payer", "0x" + "99" * 20),
])
def test_record_retains_observation_but_abstains_on_wrong_initiation(key, value):
    buy = direct()
    buy["source_initiation"][key] = value
    assert "source_fill" not in record(buy)


@pytest.mark.parametrize("key,value", [
    ("transactionHash", "0x" + "99" * 32), ("blockHash", "0x" + "99" * 32),
    ("blockNumber", "0x11"), ("blockHash", None), ("transactionHash", None),
])
def test_record_requires_same_complete_transfer_receipt_identity(key, value):
    log = dict(transactionHash=TX, blockHash=BLOCK, blockNumber="0x10")
    log[key] = value
    assert "source_fill" not in record(direct(), log=log)


def test_legacy_buy_and_unknown_initiation_remain_observations():
    buy = direct()
    buy.pop("source_initiation")
    assert "source_fill" not in record(buy)
    assert record(dict(mint=MINT, token_delta=180, source_size_usd=100, source_price=100/180)) == {
        "price_source": "fixture"}


def test_validator_preserves_exact_text_and_only_bound_fields():
    p, s = proof(), signal()
    p["unused"] = "not retained"
    result = validator()(p, s)
    assert result == proof()
    assert result is not p and p["unused"] == "not retained"
    assert validator()(proof(), {k: v for k, v in s.items() if k != "chain"}) == proof()


@pytest.mark.parametrize("key,value", [
    ("proof_version", "v0"), ("receipt_proof_version", "unverified"),
    ("receipt_proof_version", []), ("receipt_proof_version", {}),
    ("network_id", "robinhood"), ("network_id", "04663"), ("network_id", "56"),
    ("source_user_id", None), ("source_user_id", "other"), ("wallet", "0x" + "99" * 20),
    ("mint", "0x" + "99" * 20), ("tx_hash", "0x" + "99" * 32),
    ("block_hash", "0x" + "99" * 32), ("block_hash", None), ("block_number", 17),
    ("block_number", "16"), ("block_number", True), ("payment_token", None),
    ("payment_atomic", 100), ("payment_atomic", "0"), ("payment_atomic", "01"),
    ("acquired_atomic", "1.5"), ("acquired_atomic", "+1"), ("acquired_atomic", None),
    ("payment_decimals", True), ("token_decimals", -1), ("token_decimals", 37),
    ("token_decimals", "18"), ("paid_usd", None), ("paid_usd", 0), ("paid_usd", True),
    ("paid_usd", "100"), ("paid_usd", float("nan")), ("paid_usd", float("inf")),
])
def test_validator_abstains_on_malformed_or_mismatched_proof(key, value):
    p = proof()
    p[key] = value
    assert validator()(p, signal()) is None


@pytest.mark.parametrize("bad", [None, True, [], "{}"])
def test_validator_abstains_on_non_objects(bad):
    assert validator()(bad, signal()) is None
    assert validator()(proof(), bad) is None


def test_arc_erc20_quote_uses_configured_six_decimals_without_reads():
    receipt, tx = frame()
    tx["chainId"] = hex(5042)
    receipt["logs"][0]["data"] = hex(100 * 10**6)
    assert "quote_decimals" in inspect.signature(decoder.direct_buy_from_receipt).parameters
    buy = decoder.direct_buy_from_receipt(receipt, tx, WALLET, weth=QUOTE, quote_usd=1.,
                                          decimals_fn=lambda _: 18, chain="5042", quote_decimals=6)
    assert buy["source_size_usd"] == 100.
    assert buy["source_price"] == pytest.approx(100/180)
    assert buy["payment_atomic"] == str(100 * 10**6)
    assert buy["payment_decimals"] == 6


def test_arc_native_transaction_value_keeps_evm_eighteen_decimals():
    receipt, tx = frame(native=True)
    tx["chainId"] = hex(5042)
    assert "quote_decimals" in inspect.signature(decoder.direct_buy_from_receipt).parameters
    buy = decoder.direct_buy_from_receipt(receipt, tx, WALLET, weth=QUOTE, quote_usd=1.,
                                          decimals_fn=lambda _: 18, chain="5042", quote_decimals=6)
    assert buy["source_size_usd"] == 100.
    assert buy["payment_decimals"] == 18


def test_arc_watcher_wires_quote_metadata_with_only_existing_receipt_reads():
    spec = importlib.util.spec_from_file_location("arc_source_fill_watcher", watcher.__file__)
    local = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(local)
    local._configure("5042")
    receipt, tx = frame()
    tx["chainId"] = hex(5042)
    receipt["logs"][0].update(address=local.WETH, data=hex(100 * 10**6))
    calls = []
    def read(method, params):
        calls.append((method, params))
        if method == "eth_getTransactionByHash":
            return tx
        if method == "eth_getTransactionReceipt":
            return receipt
        raise AssertionError("extra provider read")
    try:
        row = local.push_detection("unused", dict(transactionHash=TX, address=MINT),
            {WALLET: (UID, "fixture")}, decimals_cache={MINT: 18}, quote_usd=1.,
            now="2026-10-04T23:00:00+00:00", read_fn=read)
    finally:
        for pool in local._RECEIPT_READER:
            pool.shutdown(wait=True)
    assert row["buy"]["source_size_usd"] == 100.
    assert row["buy"]["payment_decimals"] == 6
    assert sorted(method for method, _ in calls) == ["eth_getTransactionByHash", "eth_getTransactionReceipt"]


def source_post():
    return dict(venue="v4", pool="0x" + "99" * 32, ratio=1.1, mint_is_c1=True,
                sqrt_price_x96=str(2**96), pool_input_atomic=str(100 * 10**18),
                pool_output_atomic=str(200 * 10**18), receipt_routes_complete=True)


def test_record_and_validator_preserve_complete_source_post():
    buy = direct()
    buy["source_post"] = source_post()
    stored = record(buy)["source_fill"]
    assert stored["source_post"] == source_post()
    assert validator()(stored, signal()) == stored


def test_split_source_post_is_typed_measurement_without_native_permission():
    p = dict(proof(), source_post=dict(reason="split_route"))
    assert validator()(p, signal()) == p


@pytest.mark.parametrize("key,value", [
    ("pool", POOL), ("venue", "invented"), ("mint_is_c1", 1),
    ("sqrt_price_x96", None), ("sqrt_price_x96", "0"),
    ("pool_input_atomic", 100), ("pool_output_atomic", "-1"), ("ratio", float("nan")),
    ("receipt_routes_complete", None), ("receipt_routes_complete", 1),
])
def test_validator_rejects_malformed_source_post(key, value):
    post = source_post()
    post[key] = value
    assert validator()(dict(proof(), source_post=post), signal()) is None


def test_receipt_source_post_retains_last_same_pool_sqrt_and_buy_atoms():
    from sniper.services import memecoin_evm_post_trade as post_decoder
    fixture = json.loads((Path(__file__).parent / "fixtures/memecoin/evm_post_trade_receipts.json").read_text())
    case = fixture["cases"]["v4_hook_sells_after_buy"]
    post = post_decoder.source_post_trade("4663", case["receipt"], case["mint"], owner=case["trader"],
        pool_key_fn=lambda chain, pool: fixture["v4_keys"].get(f"{chain}:{pool}"))
    swaps = [log for log in case["receipt"]["logs"] if log["topics"][0] == post_decoder.V4_SWAP
             and log["topics"][1].lower() == post["pool"]]
    assert len(swaps) == 3
    words = lambda log: [log["data"][2:][i:i+64] for i in range(0, len(log["data"])-2, 64)]
    assert post.get("sqrt_price_x96") == str(int(words(swaps[-1])[2], 16))
    assert post.get("mint_is_c1") is False
    assert post.get("pool_output_atomic") == str(int(words(swaps[0])[0], 16))
    raw_input = int(words(swaps[0])[1], 16) - 2**256
    assert post.get("pool_input_atomic") == str(-raw_input)


def test_record_rejects_relay_net_atoms_that_conflict_with_initiation():
    buy = direct()
    buy["receipt_proof_version"] = buy["source_initiation"]["proof_version"] = "relay-envelope-linked-v1"
    buy["source_initiation"]["acquired_atomic"] = "1"
    assert "source_fill" not in record(buy)


def test_validator_rejects_conflicting_explicit_chain_bindings():
    assert validator()(proof(), dict(signal(), network_id="56")) is None


def test_missing_atomic_quantity_is_unknown_without_removing_observation():
    buy = direct()
    buy.pop("payment_atomic")
    stored = record(buy)
    assert "source_fill" not in stored and "source_initiation" in stored


def test_unknown_v4_hop_does_not_claim_complete_unique_source_pool():
    from sniper.services import memecoin_evm_post_trade as post_decoder
    fixture = json.loads((Path(__file__).parent / "fixtures/memecoin/evm_post_trade_receipts.json").read_text())
    case = fixture["cases"]["v4_multihop_bridge"]
    actual = post_decoder.source_post_trade("4663", case["receipt"], case["mint"], owner=case["trader"],
        pool_key_fn=lambda chain, pool: fixture["v4_keys"].get(f"{chain}:{pool}"))
    post = post_decoder.source_post_trade("4663", case["receipt"], case["mint"], owner=case["trader"],
        pool_key_fn=lambda chain, pool: fixture["v4_keys"].get(f"{chain}:{pool}") if pool == actual["pool"] else None)
    # The ordinary ratio survives; native source-fill proof cannot call this a complete unique route.
    assert post["ratio"] == actual["ratio"]
    assert post.get("receipt_routes_complete") is False
    assert validator()(dict(proof(), source_post=post), signal())["source_post"] == {"reason": "v4_key_unknown"}


@pytest.mark.parametrize("offsetting_inflow,native", [(False,False), (True,False), (False,True)])
def test_unexplained_quote_outflow_keeps_observation_without_complete_fill(offsetting_inflow, native):
    receipt, tx = frame(native=native)
    fee = "0x" + "ab" * 20
    receipt["logs"].append(transfer(QUOTE, WALLET, fee, 10 * 10**18))
    if offsetting_inflow:
        receipt["logs"].append(transfer(QUOTE, "0x" + "cd" * 20, WALLET, 10 * 10**18))
    buy = decoder.direct_buy_from_receipt(receipt, tx, WALLET, weth=QUOTE, quote_usd=1.,
                                        decimals_fn=lambda _: 18, chain="4663")
    assert buy and buy["source_size_usd"] == 100.
    stored = record(buy)
    assert "source_fill" not in stored
    assert stored.get("source_fill_reason") == "unexplained_quote_outflow"


def test_linked_quote_refund_is_an_exact_wallet_outflow():
    receipt, tx = frame()
    receipt["logs"].append(transfer(QUOTE, POOL, WALLET, 10 * 10**18))
    buy = decoder.direct_buy_from_receipt(receipt, tx, WALLET, weth=QUOTE, quote_usd=1.,
                                        decimals_fn=lambda _: 18, chain="4663")
    stored = record(buy)["source_fill"]
    assert stored["payment_atomic"] == str(90 * 10**18)
    assert stored["paid_usd"] == 90.
    assert stored.get("payment_basis") == "wallet_quote_outflow"


@pytest.mark.parametrize("basis", [None, "exact_native", "transaction_value_upper_bound", "relay_receipt_outflow", [], {}])
def test_direct_quote_proof_rejects_missing_or_wrong_payment_basis(basis):
    assert validator()(dict(proof(), payment_basis=basis), signal()) is None


def test_old_direct_proof_missing_payment_basis_is_unknown():
    p = proof()
    p.pop("payment_basis")
    assert validator()(p, signal()) is None


def test_existing_relay_proof_keeps_verified_receipt_payment_semantics():
    p = proof()
    p["receipt_proof_version"] = "relay-envelope-linked-v1"
    p.pop("payment_basis")
    stored = validator()(p, signal())
    assert stored.get("payment_basis") == "relay_receipt_outflow"
