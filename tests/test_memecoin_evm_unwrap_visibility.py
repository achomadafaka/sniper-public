"""Receipt proof and endpoint balance checks for native entry pre-transactions."""
import json
import sqlite3

import pytest
from eth_account import Account
from eth_utils import keccak

from sniper.execution import evm_real_seams as seams, evm_rpc_pool as pool
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg


@pytest.fixture
def entry(monkeypatch):
    """Run the real entry builder with an in-memory ledger and offline RPC responses."""
    primary, secondary = "https://fast.invalid", "https://stale.invalid"
    pool.reset_for_tests()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"4663": (primary, secondary)})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_NATIVE_VISIBLE_WAIT_S", 0.02)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_UNWRAP_CONFIRM_S", 0.25)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", False)
    for name in ("MEMECOIN_EVM_V2_ENTRY_ENABLED", "MEMECOIN_EVM_V4_ENABLED",
                 "MEMECOIN_EVM_RELAY_ENABLED", "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED"):
        monkeypatch.setattr(cfg, name, False)
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663")
    monkeypatch.setattr(seams, "quote_usd_price", lambda *_a, **_k: 1.0)
    for url, latency in ((primary, 1), (secondary, 300)):
        pool.mark_identity(url, True)
        for _ in range(5):
            pool.record(url, latency, True)

    def run(*, confirmed, ambiguous=False, primary_balance=10 * 10**18,
            secondary_balance=0, slow=False):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(conn)
        mint = "0x" + "33" * 20
        pid = m.reserve_pending(
            conn, mint=mint, source_user_id="u", strategy="alpha_b", notional=10.0,
            reserved_at="2026-09-18T00:00:00Z", network_id="4663")
        requests, sends = [], []
        amount = 10 * 10**18
        weth = seams.CHAIN_QUOTE["4663"]["token"]

        def quote(_sell, _buy, _amount, **_kwargs):
            return {
                "sellAmount": str(amount), "buyAmount": "5000000000000000000",
                "issues": {"allowance": None},
                "transaction": {"to": "0x" + "44" * 20, "data": "0x", "value": hex(amount)},
                "pre_transactions": [{"to": weth, "data": "0x2e1a7d4d" + f"{amount:064x}",
                                      "value": "0x0", "gas": 60000}],
            }

        def http(_method, url, body=None, timeout=10, **_kwargs):
            req = json.loads(body)
            method = req["method"]
            requests.append((method, url, timeout))
            if method == "eth_chainId":
                result = "0x1237"
            elif method == "eth_getTransactionCount":
                result = "0x7"
            elif method == "eth_gasPrice":
                result = "0x3b9aca00"
            elif method == "eth_sendRawTransaction":
                sends.append(req["params"][0])
                if ambiguous and len(sends) == 1:
                    return 200, {"error": {"code": -32000, "message": "provider unavailable"}}, None
                result = "0x" + keccak(hexstr=req["params"][0]).hex().removeprefix("0x")
            elif method == "eth_getTransactionReceipt":
                result = {"status": "0x1", "transactionHash": req["params"][0]} if confirmed or len(sends) > 1 else None
            elif method == "eth_getBalance":
                if slow:
                    assert timeout <= 0.02, "the balance request exceeds its remaining deadline"
                    raise TimeoutError("balance read exceeded its timeout")
                result = hex(primary_balance if url == primary else secondary_balance)
            elif method == "eth_getTransactionByHash":
                result = None
            elif method == "eth_call":
                result = "0x0"
            else:
                raise AssertionError(method)
            return 200, {"result": result}, None

        balances = iter([100.0, 90.0])
        tokens = iter([0.0, 5.0])
        from evm_receipt_fake import wrap
        acct = Account.create()
        executor = seams.build_real_evm_executor(
            acct=acct, chain_id="4663", rpc_url=primary,
            http=wrap(http, owner=acct.address, mint=mint, quote=weth, coin=5,
                      value=hex(amount)),                 # the swap's receipt: native paid
            venue_quote_fn=quote, usdc_fn=lambda: next(balances, 90.0),
            token_fn=lambda _mint: next(tokens, 5.0))
        result = executor["execute_fn"](conn, pid, mint, 10.0, "2026-09-18T00:00:01Z")
        row = m.pending_row(conn, pid)
        conn.close()
        return result, row, requests, sends

    yield run
    pool.reset_for_tests()


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unconfirmed_unwrap_keeps_its_reservation_without_cleanup(entry, ambiguous):
    result, row, requests, sends = entry(confirmed=False, ambiguous=ambiguous, primary_balance=0)
    assert result["status"] == "unknown"
    assert row["status"] == "reserved"
    assert row["tx_sig"] == result["tx_hash"]
    assert row["recovery_status"] == "native_pre_transaction_ambiguous"
    assert not row["cleanup_tx_sig"]
    assert len(sends) == 1
    assert not any(method == "eth_getBalance" for method, _, _ in requests)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_confirmed_unwrap_checks_each_endpoint_before_swap(entry, ambiguous):
    result, row, requests, sends = entry(confirmed=True, ambiguous=ambiguous)
    # ⚠️ AMBIGUOUS, NOT ABORTED (2026-09-23, LINTCHA 9021c685). The receipt confirmed, so the
    # unwrap HAPPENED -- that attempt's transaction is `success` at block 70750681 -- and only a
    # pool member's balance view lagged. All 5 pre-transactions this system called ambiguous or
    # failed in the 30 days to 2026-09-23 succeeded on chain, so the row may not claim a failure.
    # Everything else this test pins is unchanged: the swap still never goes out, the landed ETH is
    # still rewrapped, the receipt is still read before any balance, and both endpoints are asked.
    assert result["status"] == "unknown", requests
    assert result["reason"] == "native pre-transaction ambiguous"
    assert row["cleanup_status"] == "confirmed"
    assert len(sends) == 2
    methods = [method for method, _, _ in requests]
    assert methods.index("eth_getTransactionReceipt") < methods.index("eth_getBalance")
    assert {url for method, url, _ in requests if method == "eth_getBalance"} == {
        "https://fast.invalid", "https://stale.invalid"}


@pytest.mark.parametrize("ambiguous", [False, True])
def test_visible_confirmed_unwrap_allows_the_swap(entry, ambiguous):
    result, row, _requests, sends = entry(
        confirmed=True, ambiguous=ambiguous, secondary_balance=10 * 10**18)
    assert result["status"] == "filled"
    assert row["status"] == "confirmed"
    assert row["nonce"] == 8
    assert len(sends) == 2


def test_balance_reads_use_the_remaining_visibility_budget(entry):
    result, _row, requests, _sends = entry(confirmed=True, slow=True)
    balance_timeouts = [timeout for method, _, timeout in requests if method == "eth_getBalance"]
    assert balance_timeouts
    assert all(0 < timeout <= 0.02 for timeout in balance_timeouts)
    assert result["status"] == "filled"
