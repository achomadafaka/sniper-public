"""Source fallback tests use only injected RPC fixtures."""
from copy import deepcopy
import importlib

import pytest

SLOT = 12570521870
HASH = "0x" + "aa" * 32
BLOCK = "0x" + "bb" * 32
WALLET = "0x" + "44" * 20
SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
UNIT = dict(chain="56", uid="wallet", address=WALLET, mint="0x" + "11" * 20,
            signature=HASH, source_slot=SLOT, t0=1000)


def wrapper(reader, unit=None, cached=None):
    module = importlib.import_module("sniper.services.wallet_universe_source")
    return module.SourceRead(unit or UNIT, reader, cached=cached)


class Reader:
    def __init__(self):
        self.calls = []
        self.error = "missing_result"
        self.header = dict(number=hex(SLOT), hash=BLOCK, timestamp=hex(1000))
        self.block = dict(self.header, transactions=[
            dict(hash="0x" + format(n, "064x"), transactionIndex=hex(n),
                 blockNumber=hex(SLOT), blockHash=BLOCK, **{"from": WALLET}, to="0x" + "66" * 20)
            for n in range(37)])
        self.block["transactions"][36]["hash"] = HASH
        self.logs = [dict(transactionHash=HASH, blockHash=BLOCK,
                          blockNumber=hex(SLOT), transactionIndex="0x24", logIndex="0x2e",
                          removed=False, address="0x" + "33" * 20,
                          topics=[SWAP], data="0x" + "00" * 128)]

    def __call__(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if method == "eth_getTransactionReceipt":
            if self.error:
                raise ValueError(self.error)
            return {"raw": True}
        if method == "eth_getBlockByNumber":
            assert params[0] == hex(SLOT)
            return deepcopy(self.block if params[1] else self.header)
        if method == "eth_getLogs":
            assert params == [{"blockHash": BLOCK}]
            return deepcopy(self.logs)
        return "forwarded"


def test_fallback_uses_actual_index_and_sender_and_is_labeled():
    reader = Reader()
    source = wrapper(reader)
    observation = source("eth_getTransactionReceipt", [HASH])
    assert observation == dict(transactionHash=HASH, blockNumber=hex(SLOT), blockHash=BLOCK,
                               transactionIndex="0x24", **{"from": WALLET}, to="0x" + "66" * 20,
                               logs=reader.logs, status="0x1",
                               observation_source="canonical_full_block_and_logs",
                               source_view_version="wallet-universe-source-view-v2",
                               source_unit_binding=UNIT)
    assert len(reader.calls) == 4
    observation["logs"].clear()
    assert source("eth_getTransactionReceipt", [HASH])["logs"]
    assert len(reader.calls) == 4
    assert source.cached["logs"]


def test_other_methods_and_receipts_pass_through():
    reader = Reader()
    reader.error = None
    source = wrapper(reader)
    assert source("eth_getTransactionReceipt", [HASH]) == {"raw": True}
    assert source.cached is None
    assert source("eth_getBalance", [WALLET, "latest"]) == "forwarded"
    assert source("eth_getTransactionReceipt", [BLOCK]) == {"raw": True}


@pytest.mark.parametrize("error", ["provider_timeout", "rate_limit", "provider_rejected", "ban", "not_missing_result"])
def test_provider_errors_never_trigger_fallback(error):
    reader = Reader()
    reader.error = error
    with pytest.raises(ValueError, match=error):
        wrapper(reader)("eth_getTransactionReceipt", [HASH])
    assert len(reader.calls) == 1


@pytest.mark.parametrize("slot", [True, "12570521870", -1, 1.5])
def test_malformed_source_slot_is_rejected_before_calls(slot):
    reader = Reader()
    with pytest.raises(ValueError):
        wrapper(reader, dict(UNIT, source_slot=slot))("eth_getTransactionReceipt", [HASH])
    assert reader.calls == []


@pytest.mark.parametrize("defect", ["hash_only", "hash_only_object", "wrong_other_index", "missing_source", "duplicate_source", "malformed_sender",
                                     "wrong_slot", "wrong_time", "wrong_index", "wrong_tx_block"])
def test_malformed_or_forged_full_block_fails_before_logs(defect):
    reader = Reader()
    tx = reader.block["transactions"][36]
    if defect == "hash_only": reader.block["transactions"][0] = BLOCK
    if defect == "hash_only_object": reader.block["transactions"][0] = dict(hash=BLOCK)
    if defect == "wrong_other_index": reader.block["transactions"][0]["transactionIndex"] = "0x1"
    if defect == "missing_source": tx["hash"] = BLOCK
    if defect == "duplicate_source": reader.block["transactions"][0]["hash"] = HASH
    if defect == "malformed_sender": tx["from"] = "0x55"
    if defect == "wrong_slot": reader.block["number"] = "0x1"
    if defect == "wrong_time": reader.block["timestamp"] = "0x1"
    if defect == "wrong_index": tx["transactionIndex"] = "0x1"
    if defect == "wrong_tx_block": tx["blockHash"] = HASH
    with pytest.raises(ValueError, match="source_"):
        wrapper(reader)("eth_getTransactionReceipt", [HASH])
    assert not any(method == "eth_getLogs" for method, _ in reader.calls)


@pytest.mark.parametrize("defect", ["empty", "foreign", "no_swap", "removed", "missing_removed",
                                     "wrong_block", "wrong_slot", "wrong_index", "duplicate", "bad_topic"])
def test_source_logs_must_prove_anchored_success(defect):
    reader = Reader()
    log = reader.logs[0]
    if defect == "empty": reader.logs = []
    if defect == "foreign": log["transactionHash"] = BLOCK
    if defect == "no_swap": log["topics"] = [HASH]
    if defect == "removed": log["removed"] = True
    if defect == "missing_removed": del log["removed"]
    if defect == "wrong_block": log["blockHash"] = HASH
    if defect == "wrong_slot": log["blockNumber"] = "0x1"
    if defect == "wrong_index": log["transactionIndex"] = "0x1"
    if defect == "duplicate": reader.logs.append(deepcopy(log))
    if defect == "bad_topic": log["topics"] = [123]
    with pytest.raises(ValueError, match="source_"):
        wrapper(reader)("eth_getTransactionReceipt", [HASH])


def test_reorg_during_fallback_is_rejected():
    reader = Reader()
    reader.header["hash"] = HASH
    with pytest.raises(ValueError, match="source_reorg"):
        wrapper(reader)("eth_getTransactionReceipt", [HASH])


def test_persisted_observation_revalidates_canonical_header_before_reuse():
    reader = Reader()
    first = wrapper(reader)
    first("eth_getTransactionReceipt", [HASH])
    restarted = Reader()
    source = wrapper(restarted, cached=first.cached)
    assert source("eth_getTransactionReceipt", [HASH]) == first.cached
    assert restarted.calls == [("eth_getBlockByNumber", [hex(SLOT), False])]
    restarted = Reader()
    restarted.header["hash"] = HASH
    with pytest.raises(ValueError, match="source_reorg"):
        wrapper(restarted, cached=first.cached)("eth_getTransactionReceipt", [HASH])


def test_malformed_cached_sender_does_not_bypass_validation():
    reader = Reader()
    source = wrapper(reader)
    source("eth_getTransactionReceipt", [HASH])
    cached = source.cached
    cached["from"] = "0x55"
    with pytest.raises(ValueError, match="source_"):
        wrapper(Reader(), cached=cached)("eth_getTransactionReceipt", [HASH])


def test_relay_sender_is_preserved_in_recovered_and_restored_observation():
    reader = Reader()
    relayer = "0x" + "55" * 20
    reader.block["transactions"][36]["from"] = relayer
    source = wrapper(reader)
    observation = source("eth_getTransactionReceipt", [HASH])
    assert observation["from"] == relayer
    assert observation["source_unit_binding"] == UNIT
    restarted = Reader()
    assert wrapper(restarted, cached=source.cached)("eth_getTransactionReceipt", [HASH]) == observation
    assert restarted.calls == [("eth_getBlockByNumber", [hex(SLOT), False])]


@pytest.mark.parametrize("field,value", [
    ("chain", "1"), ("uid", "different-wallet"), ("address", "0x" + "55" * 20),
    ("mint", "0x" + "22" * 20), ("signature", BLOCK), ("source_slot", SLOT + 1), ("t0", 1001)])
def test_cached_source_cannot_move_to_another_unit(field, value):
    source = wrapper(Reader())
    source("eth_getTransactionReceipt", [HASH])
    changed = dict(UNIT, **{field: value})
    restarted = Reader()
    with pytest.raises(ValueError, match="source_cached_identity_mismatch"):
        wrapper(restarted, unit=changed, cached=source.cached)("eth_getTransactionReceipt", [changed["signature"]])
    assert restarted.calls == []


def test_cache_without_exact_unit_binding_is_rejected():
    source = wrapper(Reader())
    source("eth_getTransactionReceipt", [HASH])
    cached = source.cached
    cached.pop("source_unit_binding", None)
    with pytest.raises(ValueError, match="source_cached_identity_mismatch"):
        wrapper(Reader(), cached=cached)("eth_getTransactionReceipt", [HASH])


def clock(unit, reader):
    return importlib.import_module("sniper.services.wallet_universe_source").resolve_source_clock(unit, reader)


def test_solana_clock_is_bound_to_successful_exact_transaction():
    calls = []
    def read(method, params):
        calls.append((method, params))
        return dict(meta={"err": None}, transaction={"signatures": ["source-signature"]}, blockTime=1000, slot=123)
    result = clock(dict(chain="solana", signature="source-signature", source_slot=123), read)
    assert result == dict(method="wallet-universe-source-clock-v1", t0=1000, source_slot=123,
                          signature="source-signature", chain="solana", observed_from="successful_transaction_header")
    assert calls == [("getTransaction", ["source-signature", dict(encoding="json", commitment="confirmed",
                                                                  maxSupportedTransactionVersion=1)])]


@pytest.mark.parametrize("field,value", [
    ("meta", {}), ("meta", {"err": "failed"}), ("transaction", {"signatures": ["different"]}),
    ("blockTime", None), ("blockTime", 0), ("blockTime", True), ("blockTime", "1000"),
    ("slot", True), ("slot", "123"), ("slot", 124)])
def test_solana_clock_refuses_failed_wrong_or_missing_headers(field, value):
    transaction = dict(meta={"err": None}, transaction={"signatures": ["source-signature"]}, blockTime=1000, slot=123)
    transaction[field] = value
    with pytest.raises(ValueError, match="source_"):
        clock(dict(chain="solana", signature="source-signature", source_slot=123), lambda *_: transaction)


def test_solana_clock_can_recover_missing_ledger_slot():
    transaction = dict(meta={"err": None}, transaction={"signatures": ["source-signature"]}, blockTime=1000, slot=123)
    assert clock(dict(chain="solana", signature="source-signature"), lambda *_: transaction)["source_slot"] == 123


def test_evm_clock_uses_known_block_without_claiming_transaction_proof():
    reader = Reader()
    result = clock(dict(UNIT, t0=None), reader)
    assert result == dict(method="wallet-universe-source-clock-v1", t0=1000, source_slot=SLOT,
                          signature=HASH, chain="56", block_hash=BLOCK, observed_from="canonical_block_header")
    assert reader.calls == [("eth_getBlockByNumber", [hex(SLOT), False])]


def test_evm_missing_slot_is_resolved_from_exact_successful_receipt():
    calls = []
    def read(method, params):
        calls.append((method, params))
        if method == "eth_getTransactionReceipt":
            return dict(transactionHash=HASH, blockNumber=hex(SLOT), blockHash=BLOCK, status="0x1")
        return dict(number=hex(SLOT), hash=BLOCK, timestamp="0x3e8")
    result = clock(dict(UNIT, source_slot=None, t0=None), read)
    assert result["source_slot"] == SLOT and result["block_hash"] == BLOCK
    assert calls == [("eth_getTransactionReceipt", [HASH]), ("eth_getBlockByNumber", [hex(SLOT), False])]


@pytest.mark.parametrize("field,value", [("number", "0x1"), ("hash", "0x123"), ("timestamp", "0x0"), ("timestamp", None)])
def test_evm_clock_refuses_wrong_or_missing_header(field, value):
    reader = Reader()
    reader.header[field] = value
    with pytest.raises(ValueError, match="source_"):
        clock(dict(UNIT, t0=None), reader)


@pytest.mark.parametrize("field,value", [("transactionHash", BLOCK), ("status", "0x0"), ("blockHash", HASH)])
def test_evm_missing_slot_requires_receipt_identity_and_canonical_block(field, value):
    receipt = dict(transactionHash=HASH, blockNumber=hex(SLOT), blockHash=BLOCK, status="0x1")
    receipt[field] = value
    def read(method, params):
        return receipt if method == "eth_getTransactionReceipt" else dict(number=hex(SLOT), hash=BLOCK, timestamp="0x3e8")
    with pytest.raises(ValueError, match="source_"):
        clock(dict(UNIT, source_slot=None, t0=None), read)


@pytest.mark.parametrize("error", ["missing_result", "rate_limit", "provider_timeout"])
@pytest.mark.parametrize("chain", ["solana", "56"])
def test_clock_provider_errors_do_not_retry_or_fabricate_time(chain, error):
    calls = []
    def read(method, params):
        calls.append((method, params))
        raise ValueError(error)
    with pytest.raises(ValueError, match=error):
        clock(dict(UNIT, chain=chain, t0=None, source_slot=None), read)
    assert len(calls) == (2 if chain == "56" and error == "missing_result" else 1)


class HashReader(Reader):
    def __init__(self):
        super().__init__()
        self.source_transaction = deepcopy(self.block["transactions"][36])
        self.source_transaction.update(to="0x" + "66" * 20, input="0x12345678")
        self.block["transactions"] = [tx["hash"] for tx in self.block["transactions"]]
        self.lookup_error = None

    def __call__(self, method, params):
        if method == "eth_getTransactionByHash":
            self.calls.append((method, deepcopy(params)))
            assert params == [HASH]
            if self.lookup_error:
                raise ValueError(self.lookup_error)
            return deepcopy(self.source_transaction)
        return super().__call__(method, params)


def test_hash_only_block_recovers_exact_source_body_and_keeps_relayer_sender():
    reader = HashReader()
    relayer = "0x" + "55" * 20
    reader.source_transaction["from"] = relayer
    source = wrapper(reader)
    result = source("eth_getTransactionReceipt", [HASH])
    assert result["from"] == relayer and result["transactionIndex"] == "0x24"
    assert result["logs"] == reader.logs
    assert result["observation_source"] == "canonical_block_transaction_and_logs"
    assert [method for method, _ in reader.calls] == ["eth_getTransactionReceipt", "eth_getBlockByNumber",
        "eth_getTransactionByHash", "eth_getLogs", "eth_getBlockByNumber"]
    restarted = Reader()
    assert wrapper(restarted, cached=source.cached)("eth_getTransactionReceipt", [HASH]) == result
    assert len(restarted.calls) == 1


@pytest.mark.parametrize("field,value", [("hash", BLOCK), ("blockHash", HASH), ("blockNumber", "0x1"),
    ("transactionIndex", "0x23"), ("from", None), ("to", "bad"), ("input", None), ("input", "0x123")])
def test_hash_only_lookup_rejects_wrong_or_partial_source_body(field, value):
    reader = HashReader()
    reader.source_transaction[field] = value
    with pytest.raises(ValueError, match="source_"):
        wrapper(reader)("eth_getTransactionReceipt", [HASH])
    assert [method for method, _ in reader.calls][-1] == "eth_getTransactionByHash"


@pytest.mark.parametrize("defect", ["duplicate_source", "duplicate_other", "missing_source", "malformed_hash"])
def test_hash_list_must_uniquely_locate_exact_source_before_body_lookup(defect):
    reader = HashReader()
    hashes = reader.block["transactions"]
    if defect == "duplicate_source": hashes[0] = HASH
    if defect == "duplicate_other": hashes[0] = hashes[1]
    if defect == "missing_source": hashes[36] = BLOCK
    if defect == "malformed_hash": hashes[0] = "bad"
    with pytest.raises(ValueError, match="source_"):
        wrapper(reader)("eth_getTransactionReceipt", [HASH])
    assert len(reader.calls) == 2


@pytest.mark.parametrize("error", ["missing_result", "provider_timeout", "rate_limit", "provider_rejected"])
def test_hash_lookup_errors_do_not_retry_or_proceed_to_logs(error):
    reader = HashReader()
    reader.lookup_error = error
    with pytest.raises(ValueError, match=error):
        wrapper(reader)("eth_getTransactionReceipt", [HASH])
    assert len(reader.calls) == 3


def test_hash_only_source_still_rejects_reorg_after_logs():
    reader = HashReader()
    reader.header["hash"] = HASH
    with pytest.raises(ValueError, match="source_reorg"):
        wrapper(reader)("eth_getTransactionReceipt", [HASH])
    assert len(reader.calls) == 5


def relay_source_fixture(hash_only, recipient="proxy"):
    import test_wallet_universe_flow as fixtures
    unit = fixtures.relay_unit()
    provider = fixtures.relay_reader(unit)
    destination = {"proxy": fixtures.RELAY_PROXY, "wrong": "0x" + "77" * 20, None: None}[recipient]
    transactions = [dict(hash="0x" + format(index, "064x"), blockNumber="0x64",
                         blockHash=fixtures.block_hash(100), transactionIndex=hex(index),
                         **{"from": provider.receipt["from"]}, to=destination, input="0x12345678")
                    for index in range(6)]
    transactions[5]["hash"] = fixtures.SRC
    def read(method, params):
        if method == "eth_getTransactionReceipt":
            raise ValueError("missing_result")
        if method == "eth_getBlockByNumber" and params[1] is True:
            block = provider(method, params)
            block["transactions"] = [tx["hash"] for tx in transactions] if hash_only else deepcopy(transactions)
            return block
        if method == "eth_getTransactionByHash":
            return deepcopy(next(tx for tx in transactions if tx["hash"] == fixtures.SRC))
        if method == "eth_getLogs" and "blockHash" in params[0]:
            return deepcopy(provider.receipt["logs"])
        return provider(method, params)
    return unit, provider, read, transactions


@pytest.mark.parametrize("hash_only", [False, True])
@pytest.mark.parametrize("recipient", ["proxy", "wrong", None])
def test_recovered_source_preserves_recipient_for_real_relay_collector(hash_only, recipient):
    import test_wallet_universe_flow as fixtures
    from sniper.services.wallet_universe_flow import collect_evm
    unit, provider, read, transactions = relay_source_fixture(hash_only, recipient)
    destination = transactions[5]["to"]
    source = wrapper(read, unit=unit)
    result = collect_evm(unit, source, pools=fixtures.POOLS)
    assert result["source_found"] is (recipient == "proxy"), result
    assert (result["status"] == "complete") is (recipient == "proxy"), result
    assert source.cached["to"] == destination
    if recipient == "proxy":
        assert result["source_proof"]["kind"] == "relay_beneficiary_purchase"
        assert result["source_proof"]["transaction_sender"] == provider.receipt["from"]
    restored = wrapper(read, unit=unit, cached=source.cached)
    assert restored("eth_getTransactionReceipt", [fixtures.SRC])["to"] == destination


@pytest.mark.parametrize("reader_class", [Reader, HashReader])
@pytest.mark.parametrize("kind", ["missing", "malformed"])
def test_source_recipient_must_be_explicit_and_well_formed(reader_class, kind):
    provider = reader_class()
    tx = provider.source_transaction if isinstance(provider, HashReader) else provider.block["transactions"][36]
    if kind == "missing": del tx["to"]
    else: tx["to"] = "bad"
    with pytest.raises(ValueError, match="source_"):
        wrapper(provider)("eth_getTransactionReceipt", [HASH])


@pytest.mark.parametrize("kind", ["missing", "malformed"])
def test_cached_recipient_is_validated(kind):
    source = wrapper(Reader())
    source("eth_getTransactionReceipt", [HASH])
    cached = source.cached
    if kind == "missing": cached.pop("to", None)
    else: cached["to"] = "bad"
    with pytest.raises(ValueError, match="source_"):
        wrapper(Reader(), cached=cached)("eth_getTransactionReceipt", [HASH])


@pytest.mark.parametrize("hash_only", [False, True])
@pytest.mark.parametrize("changed_order", [False, True])
def test_legacy_cache_migration_reproves_source_before_resuming_real_flow(hash_only, changed_order):
    import test_wallet_universe_flow as fixtures
    from sniper.services.wallet_universe_flow import collect_evm
    unit, provider, read, transactions = relay_source_fixture(hash_only)
    first = wrapper(read, unit=unit)
    initial = collect_evm(unit, first, pools=fixtures.POOLS)
    assert initial["status"] == "complete"
    legacy = first.cached
    legacy.pop("source_view_version", None)
    legacy.pop("to")
    if changed_order:
        transactions[4]["hash"], transactions[5]["hash"] = transactions[5]["hash"], transactions[4]["hash"]
        for log in provider.receipt["logs"]:
            log["transactionIndex"] = "0x4"
    calls = []
    def paced(method, params):
        calls.append((method, deepcopy(params)))
        return read(method, params)
    migrated = wrapper(paced, unit=unit, cached=legacy)
    resumed = collect_evm(unit, migrated, pools=fixtures.POOLS, state=initial["state"])
    assert sum(method == "eth_getTransactionReceipt" for method, _ in calls) == 1
    assert migrated.cached["source_view_version"] == "wallet-universe-source-view-v2"
    assert migrated.cached["to"] == fixtures.RELAY_PROXY
    if changed_order:
        assert resumed["source_found"] is False and resumed["detail"] == "source_order_changed"
    else:
        assert resumed["status"] == "complete" and resumed["source_found"] is True
        assert resumed["quotes"] == initial["quotes"]
        assert not any(method == "eth_getLogs" and "fromBlock" in params[0] for method, params in calls)
    before = len(calls)
    migrated("eth_getTransactionReceipt", [fixtures.SRC])
    assert len(calls) == before


def test_current_version_malformed_cache_cannot_silently_refresh_around_failure():
    source = wrapper(Reader())
    source("eth_getTransactionReceipt", [HASH])
    cached = source.cached
    cached["source_view_version"] = "wallet-universe-source-view-v2"
    cached.pop("to", None)
    provider = Reader()
    with pytest.raises(ValueError, match="source_missing_recipient"):
        wrapper(provider, cached=cached)("eth_getTransactionReceipt", [HASH])
    assert provider.calls == []


def test_legacy_cache_is_discarded_even_if_independent_recovery_hits_budget():
    source = wrapper(Reader())
    source("eth_getTransactionReceipt", [HASH])
    legacy = source.cached
    legacy.pop("source_view_version", None)
    legacy.pop("to")
    provider = Reader()
    provider.error = "budget"
    migrated = wrapper(provider, cached=legacy)
    with pytest.raises(ValueError, match="budget"):
        migrated("eth_getTransactionReceipt", [HASH])
    assert len(provider.calls) == 1 and migrated.cached is None


@pytest.mark.parametrize("hash_only", [False, True])
@pytest.mark.parametrize("attribution", ["direct", "verified_relay", "unproved_relay"])
def test_missing_slot_source_is_independently_proved_and_cache_resumes_collectors(hash_only, attribution):
    import test_wallet_universe_flow as fixtures
    from sniper.services.wallet_universe_flow import collect_evm
    unit, provider, read, transactions = relay_source_fixture(hash_only)
    unit["source_slot"] = None
    if attribution != "verified_relay": unit["source_initiation"] = None
    if attribution == "direct": transactions[5]["from"] = unit["address"]
    calls = []
    def paced(method, params):
        calls.append((method, deepcopy(params)))
        return read(method, params)
    source = wrapper(paced, unit=unit)
    result = collect_evm(unit, source, pools=fixtures.POOLS)
    assert (result["status"] == "complete") is (attribution != "unproved_relay"), result
    assert source.cached["blockNumber"] == "0x64"
    assert source.cached["source_unit_binding"]["source_slot"] is None
    assert sum(method == "eth_getTransactionByHash" for method, _ in calls) == (2 if attribution == "unproved_relay" else 1)
    calls.clear()
    restored = wrapper(paced, unit=unit, cached=source.cached)
    resumed = collect_evm(unit, restored, pools=fixtures.POOLS, state=result["state"])
    assert resumed["status"] == result["status"] and resumed["quotes"] == result["quotes"]
    assert sum(method == "eth_getTransactionByHash" for method, _ in calls) == (1 if attribution == "unproved_relay" else 0)
    if attribution == "unproved_relay":
        assert resumed["source_found"] is False and resumed["source_proof"] is None


@pytest.mark.parametrize("field,value", [("hash", BLOCK), ("blockHash", None), ("blockNumber", None),
    ("transactionIndex", None), ("from", None), ("to", "bad"), ("input", None)])
def test_missing_slot_lookup_must_be_confirmed_and_fully_bound_before_block_read(field, value):
    reader = HashReader()
    reader.source_transaction[field] = value
    with pytest.raises(ValueError, match="source_"):
        wrapper(reader, unit=dict(UNIT, source_slot=None))("eth_getTransactionReceipt", [HASH])
    assert [method for method, _ in reader.calls] == ["eth_getTransactionReceipt", "eth_getTransactionByHash"]


@pytest.mark.parametrize("defect", ["wrong_hash", "wrong_index", "not_member", "wrong_time", "reorg"])
def test_missing_slot_lookup_needs_canonical_membership_and_closing_header(defect):
    reader = HashReader()
    if defect == "wrong_hash": reader.source_transaction["blockHash"] = HASH
    if defect == "wrong_index": reader.source_transaction["transactionIndex"] = "0x23"
    if defect == "not_member": reader.block["transactions"][36] = BLOCK
    if defect == "wrong_time": reader.block["timestamp"] = "0x3e9"
    if defect == "reorg": reader.header["hash"] = HASH
    with pytest.raises(ValueError, match="source_"):
        wrapper(reader, unit=dict(UNIT, source_slot=None))("eth_getTransactionReceipt", [HASH])
    assert sum(method == "eth_getTransactionByHash" for method, _ in reader.calls) == 1


def test_source_clock_can_use_confirmed_exact_transaction_after_missing_receipt_only():
    reader = HashReader()
    result = clock(dict(UNIT, source_slot=None, t0=None), reader)
    assert result["t0"] == 1000 and result["source_slot"] == SLOT and result["block_hash"] == BLOCK
    assert [method for method, _ in reader.calls] == ["eth_getTransactionReceipt", "eth_getTransactionByHash", "eth_getBlockByNumber"]
    assert "status" not in result and "source_found" not in result


@pytest.mark.parametrize("field,value", [("hash", BLOCK), ("blockHash", None), ("blockNumber", None),
    ("transactionIndex", None), ("from", None), ("to", "bad"), ("input", None)])
def test_source_clock_transaction_fallback_rejects_partial_or_unconfirmed_body(field, value):
    reader = HashReader()
    reader.source_transaction[field] = value
    with pytest.raises(ValueError, match="source_"):
        clock(dict(UNIT, source_slot=None, t0=None), reader)
    assert len(reader.calls) == 2


@pytest.mark.parametrize("error", ["provider_timeout", "provider_rejected", "rate_limit"])
def test_missing_slot_transaction_lookup_errors_are_not_retried(error):
    reader = HashReader()
    reader.lookup_error = error
    with pytest.raises(ValueError, match=error):
        wrapper(reader, unit=dict(UNIT, source_slot=None))("eth_getTransactionReceipt", [HASH])
    assert len(reader.calls) == 2


def cached_unknown_slot_recovery(limit=20):
    from test_wallet_universe_recovery import Reader as CountedReader
    from sniper.services.wallet_universe_recovery import SharedResearchRead
    unit = dict(UNIT, source_slot=None)
    provider = HashReader()
    initial = wrapper(provider, unit=unit)
    initial("eth_getTransactionReceipt", [HASH])
    primary, archive = CountedReader("missing_result"), CountedReader()
    archive.values["eth_getTransactionByHash"] = provider.source_transaction
    archive.values["eth_getBlockByNumber"] = provider.header
    shared = SharedResearchRead(primary, lambda: archive, limit)
    resumed = wrapper(shared.for_unit(unit), unit=unit, cached=initial.cached)
    return resumed, shared, primary, archive


def test_cached_unknown_slot_learns_archive_header_permission_from_exact_transaction():
    resumed, shared, primary, archive = cached_unknown_slot_recovery()
    result = resumed("eth_getTransactionReceipt", [HASH])
    assert result["source_unit_binding"]["source_slot"] is None and result["blockNumber"] == hex(SLOT)
    assert [method for method, _, _ in primary.calls] == ["eth_getBlockByNumber", "eth_getTransactionByHash", "eth_getBlockByNumber"]
    assert [method for method, _, _ in archive.calls] == ["eth_chainId", "eth_getTransactionByHash", "eth_getBlockByNumber"]
    assert shared.used == 6
    assert resumed("eth_getTransactionReceipt", [HASH]) == result and shared.used == 6


@pytest.mark.parametrize("field,value", [("hash", BLOCK), ("blockHash", HASH), ("blockNumber", "0x1"),
    ("transactionIndex", "0x23"), ("from", "0x" + "55" * 20), ("to", "0x" + "77" * 20)])
def test_cached_unknown_slot_lookup_must_match_stored_source_before_retry(field, value):
    resumed, shared, primary, archive = cached_unknown_slot_recovery()
    archive.values["eth_getTransactionByHash"][field] = value
    with pytest.raises(ValueError, match="source_"):
        resumed("eth_getTransactionReceipt", [HASH])
    assert shared.used == 4 and archive.calls[-1][0] == "eth_getTransactionByHash"


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5])
def test_cached_unknown_slot_recovery_cannot_exceed_shared_attempt_budget(limit):
    resumed, shared, primary, archive = cached_unknown_slot_recovery(limit)
    with pytest.raises(ValueError, match="budget"):
        resumed("eth_getTransactionReceipt", [HASH])
    assert shared.used == primary.used + archive.used == limit


def test_cached_unknown_slot_transport_failure_does_not_trigger_extra_lookup():
    resumed, shared, primary, archive = cached_unknown_slot_recovery()
    primary.error = "provider_timeout"
    with pytest.raises(ValueError, match="provider_timeout"):
        resumed("eth_getTransactionReceipt", [HASH])
    assert shared.used == 1 and not archive.calls
