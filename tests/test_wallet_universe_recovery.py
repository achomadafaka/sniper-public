"""Offline archive fallback boundaries; each fake counts actual attempts."""
from copy import deepcopy
import importlib

import pytest

SOURCE = "0x" + "aa" * 32
OTHER = "0x" + "bb" * 32
BLOCK = "0x" + "cc" * 32
UNIT = dict(chain="56", signature=SOURCE, source_slot=123)
RELAY_PROXY = "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be"
RELAY_ROUTER = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"


class Reader:
    def __init__(self, error=None, *, limit=100):
        self.error, self.limit, self.used = error, limit, 0
        self.calls = []
        self.closed = 0
        self.values = {"eth_chainId": "0x38",
                       "eth_getTransactionReceipt": dict(transactionHash=SOURCE, blockNumber="0x7b", blockHash=BLOCK, transactionIndex="0x1"),
                       "eth_getTransactionByHash": dict(hash=SOURCE, blockNumber="0x7b", blockHash=BLOCK, transactionIndex="0x1"),
                       "eth_getBlockByNumber": dict(number="0x7b", hash=BLOCK, timestamp="0x3e8")}

    def __call__(self, method, params):
        if self.used >= self.limit:
            raise ValueError("budget")
        self.used += 1
        self.calls.append((method, deepcopy(params), self.limit))
        if self.error:
            raise ValueError(self.error)
        return deepcopy(self.values.get(method, "primary-result"))

    def close(self):
        self.closed += 1


def facade(primary, recovery, limit=100):
    factories = []
    def factory():
        factories.append(True)
        return recovery
    cls = importlib.import_module("sniper.services.wallet_universe_recovery").SharedResearchRead
    return cls(primary, factory, limit), factories


def test_plain_facade_stays_primary_only_and_factory_is_lazy():
    primary, archive = Reader("missing_result"), Reader()
    read, factories = facade(primary, archive)
    with pytest.raises(ValueError, match="missing_result"):
        read("eth_getTransactionReceipt", [SOURCE])
    assert read.used == 1 and not factories and not archive.calls


def test_exact_source_fallback_verifies_chain_once_and_counts_all_attempts():
    primary, archive = Reader("missing_result"), Reader()
    read, factories = facade(primary, archive)
    unit = read.for_unit(UNIT)
    for method in ("eth_getTransactionReceipt", "eth_getTransactionByHash"):
        unit(method, [SOURCE])
    assert read.used == 5 == primary.used + archive.used
    assert len(factories) == 1
    assert [method for method, _, _ in archive.calls] == ["eth_chainId", "eth_getTransactionReceipt", "eth_getTransactionByHash"]
    assert primary.limit == archive.limit == 100


@pytest.mark.parametrize("limit", range(7))
def test_total_budget_includes_failed_primary_identity_and_recovery_attempts(limit):
    primary, archive = Reader("missing_result"), Reader()
    read, _ = facade(primary, archive, limit)
    unit = read.for_unit(UNIT)
    for _ in range(4):
        try: unit("eth_getTransactionReceipt", [SOURCE])
        except ValueError as exc: assert str(exc) == "budget"
    assert read.used == primary.used + archive.used == limit
    assert all(cap <= limit for _, _, cap in primary.calls + archive.calls)
    assert primary.limit == archive.limit == 100


@pytest.mark.parametrize("error", ["provider_timeout", "provider_transport", "rate_limit", "provider_rejected", "rpc_rejected", "malformed_response"])
def test_non_missing_primary_failures_do_not_fallback(error):
    primary, archive = Reader(error), Reader()
    read, factories = facade(primary, archive)
    with pytest.raises(ValueError, match=error): read.for_unit(UNIT)("eth_getTransactionReceipt", [SOURCE])
    assert read.used == 1 and not factories


@pytest.mark.parametrize("method,params", [
    ("eth_getTransactionReceipt", [OTHER]), ("eth_getTransactionByHash", [OTHER]),
    ("eth_getLogs", [{"blockHash": BLOCK}]), ("eth_call", [{"to": OTHER, "data": "0x313ce567"}, "0x7b"]),
    ("eth_getBlockByNumber", ["0x7c", False]), ("eth_getBlockByNumber", ["latest", False]),
    ("eth_getBalance", [SOURCE, "0x7b"])])
def test_other_queries_cannot_use_archive(method, params):
    read, factories = facade(Reader("missing_result"), Reader())
    with pytest.raises(ValueError, match="missing_result"): read.for_unit(UNIT)(method, params)
    assert not factories


def test_non_bsc_units_cannot_use_recovery():
    read, factories = facade(Reader("missing_result"), Reader())
    with pytest.raises(ValueError, match="missing_result"):
        read.for_unit(dict(UNIT, chain="8453"))("eth_getTransactionReceipt", [SOURCE])
    assert not factories


@pytest.mark.parametrize("slot", [123, None])
def test_exact_source_clock_header_can_recover_after_independent_slot_learning(slot):
    primary, archive = Reader("missing_result"), Reader()
    read, _ = facade(primary, archive)
    unit = read.for_unit(dict(UNIT, source_slot=slot))
    if slot is None: unit("eth_getTransactionByHash", [SOURCE])
    assert unit("eth_getBlockByNumber", ["0x7b", False])["hash"] == BLOCK
    assert archive.calls[-1][:2] == ("eth_getBlockByNumber", ["0x7b", False])


def test_unknown_slot_is_not_learned_from_a_wrong_source_response():
    primary, archive = Reader(), Reader()
    primary.values["eth_getTransactionByHash"]["hash"] = OTHER
    read, factories = facade(primary, archive)
    unit = read.for_unit(dict(UNIT, source_slot=None))
    unit("eth_getTransactionByHash", [SOURCE])
    primary.error = "missing_result"
    with pytest.raises(ValueError, match="missing_result"): unit("eth_getBlockByNumber", ["0x7b", False])
    assert not factories


def test_slot_can_be_learned_from_primary_receipt_without_using_archive():
    primary, archive = Reader(), Reader()
    read, factories = facade(primary, archive)
    unit = read.for_unit(dict(UNIT, source_slot=None))
    unit("eth_getTransactionReceipt", [SOURCE])
    assert not factories
    primary.error = "missing_result"
    assert unit("eth_getBlockByNumber", ["0x7b", True])["number"] == "0x7b"


def test_wrong_recovery_chain_never_receives_source_query():
    primary, archive = Reader("missing_result"), Reader()
    archive.values["eth_chainId"] = "0x1"
    read, _ = facade(primary, archive)
    with pytest.raises(ValueError, match="wrong_chain"):
        read.for_unit(UNIT)("eth_getTransactionReceipt", [SOURCE])
    assert read.used == 2 and [method for method, _, _ in archive.calls] == ["eth_chainId"]


def test_recovery_failure_is_counted_and_never_retried():
    primary, archive = Reader("missing_result"), Reader()
    read, _ = facade(primary, archive)
    unit = read.for_unit(UNIT)
    unit("eth_getTransactionReceipt", [SOURCE])
    archive.error = "provider_timeout"
    with pytest.raises(ValueError, match="provider_timeout"): unit("eth_getTransactionReceipt", [SOURCE])
    assert read.used == 5 and archive.limit == 100


def test_independent_unit_closures_do_not_share_learned_slots():
    primary, archive = Reader(), Reader()
    read, factories = facade(primary, archive)
    first = read.for_unit(dict(UNIT, source_slot=None))
    second = read.for_unit(dict(UNIT, signature=OTHER, source_slot=None))
    first("eth_getTransactionByHash", [SOURCE])
    primary.error = "missing_result"
    with pytest.raises(ValueError, match="missing_result"): second("eth_getBlockByNumber", ["0x7b", False])
    assert not factories


def test_close_owns_both_readers_and_is_idempotent():
    primary, archive = Reader("missing_result"), Reader()
    read, _ = facade(primary, archive)
    read.for_unit(UNIT)("eth_getTransactionReceipt", [SOURCE])
    read.close(); read.close()
    assert primary.closed == archive.closed == 1


def test_close_unused_facade_does_not_instantiate_recovery():
    primary, archive = Reader(), Reader()
    read, factories = facade(primary, archive)
    read.close()
    assert primary.closed == 1 and archive.closed == 0 and not factories


@pytest.mark.parametrize("limit", [2, 3])
def test_actual_readrpc_attempt_and_pacing_boundaries_include_archive_identity(limit):
    from sniper.services.wallet_universe_metrics import ReadRpc
    sent, paced = [], []
    class Response:
        status_code = 200
        def __init__(self, body): self.body = body
        def json(self): return self.body
    def post(url, *, json, **kwargs):
        sent.append((url, json["method"]))
        value = None
        if url == "https://archive.example":
            value = "0x38" if json["method"] == "eth_chainId" else dict(transactionHash=SOURCE,
                blockNumber="0x7b", blockHash=BLOCK, transactionIndex="0x1")
        return Response(dict(jsonrpc="2.0", id=json["id"], result=value))
    primary = ReadRpc("https://primary.example", limit=99, post=post, pace=lambda: paced.append(True))
    archive = ReadRpc("https://archive.example", limit=99, post=post, pace=lambda: paced.append(True))
    read, _ = facade(primary, archive, limit)
    if limit == 2:
        with pytest.raises(ValueError, match="budget"):
            read.for_unit(UNIT)("eth_getTransactionReceipt", [SOURCE])
    else:
        assert read.for_unit(UNIT)("eth_getTransactionReceipt", [SOURCE])["transactionHash"] == SOURCE
    assert read.used == len(sent) == len(paced) == limit
    assert primary.limit == archive.limit == 99


def test_preexisting_primary_attempts_consume_shared_budget():
    primary, archive = Reader("missing_result"), Reader()
    primary.used = 7
    read, _ = facade(primary, archive, 10)
    read.for_unit(UNIT)("eth_getTransactionReceipt", [SOURCE])
    assert read.used == 10
    with pytest.raises(ValueError, match="budget"):
        read("eth_chainId", [])
    assert read.used == 10


def test_close_archive_even_when_primary_close_fails():
    primary, archive = Reader("missing_result"), Reader()
    read, _ = facade(primary, archive)
    read.for_unit(UNIT)("eth_getTransactionReceipt", [SOURCE])
    def fail_close(): raise ValueError("close_failed")
    primary.close = fail_close
    with pytest.raises(ValueError, match="close_failed"): read.close()
    assert archive.closed == 1


@pytest.mark.parametrize("empty_primary", [False, True])
def test_only_pinned_historical_relay_code_uses_archive_under_shared_budget(empty_primary):
    primary, archive = Reader(None if empty_primary else "missing_result"), Reader()
    primary.values["eth_getCode"] = "0x"
    archive.values["eth_getCode"] = "0x60016002"
    read, _ = facade(primary, archive, 5)
    source = read.for_unit(UNIT)
    for contract in (RELAY_PROXY, RELAY_ROUTER):
        assert source("eth_getCode", [contract, "0x7b"]) == "0x60016002"
    assert read.used == 5
    assert [method for method, _, _ in archive.calls] == ["eth_chainId", "eth_getCode", "eth_getCode"]
    assert all(params[1] == "0x7b" for method, params, _ in archive.calls if method == "eth_getCode")


@pytest.mark.parametrize("params", [[RELAY_PROXY, "latest"], [RELAY_PROXY, "0x7c"],
    ["0x" + "11" * 20, "0x7b"], [RELAY_ROUTER, {"blockHash": BLOCK}], [RELAY_PROXY, "0x7b", "extra"]])
@pytest.mark.parametrize("empty_primary", [False, True])
def test_code_archive_cannot_expand_contract_or_block_scope(params, empty_primary):
    primary, archive = Reader(None if empty_primary else "missing_result"), Reader()
    primary.values["eth_getCode"] = "0x"
    read, factories = facade(primary, archive)
    source = read.for_unit(UNIT)
    if empty_primary:
        assert source("eth_getCode", params) == "0x"
    else:
        with pytest.raises(ValueError, match="missing_result"): source("eth_getCode", params)
    assert read.used == 1 and not factories


def test_code_archive_requires_exact_source_learning_when_unit_slot_unknown():
    primary, archive = Reader(), Reader()
    primary.values["eth_getCode"] = "0x"
    archive.values["eth_getCode"] = "0x6001"
    read, factories = facade(primary, archive)
    source = read.for_unit(dict(UNIT, source_slot=None))
    assert source("eth_getCode", [RELAY_PROXY, "0x7b"]) == "0x" and not factories
    source("eth_getTransactionByHash", [SOURCE])
    assert source("eth_getCode", [RELAY_PROXY, "0x7b"]) == "0x6001"
    assert read.used == 5


@pytest.mark.parametrize("code", ["0x00", "0x6000", "0X", None, "bad"])
def test_nonempty_or_malformed_primary_code_is_not_replaced(code):
    primary, archive = Reader(), Reader()
    primary.values["eth_getCode"] = code
    read, factories = facade(primary, archive)
    assert read.for_unit(UNIT)("eth_getCode", [RELAY_PROXY, "0x7b"]) == code
    assert read.used == 1 and not factories


@pytest.mark.parametrize("error", ["provider_timeout", "rate_limit", "provider_rejected"])
def test_code_transport_and_provider_errors_do_not_trigger_archive(error):
    read, factories = facade(Reader(error), Reader())
    with pytest.raises(ValueError, match=error):
        read.for_unit(UNIT)("eth_getCode", [RELAY_PROXY, "0x7b"])
    assert read.used == 1 and not factories


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_code_archive_identity_and_fetch_share_hard_total_cap(limit):
    primary, archive = Reader(), Reader()
    primary.values["eth_getCode"] = "0x"
    read, _ = facade(primary, archive, limit)
    with pytest.raises(ValueError, match="budget"):
        read.for_unit(UNIT)("eth_getCode", [RELAY_PROXY, "0x7b"])
    assert read.used == primary.used + archive.used == limit


def test_historical_code_archive_refuses_wrong_chain_and_never_uses_head():
    primary, archive = Reader(), Reader()
    primary.values["eth_getCode"] = "0x"
    archive.values["eth_chainId"] = "0x1"
    read, _ = facade(primary, archive)
    with pytest.raises(ValueError, match="wrong_chain"):
        read.for_unit(UNIT)("eth_getCode", [RELAY_PROXY, "0x7b"])
    assert read.used == 2 and archive.calls[0][0] == "eth_chainId" and len(archive.calls) == 1


def test_empty_archive_code_is_returned_for_hash_validator_without_another_retry():
    primary, archive = Reader(), Reader()
    primary.values["eth_getCode"] = archive.values["eth_getCode"] = "0x"
    read, _ = facade(primary, archive)
    assert read.for_unit(UNIT)("eth_getCode", [RELAY_PROXY, "0x7b"]) == "0x"
    assert read.used == 3
